"""Contexto de geracao: seed, estilo, nivel de detalhe e RNG deterministico."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from ..core.geo import BBox, LocalProjection
from ..styles import Palette, Style, get_style

DETAIL_LEVELS = ("distante", "low", "medium", "high")

# Multiplicadores por nivel de detalhe.
_DETAIL_PRESETS: dict[str, dict[str, Any]] = {
    # Nivel mais barato, para bloco distante no carregamento dinamico: sem
    # calcada elevada, sem mobiliario, predio como volume.
    "distante": {
        "buffer_segments": 1,
        "windows": False,
        "tree_spacing": 34.0,
        "simplify": 2.5,
        "min_building_area": 40.0,
        "roof_detail": False,
        "road_markings": False,
        "building_detail": False,
        "street_furniture": False,
        "curb_walls": False,
    },
    "low": {
        "buffer_segments": 1,
        "windows": False,
        "tree_spacing": 26.0,
        "simplify": 1.2,
        "min_building_area": 25.0,
        "roof_detail": False,
        "road_markings": False,
        "building_detail": False,
        "street_furniture": False,
        # O degrau do meio-fio tem 12 cm e some a poucos metros; as paredes
        # verticais dele custam mais que todos os predios juntos.
        "curb_walls": False,
    },
    "medium": {
        "buffer_segments": 2,
        "windows": True,
        "tree_spacing": 16.0,
        "simplify": 0.4,
        "min_building_area": 10.0,
        "roof_detail": True,
        "road_markings": True,
        "building_detail": True,
        "street_furniture": True,
        "curb_walls": False,
    },
    "high": {
        "buffer_segments": 4,
        "windows": True,
        "tree_spacing": 11.0,
        "simplify": 0.0,
        "min_building_area": 4.0,
        "roof_detail": True,
        "road_markings": True,
        "building_detail": True,
        "street_furniture": True,
        "curb_walls": True,
    },
}


@dataclass
class GenerationSettings:
    """Tudo que define o resultado. Salvo no projeto no lugar da malha."""

    seed: int = 1
    style: str = "lowpoly"
    detail: str = "medium"
    # None = deduzido da latitude da regiao.
    region: Optional[str] = None
    # Forca o porte do assentamento ('povoado', 'pequena', 'media', 'grande')
    # em vez de medi-lo nos contornos. None = medir.
    urban_scale: Optional[str] = None
    # Regiao sem nada mapeado gera so o terreno em vez de levantar erro. E o que
    # o carregamento dinamico precisa: bloco no meio do rio tambem existe.
    allow_empty: bool = False

    terrain: bool = True
    roads: bool = True
    buildings: bool = True
    water: bool = True
    vegetation: bool = True
    sidewalks: bool = True
    structures: bool = True  # torres, silos, postes mapeados no OSM
    street_lamps: bool = True  # iluminacao publica, deduzida do tracado
    windows: Optional[bool] = None  # None = usa o padrao do estilo/detalhe

    tree_density: float = 1.0
    max_trees: int = 40_000
    building_height_scale: float = 1.0

    # --- imagem de satelite ---
    satellite: bool = False
    satellite_provider: str = "esri"
    satellite_zoom: Optional[int] = None
    # A imagem serve como FONTE DE COR, nao como textura: as superficies continuam
    # chapadas, so que tingidas pelo que existe de verdade no lugar. Colar a foto
    # no terreno (ground_texture) briga com a leitura low-poly do resto da cena.
    ground_texture: bool = False
    satellite_landuse: bool = True  # pintar parques/bosques (com cor amostrada)
    roof_blend: float = 0.75  # 0 = so estilo, 1 = so foto
    area_blend: float = 0.55  # idem para terreno, parques, bosques
    roof_palette_size: int = 24
    texture_brightness: float = 1.04
    texture_saturation: float = 0.92
    # Como a textura do terreno e feita quando `ground_texture` esta ligado:
    # 'pintada' repinta cada classe de cobertura com a cor medida na foto;
    # 'foto' cola a foto crua, que e o modo antigo.
    ground_texture_mode: str = "pintada"
    # Quanto da variacao interna de cada classe sobrevive na repintura.
    texture_variation: float = 0.55
    # Resolucao alvo da textura pintada, em metros por pixel. A foto tem ~1,2
    # m/px e esta borrada; a banda que falta e preenchida com o grao procedural
    # de cada cobertura. 0 desliga o detalhe e mantem so a cor macro.
    texture_detail_m: float = 0.35
    # Multiplicador do grao. 0 = so a cor macro, 1 = calibrado, 2 = exagerado.
    texture_grain: float = 1.0

    # --- fontes extras de edificios ---
    # Overture Maps: conflacao OSM + Microsoft + Esri + Google Open Buildings,
    # a mesma base que o Google Maps serve no celular. Melhor fonte disponivel
    # para cidade pequena brasileira; exige duckdb.
    overture: bool = False
    # Completa o OSM com os contornos abertos da Microsoft (ODbL), extraidos de
    # imagem de satelite. Reserva para quando o Overture nao estiver acessivel.
    extra_footprints: bool = False
    shadow_heights: bool = False  # estima altura pela sombra na imagem
    # Ultima linha: detecta telhados na propria imagem onde nenhuma fonte de
    # contorno cobre. Exige satellite=True.
    detect_buildings: bool = False

    # --- vegetacao ---
    # Encontra mata e gramado na foto. Diferente da deteccao de telhado, aqui o
    # sinal e confiavel e nao ha fonte pronta equivalente: o OSM quase nunca
    # desenha mata em cidade pequena. Exige satellite=True.
    detect_vegetation: bool = False
    # O miolo das matas grandes vira uma superficie ondulada unica em vez de
    # arvores individuais.
    #
    # **Desligado por padrao.** A economia media (10% da cena) nao pagou os
    # defeitos: a cor por celula de 8 m produzia um xadrez verde que nao existe
    # em mata nenhuma, o afunilamento da borda dobrava a superficie e abria
    # buracos escuros, e o miolo ficava vazio justamente onde ha arvore de
    # verdade. Arvore individual com o prototipo barato custa 10% a mais e nao
    # tem nenhum desses problemas.
    canopy_shell: bool = False
    # Redesenha a textura do chao com pincel por cobertura em vez de so modular
    # ruido: copa vira disco de copa, areia vira granulado, capim vira traco.
    # As cores saem da propria foto; o que muda e a *estrutura* do desenho.
    redraw_ground: bool = False

    # --- relevo ---
    elevation: bool = False
    elevation_zoom: Optional[int] = None
    elevation_exaggeration: float = 1.0
    elevation_smooth: int = 1
    # Altitude real que corresponde a z = 0. None = o minimo do proprio bloco.
    # Na geracao em blocos ela e fixada para a regiao inteira: sem isso cada
    # bloco tem a sua propria referencia de altura e o relevo nao casa na divisa.
    elevation_base: Optional[float] = None

    # --- fisica ---
    # Malha de colisao em formas simples (caixa por predio, cilindro por arvore,
    # campo de altura para o chao) em nos proprios, mais um JSON com as formas
    # analiticas. Desligada por padrao: o mapa e so o visual ate alguem pedir.
    colliders: bool = False
    # Convencao de nome que a engine reconhece na importacao.
    collider_naming: str = "godot"

    # --- diagnostico ---
    # Anota qual gerador produziu cada lote de triangulos. E o que responde
    # "de onde saiu essa geometria?" sem adivinhacao. Custa uma leitura de pilha
    # por lote, entao fica desligado por padrao.
    diagnose: bool = False

    def __post_init__(self) -> None:
        if self.detail not in DETAIL_LEVELS:
            raise ValueError(f"detalhe invalido: {self.detail!r} (use {DETAIL_LEVELS})")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GenerationSettings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


ProgressFn = Callable[[str, float], None]


class GenerationContext:
    """Estado compartilhado entre os geradores."""

    def __init__(
        self,
        bbox: BBox,
        settings: Optional[GenerationSettings] = None,
        projection: Optional[LocalProjection] = None,
        progress: Optional[ProgressFn] = None,
        imagery=None,
        terrain=None,
        clip=None,
    ):
        self.bbox = bbox
        self.settings = settings or GenerationSettings()
        self.projection = projection or LocalProjection.for_bbox(bbox)
        self.style: Style = get_style(self.settings.style)
        self.palette: Palette = self.style.palette()

        # Perfil regional pela latitude do centro (ou forcado nas configuracoes):
        # e o que decide chamine x caixa d'agua, e o vies de telhado e vegetacao.
        from .region import get_profile, profile_for_latitude

        self.region = (
            get_profile(self.settings.region)
            if self.settings.region
            else profile_for_latitude(bbox.center[0])
        )
        self.detail = dict(_DETAIL_PRESETS[self.settings.detail])
        self._progress = progress

        # Porte do assentamento. Medido nos contornos em `build_scene`, quando o
        # MapData ja existe; e o que impede um galpao de povoado virar torre.
        self.urban = None

        # Imagem de satelite georreferenciada (GeoImage) e o que dela deriva.
        self.imagery = imagery
        # MapData da cena, preenchido em `build_scene`: o classificador de
        # cobertura usa o mapa vetorial para saber onde ha via e agua.
        self.map_data = None
        # Resultado da textura pintada, quando ela e gerada.
        self.painted_ground = None
        self.roof_materials: dict[int, object] = {}
        self.area_materials: dict[str, dict[int, object]] = {}
        # Cor real de cada corpo d'agua: no Brasil o rio nunca e "azul".
        self.water_materials: dict[int, object] = {}
        # Cor real de cada tipo de pavimento, por chave de superficie.
        self.road_materials: dict[str, object] = {}
        self.ground_material = None
        # Alturas medidas pela sombra na imagem (osm_id -> metros).
        self.shadow_heights: dict[int, float] = {}

        # Campo de altura (TerrainField) ou None quando o terreno e plano.
        self.terrain = terrain
        # Poligono de recorte em metros locais, quando a regiao foi desenhada a
        # mao em vez de escolhida como retangulo. None = a bbox inteira.
        self.clip = clip

        # --- fisica ---
        # Os geradores anotam aqui a forma de colisao de cada objeto enquanto
        # constroem o visual. E o que garante que o colisor case com o que se ve:
        # a caixa do predio usa a altura que o gerador de fato escolheu, e nao
        # uma segunda estimativa que poderia divergir.
        # (contorno, z da base, z do topo da parede)
        self.collider_boxes: list = []
        # (x, y, z da base, altura, raio)
        self.collider_cylinders: list = []
        self.collider_set = None

        # Procedencia da geometria, quando o diagnostico esta ligado.
        self.geometry_log = None
        if self.settings.diagnose:
            from ..diagnostics import GeometryLog

            self.geometry_log = GeometryLog()

        if self.settings.windows is not None:
            self.detail["windows"] = self.settings.windows
        self.detail["windows"] = bool(self.detail["windows"] and self.style.windows)

    # --------------------------------------------------------------- helpers

    def ground_at(self, x: float, y: float) -> float:
        """Altura do chao num ponto. Zero quando nao ha relevo."""
        return 0.0 if self.terrain is None else self.terrain.at(x, y)

    @property
    def draped(self) -> bool:
        return self.terrain is not None

    def rng(self, *ids: int) -> np.random.Generator:
        """RNG estavel para um objeto: o mesmo id sempre gera o mesmo resultado."""
        value = np.uint64(self.settings.seed & 0xFFFFFFFF)
        for item in ids:
            value = np.uint64(
                (int(value) * 0x100000001B3 ^ (int(item) & 0xFFFFFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF
            )
        return np.random.default_rng(int(value))

    def report(self, message: str, fraction: float) -> None:
        if self._progress:
            self._progress(message, max(0.0, min(1.0, fraction)))

    @property
    def half_size(self) -> tuple[float, float]:
        return (self.bbox.width_m / 2.0, self.bbox.height_m / 2.0)

    def simplify(self, geom, factor: float = 1.0):
        """Simplifica geometria conforme o nivel de detalhe (reduz triangulos).

        `factor` ajusta a tolerancia por camada: as vias usam um valor bem menor
        porque a tolerancia cheia apagaria os arcos das esquinas suavizadas.
        """
        tolerance = self.detail["simplify"] * factor
        if tolerance <= 0 or geom is None or geom.is_empty:
            return geom
        try:
            simplified = geom.simplify(tolerance, preserve_topology=True)
        except Exception:  # noqa: BLE001 - mantem o original em caso de falha
            return geom
        return geom if simplified.is_empty else simplified


