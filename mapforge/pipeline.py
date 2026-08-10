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


def polygon_to_local(coords, projection: LocalProjection):
    """Contorno em (lat, lon) -> poligono Shapely em metros locais."""
    from shapely.geometry import Polygon as ShapelyPolygon

    if not coords or len(coords) < 3:
        return None
    points = projection.project_many([(float(a), float(b)) for a, b in coords])
    polygon = ShapelyPolygon(points)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    return polygon if (polygon is not None and not polygon.is_empty) else None


# Lado da celula que decide se um pedaco do mapa ja tem contorno, em metros.
GAP_CELL_M = 220.0


def _fill_only_gaps(detectados, map_data, bbox):
    """Mantem so os retangulos detectados onde nenhuma fonte de contorno chegou.

    A deteccao por imagem e a ultima linha, e o numero justifica o cuidado:
    medida contra o Overture em Araioses, ela tem **49,6% de precisao e 61,4% de
    falso positivo** - areia e solo exposto lidos como telhado. Sobre um bairro
    que o Overture ja mapeou, ela nao acrescenta predio nenhum que faltasse; so
    espalha retangulo claro sobre o chao, que foi exatamente o defeito relatado.

    Onde nao ha fonte alguma, porem, ela continua sendo a diferenca entre uma
    cidade vazia e uma cidade povoada - entao a decisao e por regiao, nao global:
    o mapa e dividido em celulas e a deteccao so vale nas celulas sem contorno.
    """
    if not detectados:
        return []

    existentes = [b for b in map_data.buildings if b.footprint is not None]
    if not existentes:
        return list(detectados)

    half_w, half_h = bbox.width_m / 2.0, bbox.height_m / 2.0

    def celula(x: float, y: float) -> tuple[int, int]:
        return (int((x + half_w) // GAP_CELL_M), int((y + half_h) // GAP_CELL_M))

    ocupadas = set()
    for predio in existentes:
        ponto = predio.footprint.representative_point()
        ocupadas.add(celula(ponto.x, ponto.y))

    aceitos = []
    for poly in detectados:
        ponto = poly.representative_point()
        if celula(ponto.x, ponto.y) not in ocupadas:
            aceitos.append(poly)

    log.info(
        "Deteccao: %d de %d retangulos mantidos (o resto caiu onde ja havia contorno)",
        len(aceitos), len(detectados),
    )
    return aceitos


def load_map(
    bbox: BBox,
    cache: Optional[Cache] = None,
    force_download: bool = False,
    progress: Optional[ProgressFn] = None,
    extra_footprints: bool = False,
    overture: bool = False,
    clip_area=None,
    raw_osm=None,
    raw_overture=None,
) -> tuple[MapData, LocalProjection]:
    """Baixa (ou le do cache) e converte para objetos internos.

    Cidade pequena costuma ter dois ou tres predios desenhados no OSM, e a cena
    sai vazia. Duas fontes abertas completam, nesta ordem:

    - `overture`: conflacao OSM + Microsoft + Esri + Google Open Buildings. E o
      mesmo dado que o Google Maps serve no celular, e cobre onde as outras
      falham (em Araioses-MA: 5.382 predios, contra 15 do OSM).
    - `extra_footprints`: o conjunto da Microsoft, mantido como reserva para
      quando o Overture estiver fora do ar ou o duckdb nao estiver instalado.

    O que ja existe no MapData sempre vence, entao rodar as duas nao duplica.
    """
    # `raw_osm` permite reaproveitar um download maior: na geracao em blocos, a
    # regiao inteira e baixada de uma vez e cada bloco so recorta.
    raw = raw_osm
    if raw is None:
        raw = download_region(
            bbox, cache=cache, force=force_download, progress=_stage(progress, 0.0, 0.45)
        )
    if progress:
        progress("interpretando dados do OSM", 0.5)
    projection = LocalProjection.for_bbox(bbox)
    map_data = parse_osm(raw, bbox, projection, clip_area=clip_area)

    if overture or raw_overture is not None:
        from .data import fetch_overture, merge_into

        try:
            # `raw_overture` reaproveita uma consulta maior, como o `raw_osm`:
            # na geracao em blocos a regiao inteira e consultada de uma vez.
            found = (
                raw_overture
                if raw_overture is not None
                else fetch_overture(
                    bbox, cache=cache, progress=_stage(progress, 0.55, 0.2),
                    force=force_download,
                )
            )
            added = merge_into(map_data, found, projection, source="overture", id_base=-1_000_000)
            if progress:
                progress(f"Overture: +{added} edificios", 0.78)
        except Exception as exc:  # noqa: BLE001 - a cena continua sem ele
            log.warning("Overture indisponivel: %s", exc)
            if progress:
                progress(f"Overture falhou ({exc})", 0.78)

    if extra_footprints:
        from .data import fetch_footprints, merge_into

        try:
            found = fetch_footprints(
                bbox, cache=cache, progress=_stage(progress, 0.78, 0.1), force=force_download
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
    clip_polygon=None,
    raw_osm=None,
    raw_overture=None,
) -> GenerationResult:
    """Pipeline completo: download -> parser -> geracao -> (opcional) exportacao.

    `clip_polygon` e uma lista de (lat, lon) fechando um contorno. Quando
    informado, a cena e recortada por ele em vez do retangulo da bbox - a bbox
    continua valendo para os downloads, que sao sempre retangulares.
    """
    settings = settings or GenerationSettings()

    projection = LocalProjection.for_bbox(bbox)
    clip_area = polygon_to_local(clip_polygon, projection) if clip_polygon else None

    map_data, projection = load_map(
        bbox,
        cache,
        force_download,
        _stage(progress, 0.0, 0.30),
        extra_footprints=settings.extra_footprints,
        overture=settings.overture,
        clip_area=clip_area,
        raw_osm=raw_osm,
        raw_overture=raw_overture,
    )
    if map_data.is_empty() and not settings.allow_empty:
        # No carregamento dinamico isto e normal e nao e erro: um bloco no meio
        # do rio ou da mata nao tem nada mapeado, e ainda assim precisa existir
        # como chao. Quem pede a cena decide se o vazio e falha.
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
            aceitos = _fill_only_gaps(detection.polygons, map_data, bbox)
            start_id = -100_000
            for offset, polygon in enumerate(aceitos):
                map_data.buildings.append(
                    Building(
                        osm_id=start_id - offset,
                        kind=FeatureKind.BUILDING,
                        tags={"source": "deteccao"},
                        footprint=polygon,
                        building_type="yes",
                    )
                )
            descartados = len(detection.polygons) - len(aceitos)
            if progress:
                progress(
                    f"detectados na imagem: +{len(aceitos)}"
                    + (f" ({descartados} descartados onde ja havia contorno)" if descartados else ""),
                    0.47,
                )
        except Exception as exc:  # noqa: BLE001 - a cena segue sem a deteccao
            log.warning("Deteccao de edificios falhou: %s", exc)

    # Vegetacao pela imagem. Ao contrario do telhado, aqui a deteccao ganha de
    # longe: o OSM quase nunca desenha mata em cidade pequena (Araioses tem
    # zero), e o excesso de verde e um sinal robusto. Roda depois dos edificios
    # porque usa os contornos para nao plantar arvore em cima de telhado.
    if imagery is not None and settings.detect_vegetation and settings.vegetation:
        from .core.features import FeatureKind, Forest, Park
        from .imagery.canopy import detect_canopy

        if progress:
            progress("procurando vegetacao na imagem", 0.48)
        try:
            copa = detect_canopy(imagery, map_data)
            base_id = -200_000
            for offset, poly in enumerate(copa.canopy_polygons):
                map_data.forests.append(
                    Forest(
                        osm_id=base_id - offset,
                        kind=FeatureKind.FOREST,
                        tags={"source": "deteccao", "natural": "wood"},
                        geometry=poly,
                    )
                )
            base_id = -300_000
            for offset, poly in enumerate(copa.grass_polygons):
                map_data.parks.append(
                    Park(
                        osm_id=base_id - offset,
                        kind=FeatureKind.PARK,
                        tags={"source": "deteccao", "landuse": "grass"},
                        geometry=poly,
                    )
                )
            if progress:
                progress(f"vegetacao detectada: {copa.summary()}", 0.50)
        except Exception as exc:  # noqa: BLE001 - a cena segue sem a deteccao
            log.warning("Deteccao de vegetacao falhou: %s", exc)

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
        clip=clip_area,
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
