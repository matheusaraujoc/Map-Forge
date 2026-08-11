"""Orquestrador: MapData + configuracoes -> Scene pronta para renderizar/exportar."""

from __future__ import annotations

import logging
import time

from shapely.ops import unary_union

from ..core.features import MapData
from ..core.mesh import MeshBuilder, Scene
from .buildings import generate_buildings
from .coloring import (
    build_area_materials,
    build_ground_color,
    build_road_materials,
    build_roof_materials,
    build_water_materials,
)
from .context import GenerationContext
from .roads import generate_railways, generate_roads
from .structures import generate_street_lamps, generate_structures
from .terrain import generate_landuse, generate_terrain
from .vegetation import generate_vegetation
from .water import generate_water

log = logging.getLogger(__name__)


def build_scene(map_data: MapData, ctx: GenerationContext) -> Scene:
    """Executa o pipeline de geracao na ordem em que as camadas se empilham."""
    started = time.perf_counter()
    builder = MeshBuilder(terrain=ctx.terrain, clip=ctx.clip, log=ctx.geometry_log)
    stats: dict[str, int] = {}
    ctx.map_data = map_data

    # Porte do assentamento antes de qualquer geometria: e o teto de pavimentos
    # de todo predio sem altura em tag.
    from .urban import SCALES, profile_for

    if ctx.settings.urban_scale:
        forcado = {s.key: s for s in SCALES}.get(ctx.settings.urban_scale)
        ctx.urban = forcado or profile_for(map_data, ctx.bbox)
    else:
        ctx.urban = profile_for(map_data, ctx.bbox)
    ctx.report(f"porte: {ctx.urban.label}", 0.02)

    if ctx.imagery is not None:
        ctx.report("amostrando cores reais", 0.03)
        try:
            if ctx.settings.buildings:
                ctx.roof_materials = build_roof_materials(ctx, map_data.buildings)
            ctx.area_materials = build_area_materials(ctx, map_data)
            ctx.ground_material = build_ground_color(ctx)
            if ctx.settings.water:
                ctx.water_materials = build_water_materials(
                    ctx, map_data.waters, map_data.rivers
                )
            if ctx.settings.roads:
                ctx.road_materials = build_road_materials(ctx, map_data.roads)
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
    # Serve a dois usos: recortar a calcada e impedir arvore dentro de casa.
    building_union = None
    if ctx.settings.buildings and map_data.buildings:
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

    ctx.report("vegetacao", 0.78)
    # Arvore nao nasce dentro de casa: o que ja foi construido tambem exclui.
    # Sem isto copa e telhado se atravessam, que era o defeito mais visivel.
    excluir = roads.paved_union
    if building_union is not None and not building_union.is_empty:
        recuo = building_union.buffer(1.5)
        excluir = recuo if excluir is None else unary_union([excluir, recuo])
    stats["trees"] = generate_vegetation(builder, ctx, map_data, exclude=excluir)

    ctx.report("torres, silos e iluminacao", 0.88)
    stats["structures"] = generate_structures(builder, ctx, map_data.structures)
    stats["lamps"] = generate_street_lamps(builder, ctx, roads)

    # --- malha de fisica ---
    #
    # Sai depois de todo o visual porque e alimentada pelo que os geradores de
    # fato produziram, nao por uma segunda leitura do MapData.
    colisores = None
    if ctx.settings.colliders:
        ctx.report("malha de fisica", 0.92)
        try:
            from .colliders import add_collider_meshes, build_colliders

            colisores = build_colliders(ctx, map_data, water_union)
            stats["fisica"] = add_collider_meshes(
                builder, colisores, naming=ctx.settings.collider_naming
            )
            ctx.collider_set = colisores
            ctx.report(f"fisica: {colisores.summary()}", 0.94)
        except Exception as exc:  # noqa: BLE001 - a cena visual continua valendo
            log.warning("Malha de fisica falhou: %s", exc)
            colisores = None

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
    if colisores is not None:
        scene.metadata["colliders"] = colisores.to_dict()
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
