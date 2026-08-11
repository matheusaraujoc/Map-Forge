"""Geracao em blocos, para regioes grandes demais para uma cena so.

O limite de 25 km2 nao e capricho: uma consulta Overpass daquele tamanho e
recusada, a imagem de satelite estoura o orcamento de tiles, e a malha nao cabe
na memoria. Uma cidade inteira como Parnaiba passa longe disso.

A saida e cortar a regiao em blocos, gerar um de cada vez e gravar ao lado um
*manifesto*: a lista de blocos com o deslocamento de cada um em metros. O
arquivo pesado vira N arquivos que a engine posiciona por translacao, e o
manifesto guarda tambem a seed e as configuracoes - entao a regiao inteira e
reconstruivel a partir de parametros, sem guardar malha nenhuma.

Cada bloco tem o seu proprio centro local, entao as coordenadas ficam pequenas
e a precisao de float nao sofre.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .core.geo import BBox, LocalProjection
from .data import Cache
from .generation import GenerationSettings

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]

# Lado padrao do bloco. Abaixo disto o custo fixo por bloco domina; acima, a
# consulta Overpass comeca a ser recusada.
DEFAULT_TILE_KM = 1.5


@dataclass
class TileResult:
    row: int
    col: int
    bbox: list[float]  # sul, oeste, norte, leste
    offset: list[float]  # deslocamento do centro do bloco, em metros (leste, norte)
    file: Optional[str] = None
    triangles: int = 0
    buildings: int = 0
    seconds: float = 0.0
    error: str = ""


@dataclass
class TiledResult:
    name: str
    region: list[float]
    tile_km: float
    grid: list[int]  # colunas, linhas
    tiles: list[TileResult] = field(default_factory=list)
    manifest: Optional[Path] = None
    seconds: float = 0.0

    @property
    def ok(self) -> list[TileResult]:
        return [t for t in self.tiles if t.file]

    @property
    def triangles(self) -> int:
        return sum(t.triangles for t in self.tiles)

    @property
    def buildings(self) -> int:
        return sum(t.buildings for t in self.tiles)


def plan_tiles(region: BBox, tile_km: float = DEFAULT_TILE_KM):
    """Divide a regiao numa grade de blocos aproximadamente quadrados."""
    tile_m = max(tile_km, 0.2) * 1000.0
    cols = max(int(math.ceil(region.width_m / tile_m)), 1)
    rows = max(int(math.ceil(region.height_m / tile_m)), 1)

    d_lon = (region.east - region.west) / cols
    d_lat = (region.north - region.south) / rows

    plano = []
    projection = LocalProjection.for_bbox(region)
    for row in range(rows):
        for col in range(cols):
            south = region.south + d_lat * row
            north = south + d_lat
            west = region.west + d_lon * col
            east = west + d_lon
            bbox = BBox(south, west, north, east)
            # Deslocamento do centro do bloco em relacao ao centro da regiao.
            center_lat, center_lon = bbox.center
            offset = projection.project(center_lat, center_lon)
            plano.append(
                TileResult(
                    row=row,
                    col=col,
                    bbox=[south, west, north, east],
                    offset=[round(offset[0], 3), round(offset[1], 3)],
                )
            )
    return plano, cols, rows


def _recortar(contornos, bbox: BBox):
    """Fatia a consulta da regiao inteira no que interessa a um bloco.

    Sobra uma margem porque o predio que cruza a divisa precisa aparecer nos
    dois blocos; quem recorta de fato e o `merge_into`.
    """
    if contornos is None:
        return None
    margem = 0.001  # ~110 m
    oeste, leste = bbox.west - margem, bbox.east + margem
    sul, norte = bbox.south - margem, bbox.north + margem
    dentro = []
    for item in contornos:
        minx, miny, maxx, maxy = item["polygon"].bounds
        if maxx >= oeste and minx <= leste and maxy >= sul and miny <= norte:
            dentro.append(item)
    return dentro


def _group_tiles(plano, region: BBox, cols: int, rows: int):
    """Agrupa blocos vizinhos em super-blocos para baixar de uma vez so.

    O tamanho do grupo respeita o limite de area de uma consulta Overpass; o
    parser depois recorta cada bloco a partir do mesmo dado bruto.
    """
    from . import config

    tile_area = region.area_km2 / max(cols * rows, 1)
    # Quantos blocos cabem num download, com folga.
    por_lado = max(int(math.sqrt(max(config.MAX_AREA_KM2 * 0.7 / max(tile_area, 1e-6), 1.0))), 1)

    grupos: dict[tuple[int, int], list] = {}
    for tile in plano:
        chave = (tile.row // por_lado, tile.col // por_lado)
        grupos.setdefault(chave, []).append(tile)
    return grupos


def _share_elevation_base(
    settings: GenerationSettings, region: BBox, cache, progress: Optional[ProgressFn]
) -> GenerationSettings:
    """Fixa a mesma altitude de referencia (z = 0) para a regiao inteira.

    Cada `TerrainField` normaliza as alturas subtraindo o **minimo do proprio
    recorte**. Numa cena unica isso e certo e mantem as coordenadas pequenas. Na
    geracao em blocos e a origem de um defeito que so aparece na montagem: dois
    blocos vizinhos tem minimos diferentes, entao o mesmo ponto do terreno recebe
    z diferente nos dois, o relevo salta na divisa e o rio que atravessa a emenda
    aparece em duas alturas.

    A correcao e medir o minimo uma vez, na regiao toda, e passar o mesmo valor a
    todos os blocos. Se a medicao falhar, cada bloco volta a usar o proprio
    minimo - torto, mas gerando.
    """
    if not settings.elevation or settings.elevation_base is not None:
        return settings

    from dataclasses import replace

    from .imagery.elevation import fetch_elevation

    try:
        if progress:
            progress("medindo a referencia de altura da regiao inteira", 0.0)
        grade = fetch_elevation(
            region, cache=cache, zoom=settings.elevation_zoom, smooth=settings.elevation_smooth
        )
        if cache is not None:
            cache.flush_tiles()
        base = float(grade.min)
    except Exception as exc:  # noqa: BLE001 - cada bloco usa o proprio minimo
        log.warning(
            "Referencia de altura da regiao indisponivel (%s): os blocos podem "
            "nao casar na divisa",
            exc,
        )
        return settings

    log.info("Referencia de altura compartilhada entre os blocos: %.1f m", base)
    return replace(settings, elevation_base=base)


def _download_groups(grupos, cache, progress: Optional[ProgressFn]):
    """Baixa o OSM de cada super-bloco. Devolve bloco -> dado bruto."""
    from .data import download_region

    raw_por_tile: dict[tuple[int, int], dict] = {}
    total = len(grupos)
    for n, (chave, tiles) in enumerate(sorted(grupos.items())):
        envolvente = BBox(
            min(t.bbox[0] for t in tiles),
            min(t.bbox[1] for t in tiles),
            max(t.bbox[2] for t in tiles),
            max(t.bbox[3] for t in tiles),
        )
        if progress:
            progress(
                f"baixando OSM do grupo {n + 1}/{total} "
                f"({envolvente.area_km2:.1f} km2, {len(tiles)} blocos)",
                0.0,
            )
        try:
            raw = download_region(envolvente, cache=cache)
        except Exception as exc:  # noqa: BLE001 - cada bloco tenta sozinho depois
            log.warning("Download do grupo %s falhou: %s", chave, exc)
            continue
        for tile in tiles:
            raw_por_tile[(tile.row, tile.col)] = raw
    return raw_por_tile


def generate_tiled(
    region: BBox,
    settings: Optional[GenerationSettings] = None,
    output_dir: Optional[Path] = None,
    name: str = "regiao",
    tile_km: float = DEFAULT_TILE_KM,
    cache: Optional[Cache] = None,
    progress: Optional[ProgressFn] = None,
    extension: str = ".glb",
    skip_existing: bool = False,
) -> TiledResult:
    """Gera a regiao inteira em blocos e grava o manifesto de montagem."""
    from .pipeline import generate

    settings = settings or GenerationSettings()
    output_dir = Path(output_dir or Path("output") / name)
    output_dir.mkdir(parents=True, exist_ok=True)

    plano, cols, rows = plan_tiles(region, tile_km)
    resultado = TiledResult(
        name=name,
        region=[region.south, region.west, region.north, region.east],
        tile_km=tile_km,
        grid=[cols, rows],
        tiles=plano,
    )

    owns_cache = cache is None
    cache = cache or Cache()
    started = time.perf_counter()

    try:
        # A referencia de altura tem de ser a mesma nos blocos todos, senao a
        # divisa vira degrau e um rio muda de nivel no meio.
        settings = _share_elevation_base(settings, region, cache, progress)

        # Um download por super-bloco, nao por bloco. Sao os downloads que
        # dominam o relogio: 6 blocos de 1,5 km levavam 583 s, quase tudo
        # esperando o Overpass responder seis vezes a mesma vizinhanca.
        grupos = _group_tiles(plano, region, cols, rows)
        raw_por_tile = _download_groups(grupos, cache, progress)

        # O mesmo vale para o Overture: uma consulta para a regiao inteira, e
        # cada bloco so filtra. Consultar por bloco multiplicaria a espera pelo
        # numero de blocos.
        contornos = None
        if getattr(settings, "overture", False):
            from .data import fetch_overture

            try:
                if progress:
                    progress("consultando o Overture para a regiao inteira", 0.0)
                contornos = fetch_overture(region, cache=cache, progress=progress)
            except Exception as exc:  # noqa: BLE001 - a cena continua sem ele
                log.warning("Overture indisponivel: %s", exc)
                # Lista vazia, e nao None: evita que cada bloco tente de novo.
                contornos = []

        for i, tile in enumerate(plano):
            rotulo = f"bloco {i + 1}/{len(plano)} (linha {tile.row}, coluna {tile.col})"
            if progress:
                progress(rotulo, i / len(plano))

            destino = output_dir / f"{name}_r{tile.row:02d}c{tile.col:02d}{extension}"
            if skip_existing and destino.exists():
                tile.file = destino.name
                log.info("%s ja existe, pulando", destino.name)
                continue

            bbox = BBox(*tile.bbox)
            t0 = time.perf_counter()
            try:
                saida = generate(
                    bbox,
                    settings=settings,
                    output=destino,
                    cache=cache,
                    raw_osm=raw_por_tile.get((tile.row, tile.col)),
                    raw_overture=_recortar(contornos, bbox),
                    progress=(
                        (lambda m, f, base=i: progress(f"{rotulo}: {m}", (base + f) / len(plano)))
                        if progress
                        else None
                    ),
                )
                tile.file = destino.name
                tile.triangles = saida.scene.triangle_count
                tile.buildings = len(saida.map_data.buildings)
            except Exception as exc:  # noqa: BLE001 - um bloco vazio nao para a regiao
                tile.error = str(exc)
                log.warning("Bloco r%dc%d falhou: %s", tile.row, tile.col, exc)
            tile.seconds = round(time.perf_counter() - t0, 2)

        resultado.seconds = round(time.perf_counter() - started, 1)
        resultado.manifest = _write_manifest(resultado, settings, output_dir, name)
    finally:
        if owns_cache:
            cache.close()

    if progress:
        progress(f"{len(resultado.ok)} de {len(plano)} blocos gerados", 1.0)
    return resultado


def _write_manifest(
    resultado: TiledResult, settings: GenerationSettings, output_dir: Path, name: str
) -> Path:
    """Grava a informacao de montagem: onde cada bloco entra, e com que parametros.

    E o arquivo que permite reconstruir a regiao inteira sem guardar malha: os
    blocos podem ser apagados e regerados identicos a partir daqui.
    """
    dados = {
        "name": resultado.name,
        "region": resultado.region,
        "grid": {"cols": resultado.grid[0], "rows": resultado.grid[1]},
        "tile_km": resultado.tile_km,
        "units": "metros; deslocamento e (leste, norte) do centro da regiao",
        "axis": "glTF Y-up: aplique (x, z) = (leste, -norte)",
        "settings": settings.to_dict(),
        "totals": {
            "tiles": len(resultado.tiles),
            "generated": len(resultado.ok),
            "triangles": resultado.triangles,
            "buildings": resultado.buildings,
            "seconds": resultado.seconds,
        },
        "tiles": [asdict(t) for t in resultado.tiles],
    }
    caminho = output_dir / f"{name}_manifesto.json"
    caminho.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Manifesto: %s", caminho)
    return caminho
