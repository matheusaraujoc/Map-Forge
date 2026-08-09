"""Mundo continuo: blocos gerados sob demanda conforme a camera se move.

O `tiling.py` corta uma regiao *fixa* em blocos e gera todos. Isso resolve uma
cidade; nao resolve um pais. A conta e direta, medida em Araioses com o mesmo
gerador:

    detalhe 'medium'    143.600 triangulos/km2    4,7 MB/km2
    detalhe 'distante'   32.500 triangulos/km2    1,7 MB/km2

O Brasil tem 8.510.000 km2. No nivel mais barato que existe aqui, o pais inteiro
daria **277 bilhoes de triangulos e cerca de 14 TB** em glTF; em 'medium', 40 TB.
Nao e so o disco: nenhuma engine carrega 277 bilhoes de triangulos, e nenhuma
placa desenha isso. Um modelo unico do Brasil nao e caro - e inutil.

O que *cabe* e a materia-prima. O OSM do Brasil inteiro tem ~2 GB em PBF, o
relevo a 30 m cerca de 19 GB, e os contornos do Overture algumas dezenas de GB.
Ou seja: **o modelo do Brasil nao existe, mas o Brasil existe** - como dado de
entrada de onde qualquer pedaco pode ser construido em segundos.

E disso que este modulo trata. Nada e pre-gerado:

- o mundo e uma grade fixa de blocos, indexada por (nivel, coluna, linha);
- um bloco so passa a existir quando a camera chega perto;
- o nivel de detalhe cai com a distancia, em aneis;
- o que sai do alcance e descartado por um teto de memoria (LRU).

Assim a memoria depende do raio de visao, nao do tamanho do mundo: ver o Brasil
custa o mesmo que ver Araioses.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .core.geo import BBox
from .generation import GenerationSettings

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]

# Lado do bloco em metros no nivel 0. Cada nivel acima dobra o lado, como um
# mapa de tiles - assim um bloco distante cobre mais chao com menos detalhe.
BASE_TILE_M = 1_000.0

# Aneis de detalhe: ate onde vai cada nivel, em metros a partir da camera, e com
# que detalhe o bloco e gerado. A ordem importa: o primeiro que couber vence.
RINGS: tuple[tuple[float, str], ...] = (
    (1_500.0, "medium"),
    (4_000.0, "low"),
    (12_000.0, "distante"),
)

# Teto de memoria em triangulos. Ao passar disso, os blocos mais distantes da
# camera saem primeiro.
DEFAULT_BUDGET_TRIANGLES = 3_000_000

_METERS_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class TileKey:
    """Endereco de um bloco na grade do mundo."""

    level: int
    col: int
    row: int

    @property
    def size_m(self) -> float:
        return BASE_TILE_M * (2**self.level)

    def __str__(self) -> str:  # pragma: no cover - so para log
        return f"z{self.level}/{self.col}/{self.row}"


@dataclass
class Tile:
    """Um bloco carregado, com o que ele custa e quando foi usado."""

    key: TileKey
    bbox: BBox
    detail: str
    scene: object = None
    triangles: int = 0
    seconds: float = 0.0
    error: str = ""
    last_used: float = field(default_factory=time.monotonic)

    @property
    def ready(self) -> bool:
        return self.scene is not None or bool(self.error)


def tile_bbox(key: TileKey) -> BBox:
    """Retangulo geografico de um bloco.

    A grade e definida em metros na latitude, e em graus de longitude corrigidos
    pelo cosseno da latitude do bloco - o suficiente para blocos de poucos km,
    e sem a distorcao de area do Mercator, que perto do equador nao importa e
    perto do polo tornaria o bloco inutilmente estreito.
    """
    lado = key.size_m
    graus_lat = lado / _METERS_PER_DEG_LAT
    sul = key.row * graus_lat
    norte = sul + graus_lat

    meio = math.radians(max(min((sul + norte) / 2.0, 85.0), -85.0))
    graus_lon = lado / max(_METERS_PER_DEG_LAT * math.cos(meio), 1.0)
    oeste = key.col * graus_lon
    return BBox(sul, oeste, norte, oeste + graus_lon)


def tile_at(lat: float, lon: float, level: int) -> TileKey:
    """Bloco que contem um ponto, num dado nivel."""
    lado = BASE_TILE_M * (2**level)
    graus_lat = lado / _METERS_PER_DEG_LAT
    row = math.floor(lat / graus_lat)

    meio = math.radians(max(min((row + 0.5) * graus_lat, 85.0), -85.0))
    graus_lon = lado / max(_METERS_PER_DEG_LAT * math.cos(meio), 1.0)
    return TileKey(level=level, col=math.floor(lon / graus_lon), row=row)


def _level_for(distance_m: float) -> Optional[tuple[int, str]]:
    """Nivel de grade e detalhe para um bloco a certa distancia da camera."""
    for indice, (alcance, detalhe) in enumerate(RINGS):
        if distance_m <= alcance:
            return indice, detalhe
    return None


def tiles_for_view(lat: float, lon: float, radius_m: float) -> list[tuple[TileKey, str]]:
    """Blocos que precisam existir para uma camera em (lat, lon) vendo `radius_m`.

    Devolve pares (bloco, detalhe), do mais perto para o mais longe - a ordem em
    que devem ser gerados, porque o que esta debaixo do nariz importa primeiro.
    """
    alcance = min(radius_m, RINGS[-1][0])
    escolhidos: dict[TileKey, tuple[float, str]] = {}
    cobertos: list[tuple[float, float, float]] = []  # (lat, lon, raio) ja resolvidos

    for nivel, (limite_anel, detalhe) in enumerate(RINGS):
        limite = min(limite_anel, alcance)
        if limite <= 0:
            continue
        lado = BASE_TILE_M * (2**nivel)
        passos = int(math.ceil(limite / lado)) + 1

        centro = tile_at(lat, lon, nivel)
        for dcol in range(-passos, passos + 1):
            for drow in range(-passos, passos + 1):
                key = TileKey(nivel, centro.col + dcol, centro.row + drow)
                caixa = tile_bbox(key)
                clat, clon = caixa.center
                distancia = _distancia_m(lat, lon, clat, clon)
                if distancia > limite + lado * 0.75:
                    continue
                # Um anel mais fino ja cobriu este pedaco de chao?
                if any(
                    _distancia_m(clat, clon, plat, plon) < praio
                    for plat, plon, praio in cobertos
                ):
                    continue
                anterior = escolhidos.get(key)
                if anterior is None or distancia < anterior[0]:
                    escolhidos[key] = (distancia, detalhe)
        cobertos.append((lat, lon, min(limite_anel, alcance)))

    ordenado = sorted(escolhidos.items(), key=lambda item: item[1][0])
    return [(key, detalhe) for key, (_, detalhe) in ordenado]


def _contains(externa: BBox, interna: BBox) -> bool:
    """A caixa maior ja cobre a menor? Evita rebaixar a vista a cada passo."""
    return (
        externa.south <= interna.south
        and externa.west <= interna.west
        and externa.north >= interna.north
        and externa.east >= interna.east
    )


def _distancia_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia plana, boa o bastante na escala de dezenas de km."""
    meio = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * _METERS_PER_DEG_LAT * math.cos(meio)
    dy = (lat2 - lat1) * _METERS_PER_DEG_LAT
    return math.hypot(dx, dy)


