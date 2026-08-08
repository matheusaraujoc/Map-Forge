"""Orquestrador: MapData + configuracoes -> Scene pronta para renderizar/exportar."""

from __future__ import annotations

import logging
import time

from shapely.ops import unary_union

from ..core.features import MapData
from ..core.mesh import MeshBuilder, Scene
from .buildings import generate_buildings
from .coloring import build_area_materials, build_ground_color, build_roof_materials
from .context import GenerationContext
from .roads import generate_railways, generate_roads
from .terrain import generate_landuse, generate_terrain
from .vegetation import generate_vegetation
from .water import generate_water

log = logging.getLogger(__name__)


def build_scene(map_data: MapData, ctx: GenerationContext) -> Scene:
    """Executa o pipeline de geracao na ordem em que as camadas se empilham."""
    started = time.perf_counter()
    builder = MeshBuilder(terrain=ctx.terrain)
    stats: dict[str, int] = {}

    if ctx.imagery is not None:
        ctx.report("amostrando cores reais", 0.03)
        try:
            if ctx.settings.buildings:
                ctx.roof_materials = build_roof_materials(ctx, map_data.buildings)
            ctx.area_materials = build_area_materials(ctx, map_data)
            ctx.ground_material = build_ground_color(ctx)
        except Exception as exc:  # noqa: BLE001 - segue com a paleta do estilo
            log.warning("Amostragem de cores falhou: %s", exc)

        if ctx.settings.shadow_heights and ctx.settings.buildings:
            ctx.report("medindo alturas pela sombra", 0.04)
            try:
                from ..imagery.shadows import estimate_heights

                known = {
                    b.osm_id: b.height
                    for b in map_data.buildings
                    if b.height and 3.0 < b.height < 250.0
                }
                result = estimate_heights(ctx.imagery, map_data.buildings, known)
                # Sem predios de referencia a escala e um chute sobre a elevacao
                # solar; aplicar isso produz altura pior que a procedural.
                if result.confidence == "baixa":
                    log.warning(
                        "Alturas por sombra descartadas: %s", result.note or "confianca baixa"
                    )
                else:
                    ctx.shadow_heights = result.heights
                stats["shadow"] = result.summary()
            except Exception as exc:  # noqa: BLE001 - altura procedural continua valendo
                log.warning("Estimativa por sombra falhou: %s", exc)

    # A agua vem antes do terreno de proposito: com relevo, ela escava a grade
    # antes de a malha ser construida.
    ctx.report("agua", 0.05)
    water_union = generate_water(builder, ctx, map_data.waters, map_data.rivers)

    ctx.report("terreno", 0.15)
    generate_terrain(builder, ctx, water_union)
    generate_landuse(builder, ctx, map_data)

    # A uniao dos footprints e usada para recortar as calcadas; vale o custo
    # porque sem ela a calcada atravessa o terreo dos predios de esquina.
    building_union = None
    if ctx.settings.sidewalks and ctx.settings.buildings and map_data.buildings:
        try:
            building_union = unary_union([b.footprint for b in map_data.buildings if b.footprint])
        except Exception as exc:  # noqa: BLE001 - segue sem o recorte
            log.debug("Uniao de edificios falhou: %s", exc)

    ctx.report("ruas", 0.30)
    roads = generate_roads(
        builder, ctx, map_data.roads, water_union=water_union, building_union=building_union
    )
    generate_railways(builder, ctx, map_data.railways)
    stats["roads"] = len(map_data.roads)

    ctx.report("edificios", 0.45)
    stats["buildings"] = generate_buildings(builder, ctx, map_data.buildings)

    ctx.report("vegetacao", 0.80)
    stats["trees"] = generate_vegetation(builder, ctx, map_data, exclude=roads.paved_union)

    ctx.report("finalizando malha", 0.95)
    groups = builder.build()
    elapsed = time.perf_counter() - started

    scene = Scene(
        name=f"mapforge_{ctx.settings.style}",
        groups=groups,
        metadata={
            "bbox": ctx.bbox.key(),
            "center": ctx.bbox.center,
            "size_m": [round(ctx.bbox.width_m, 1), round(ctx.bbox.height_m, 1)],
            "style": ctx.settings.style,
            "detail": ctx.settings.detail,
            "seed": ctx.settings.seed,
            "settings": ctx.settings.to_dict(),
            "source": map_data.summary(),
            "generated": stats,
            "materials": len(groups),
            "build_seconds": round(elapsed, 2),
        },
    )
    if ctx.terrain is not None:
        scene.metadata["terrain"] = {
            "grid": [len(ctx.terrain.xs), len(ctx.terrain.ys)],
            "base_m": round(ctx.terrain.base, 1),
            "relief_m": round(ctx.terrain.relief, 1),
        }
    ctx.report("pronto", 1.0)
    log.info(
        "Cena gerada em %.2fs: %d triangulos, %d materiais",
        elapsed,
        scene.triangle_count,
        len(groups),
    )
    return scene
