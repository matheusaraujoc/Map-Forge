"""Contexto de geracao: seed, estilo, nivel de detalhe e RNG deterministico."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from ..core.geo import BBox, LocalProjection
from ..styles import Palette, Style, get_style

DETAIL_LEVELS = ("low", "medium", "high")

# Multiplicadores por nivel de detalhe.
_DETAIL_PRESETS: dict[str, dict[str, Any]] = {
    "low": {
        "buffer_segments": 1,
        "windows": False,
        "tree_spacing": 26.0,
        "simplify": 1.2,
        "min_building_area": 25.0,
        "roof_detail": False,
        "road_markings": False,
        "building_detail": False,
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
    },
}


@dataclass
class GenerationSettings:
    """Tudo que define o resultado. Salvo no projeto no lugar da malha."""

    seed: int = 1
    style: str = "lowpoly"
    detail: str = "medium"

    terrain: bool = True
    roads: bool = True
    buildings: bool = True
    water: bool = True
    vegetation: bool = True
    sidewalks: bool = True
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

    # --- fontes extras de edificios ---
    # Completa o OSM com os contornos abertos da Microsoft (ODbL), extraidos de
    # imagem de satelite. E o que enche a cidade pequena.
    extra_footprints: bool = False
    shadow_heights: bool = False  # estima altura pela sombra na imagem
    # Ultima linha: detecta telhados na propria imagem onde nenhuma fonte de
    # contorno cobre. Exige satellite=True.
    detect_buildings: bool = False

    # --- relevo ---
    elevation: bool = False
    elevation_zoom: Optional[int] = None
    elevation_exaggeration: float = 1.0
    elevation_smooth: int = 1

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
    ):
        self.bbox = bbox
        self.settings = settings or GenerationSettings()
        self.projection = projection or LocalProjection.for_bbox(bbox)
        self.style: Style = get_style(self.settings.style)
        self.palette: Palette = self.style.palette()
        self.detail = dict(_DETAIL_PRESETS[self.settings.detail])
        self._progress = progress

        # Imagem de satelite georreferenciada (GeoImage) e o que dela deriva.
        self.imagery = imagery
        self.roof_materials: dict[int, object] = {}
        self.area_materials: dict[str, dict[int, object]] = {}
        self.ground_material = None
        # Alturas medidas pela sombra na imagem (osm_id -> metros).
        self.shadow_heights: dict[int, float] = {}

        # Campo de altura (TerrainField) ou None quando o terreno e plano.
        self.terrain = terrain

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