class WorldStreamer:
    """Mantem carregado so o pedaco de mundo que a camera esta vendo.

    Uso:

        streamer = WorldStreamer(settings)
        streamer.move(-2.8909, -41.9050, radius_m=3000)
        streamer.wait()          # ou consulte streamer.ready() a cada quadro
        for tile in streamer.loaded():
            ...

    A geracao roda em threads: mover a camera nao trava a interface. O que ja
    esta carregado e reaproveitado, e o excedente sai por distancia.
    """

    def __init__(
        self,
        settings: Optional[GenerationSettings] = None,
        cache=None,
        budget_triangles: int = DEFAULT_BUDGET_TRIANGLES,
        workers: int = 2,
        output_dir: Optional[str | Path] = None,
        progress: Optional[ProgressFn] = None,
    ) -> None:
        self.settings = settings or GenerationSettings()
        self.cache = cache
        self.budget = int(budget_triangles)
        self.workers = max(1, int(workers))
        self.output_dir = Path(output_dir) if output_dir else None
        self._progress = progress

        self._tiles: "OrderedDict[TileKey, Tile]" = OrderedDict()
        self._wanted: list[tuple[TileKey, str]] = []
        self._lock = threading.Lock()
        self._pending: set[TileKey] = set()
        self._threads: list[threading.Thread] = []
        self._center = (0.0, 0.0)
        # Dado bruto da vista inteira, compartilhado por todos os blocos dela.
        self._fetched: Optional[BBox] = None
        self._raw_osm_por_bloco: dict[TileKey, object] = {}
        self._raw_overture = None
        self.stats = {"gerados": 0, "reaproveitados": 0, "descartados": 0, "falhas": 0}

    # ------------------------------------------------------------------ leitura

    def loaded(self) -> list[Tile]:
        """Blocos prontos, do mais perto da camera para o mais longe."""
        with self._lock:
            prontos = [t for t in self._tiles.values() if t.scene is not None]
        lat, lon = self._center
        prontos.sort(key=lambda t: _distancia_m(lat, lon, *t.bbox.center))
        return prontos

    def triangle_count(self) -> int:
        with self._lock:
            return sum(t.triangles for t in self._tiles.values())

    def ready(self) -> bool:
        """Terminou de gerar tudo que a vista pediu?"""
        with self._lock:
            return not self._pending

    def missing(self) -> int:
        with self._lock:
            return len(self._pending)

    # ---------------------------------------------------------------- movimento

    def move(self, lat: float, lon: float, radius_m: float = 3_000.0) -> list[TileKey]:
        """Reposiciona a camera. Devolve os blocos que passaram a faltar."""
        self._center = (lat, lon)
        self._wanted = tiles_for_view(lat, lon, radius_m)
        desejados = {key for key, _ in self._wanted}

        # O gargalo do streaming nao e a malha, e a rede: medido, um bloco frio
        # levava 139 s, dos quais menos de 3 s eram geometria. Uma consulta por
        # bloco repete a mesma vizinhanca dezenas de vezes. Entao a vista inteira
        # e baixada de uma vez, antes, e cada bloco so recorta o que ja esta em
        # maos - a mesma ideia que derrubou a geracao em blocos de 583 s para 14 s.
        self._prefetch(desejados)

        faltando: list[tuple[TileKey, str]] = []
        with self._lock:
            for key, detalhe in self._wanted:
                existente = self._tiles.get(key)
                if existente is not None and existente.detail == detalhe:
                    existente.last_used = time.monotonic()
                    self._tiles.move_to_end(key)
                    self.stats["reaproveitados"] += 1
                    continue
                if key in self._pending:
                    continue
                faltando.append((key, detalhe))
                self._pending.add(key)

            # O que saiu do campo de visao sai da memoria.
            for key in [k for k in self._tiles if k not in desejados]:
                self._tiles.pop(key, None)
                self.stats["descartados"] += 1

        for key, detalhe in faltando:
            self._submit(key, detalhe)
        return [key for key, _ in faltando]

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Bloqueia ate a vista estar completa. Devolve False se estourou o prazo."""
        limite = None if timeout is None else time.monotonic() + timeout
        for thread in list(self._threads):
            restante = None if limite is None else max(limite - time.monotonic(), 0.0)
            thread.join(restante)
        self._threads = [t for t in self._threads if t.is_alive()]
        return self.ready()

    # ------------------------------------------------------------------ interno

    def _prefetch(self, desejados: Iterable[TileKey]) -> None:
        """Baixa OSM e contornos para a vista, em pedacos que o Overpass aceite.

        Uma consulta unica para a vista inteira nao serve: uma vista de 12 km de
        raio tem ~98 km2 e o Overpass recusa acima de 25 km2. Entao a vista e
        cortada em faixas do tamanho maximo aceito, e cada bloco recebe o dado
        bruto da faixa que o contem.
        """
        caixas = {key: tile_bbox(key) for key in desejados}
        if not caixas:
            return
        vista = BBox(
            min(c.south for c in caixas.values()),
            min(c.west for c in caixas.values()),
            max(c.north for c in caixas.values()),
            max(c.east for c in caixas.values()),
        )
        if self._fetched is not None and _contains(self._fetched, vista):
            return

        from . import config
        from .data import download_region

        inicio = time.perf_counter()

        # O agrupamento e por nivel: um bloco de nivel 2 tem 4 km de lado e
        # 16 km2 sozinho, entao dois deles ja estouram o limite. Quantos cabem
        # num download depende do tamanho do bloco daquele nivel.
        grupos: dict[tuple[int, int, int], list[TileKey]] = {}
        for key in caixas:
            area_bloco = (key.size_m / 1000.0) ** 2
            por_lado = max(int(math.sqrt(config.MAX_AREA_KM2 * 0.7 / area_bloco)), 1)
            grupos.setdefault(
                (key.level, key.col // por_lado, key.row // por_lado), []
            ).append(key)

        self._raw_osm_por_bloco = {}
        for membros in grupos.values():
            envolvente = BBox(
                min(caixas[k].south for k in membros),
                min(caixas[k].west for k in membros),
                max(caixas[k].north for k in membros),
                max(caixas[k].east for k in membros),
            )
            try:
                raw = download_region(envolvente, cache=self.cache)
            except Exception as exc:  # noqa: BLE001 - o bloco tenta sozinho depois
                log.warning("OSM do grupo indisponivel: %s", exc)
                continue
            for key in membros:
                self._raw_osm_por_bloco[key] = raw

        self._raw_overture = None
        if self.settings.overture:
            from .data import fetch_overture

            try:
                # O Overture nao tem limite de area por consulta: uma so resolve.
                self._raw_overture = fetch_overture(vista, cache=self.cache)
            except Exception as exc:  # noqa: BLE001
                log.warning("Overture da vista indisponivel: %s", exc)

        self._fetched = vista
        decorrido = time.perf_counter() - inicio
        log.info(
            "Vista de %.1f x %.1f km: %d grupos OSM em %.1fs",
            vista.width_m / 1000, vista.height_m / 1000, len(grupos), decorrido,
        )
        if self._progress:
            self._progress(f"dados da vista em {decorrido:.0f}s", 0.0)

    def _submit(self, key: TileKey, detail: str) -> None:
        # Segura o numero de threads vivas; o resto espera na propria chamada.
        self._threads = [t for t in self._threads if t.is_alive()]
        if len(self._threads) >= self.workers:
            self._build(key, detail)
            return
        thread = threading.Thread(
            target=self._build, args=(key, detail), daemon=True, name=f"tile-{key}"
        )
        self._threads.append(thread)
        thread.start()

    def _build(self, key: TileKey, detail: str) -> None:
        from .pipeline import generate

        caixa = tile_bbox(key)
        inicio = time.perf_counter()
        tile = Tile(key=key, bbox=caixa, detail=detail)

        settings = dataclasses.replace(self.settings, detail=detail, allow_empty=True)
        if detail == "distante":
            # A imagem e o que domina o relogio num bloco distante: um bloco de
            # 4 km no zoom da vista proxima pede centenas de tiles e faz a
            # deteccao de vegetacao varrer 11 megapixels - 140 s medidos, para
            # algo que sera visto a 8 km. Aqui a foto so precisa dar a cor.
            settings = dataclasses.replace(
                settings,
                satellite_zoom=min(settings.satellite_zoom or 15, 15),
                detect_vegetation=False,
                detect_buildings=False,
                shadow_heights=False,
            )
        destino = None
        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            destino = self.output_dir / f"z{key.level}_{key.col}_{key.row}.glb"

        try:
            resultado = generate(
                caixa,
                settings=settings,
                cache=self.cache,
                output=destino,
                raw_osm=self._raw_osm_por_bloco.get(key),
                raw_overture=self._raw_overture,
            )
            tile.scene = resultado.scene
            tile.triangles = resultado.scene.triangle_count
        except Exception as exc:  # noqa: BLE001 - um bloco vazio nao derruba a vista
            tile.error = str(exc)
            log.info("Bloco %s sem cena: %s", key, exc)
        tile.seconds = time.perf_counter() - inicio

        with self._lock:
            self._pending.discard(key)
            if tile.error:
                self.stats["falhas"] += 1
            else:
                self._tiles[key] = tile
                self.stats["gerados"] += 1
            self._evict_locked()

        if self._progress:
            self._progress(
                f"bloco {key} ({detail}) em {tile.seconds:.1f}s", 0.0
            )

    def _evict_locked(self) -> None:
        """Descarta os blocos mais distantes ate caber no orcamento."""
        total = sum(t.triangles for t in self._tiles.values())
        if total <= self.budget:
            return
        lat, lon = self._center
        por_distancia = sorted(
            self._tiles.values(),
            key=lambda t: -_distancia_m(lat, lon, *t.bbox.center),
        )
        for tile in por_distancia:
            if total <= self.budget:
                break
            self._tiles.pop(tile.key, None)
            self.stats["descartados"] += 1
            total -= tile.triangles


def estimate_world(area_km2: float, detail: str = "distante") -> dict[str, float]:
    """Quanto custaria gerar uma area inteira de uma vez.

    Medido em Araioses com o gerador atual (satelite, relevo, vegetacao
    detectada, textura pintada). Serve para dizer com numero o que a intuicao ja
    diz: nao se guarda um pais em malha.
    """
    # triangulos por km2, MB por km2
    CUSTO = {
        "high": (288_500, 8.6),
        "medium": (143_600, 4.7),
        "low": (45_900, 2.1),
        "distante": (32_500, 1.7),
    }
    triangulos, megabytes = CUSTO.get(detail, CUSTO["distante"])
    return {
        "triangulos": triangulos * area_km2,
        "megabytes": megabytes * area_km2,
        "terabytes": megabytes * area_km2 / 1e6,
    }
