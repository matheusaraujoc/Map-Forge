"""API de alto nivel: da regiao ao arquivo exportado."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .core.features import MapData
from .core.geo import BBox, LocalProjection
from .core.mesh import Scene
from .data import Cache, download_region, parse_osm
from .export import export_scene
from .generation import GenerationContext, GenerationSettings, build_scene

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]


@dataclass
class GenerationResult:
    bbox: BBox
    map_data: MapData
    scene: Scene
    settings: GenerationSettings
    output: Optional[Path] = None
    imagery: object | None = None  # GeoImage, quando o satelite esta ligado


def _stage(progress: Optional[ProgressFn], start: float, span: float) -> ProgressFn:
    """Mapeia o progresso de uma etapa para uma faixa do progresso total."""

    def inner(message: str, fraction: float) -> None:
        if progress:
            progress(message, start + span * max(0.0, min(1.0, fraction)))

    return inner


def load_map(
    bbox: BBox,
    cache: Optional[Cache] = None,
    force_download: bool = False,
    progress: Optional[ProgressFn] = None,
    extra_footprints: bool = False,
) -> tuple[MapData, LocalProjection]:
    """Baixa (ou le do cache) e converte para objetos internos.

    Com `extra_footprints`, completa os edificios do OSM com os contornos
    abertos da Microsoft - o que salva cidade pequena, onde o OSM costuma ter
    dois ou tres predios desenhados.
    """
    raw = download_region(bbox, cache=cache, force=force_download, progress=_stage(progress, 0.0, 0.45))
    if progress:
        progress("interpretando dados do OSM", 0.5)
    projection = LocalProjection.for_bbox(bbox)
    map_data = parse_osm(raw, bbox, projection)

    if extra_footprints:
        from .data import fetch_footprints, merge_into

        try:
            found = fetch_footprints(
                bbox, cache=cache, progress=_stage(progress, 0.55, 0.3), force=force_download
            )
            added = merge_into(map_data, found, projection)
            if progress:
                progress(f"contornos extras: +{added} edificios", 0.88)
        except Exception as exc:  # noqa: BLE001 - a cena continua so com o OSM
            log.warning("Contornos extras indisponiveis: %s", exc)
            if progress:
                progress(f"contornos extras falharam ({exc})", 0.88)

    if progress:
        counts = ", ".join(f"{k}={v}" for k, v in map_data.summary().items() if v)
        progress(f"parser: {counts or 'nada encontrado'}", 0.95)
    return map_data, projection


def generate(
    bbox: BBox,
    settings: Optional[GenerationSettings] = None,
    output: Optional[str | Path] = None,
    cache: Optional[Cache] = None,
    force_download: bool = False,
    progress: Optional[ProgressFn] = None,
) -> GenerationResult:
    """Pipeline completo: download -> parser -> geracao -> (opcional) exportacao."""
    settings = settings or GenerationSettings()

    map_data, projection = load_map(
        bbox,
        cache,
        force_download,
        _stage(progress, 0.0, 0.30),
        extra_footprints=settings.extra_footprints,
    )
    if map_data.is_empty():
        raise RuntimeError(
            "nenhum dado encontrado nessa regiao - confira a bbox (sul,oeste,norte,leste)"
        )

    imagery = None
    if settings.satellite:
        from .imagery import fetch_imagery

        imagery = fetch_imagery(
            bbox,
            provider_name=settings.satellite_provider,
            cache=cache,
            zoom=settings.satellite_zoom,
            progress=_stage(progress, 0.30, 0.12),
            force=force_download,
        )
        if cache is not None:
            cache.flush_tiles()

    # Ultima fonte de edificios: detectar na imagem. So depois do OSM e dos
    # contornos abertos, e so onde eles nao chegaram.
    detection = None
    if imagery is not None and settings.detect_buildings and settings.buildings:
        from .core.features import Building, FeatureKind
        from .imagery.detect import detect_buildings

        if progress:
            progress("procurando telhados na imagem", 0.44)
        try:
            detection = detect_buildings(imagery, map_data)
            start_id = -100_000
            for offset, polygon in enumerate(detection.polygons):
                map_data.buildings.append(
                    Building(
                        osm_id=start_id - offset,
                        kind=FeatureKind.BUILDING,
                        tags={"source": "deteccao"},
                        footprint=polygon,
                        building_type="yes",
                    )
                )
            if progress:
                progress(f"detectados na imagem: +{len(detection.polygons)}", 0.47)
        except Exception as exc:  # noqa: BLE001 - a cena segue sem a deteccao
            log.warning("Deteccao de edificios falhou: %s", exc)

    terrain = None
    elevation_grid = None
    if settings.elevation:
        from .generation.terrain import GRID_STEP, TerrainField
        from .imagery.elevation import fetch_elevation

        elevation_grid = fetch_elevation(
            bbox,
            cache=cache,
            zoom=settings.elevation_zoom,
            progress=_stage(progress, 0.42, 0.10),
            force=force_download,
            smooth=settings.elevation_smooth,
        )
        if cache is not None:
            cache.flush_tiles()
        terrain = TerrainField.from_grid(
            bbox,
            elevation_grid,
            step=GRID_STEP[settings.detail],
            exaggeration=settings.elevation_exaggeration,
        )

    ctx = GenerationContext(
        bbox=bbox,
        settings=settings,
        projection=projection,
        progress=_stage(progress, 0.52, 0.36),
        imagery=imagery,
        terrain=terrain,
    )
    scene = build_scene(map_data, ctx)
    if imagery is not None:
        scene.metadata["imagery"] = {
            "provider": imagery.provider,
            "zoom": imagery.zoom,
            "size_px": list(imagery.size),
            "meters_per_pixel": round(imagery.meters_per_pixel, 3),
            "attribution": imagery.attribution,
        }

    if detection is not None:
        scene.metadata["detection"] = detection.summary()

    if elevation_grid is not None:
        scene.metadata["elevation"] = {
            "provider": elevation_grid.provider,
            "zoom": elevation_grid.zoom,
            "min_m": round(elevation_grid.min, 1),
            "max_m": round(elevation_grid.max, 1),
            "meters_per_pixel": round(elevation_grid.meters_per_pixel, 2),
            "attribution": elevation_grid.attribution,
        }

    result = GenerationResult(
        bbox=bbox, map_data=map_data, scene=scene, settings=settings, imagery=imagery
    )

    if output:
        if progress:
            progress("exportando", 0.9)
        result.output = export_scene(scene, output)
        if progress:
            progress(f"salvo em {result.output}", 1.0)

    return result
