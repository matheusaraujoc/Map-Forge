"""Geracao da agua: lagos, reservatorios e rios.

O canal e escavado de verdade: o terreno e recortado, as margens descem em
talude ate o leito e a lamina fica alguns decimetros abaixo do solo. E isso que
faz um rio parecer um rio e nao uma fita azul colada no chao - e o que da
sentido as pontes geradas em roads.py.
"""

from __future__ import annotations

import logging

from shapely.ops import unary_union

from ..core.mesh import MeshBuilder
from . import layers
from .context import GenerationContext
from .curves import smooth_river

log = logging.getLogger(__name__)

# Largura padrao por tipo de curso d'agua quando o OSM nao informa.
WATERWAY_WIDTH = {
    "river": 16.0,
    "canal": 11.0,
    "stream": 4.5,
    "ditch": 2.2,
    "drain": 2.2,
}

# Canal e vala sao obra: margem reta, sem meandro. Suavizar o eixo deles produz
# uma curva que nao existe no terreno.
STRAIGHT_WATERWAYS = {"canal", "ditch", "drain"}

# Fundura relativa por tipo. Corrego e raso e deixa o leito aparecer; rio e
# fundo e a lamina esconde o fundo.
DEPTH_FACTOR = {
    "river": 1.0,
    "canal": 0.85,
    "stream": 0.45,
    "ditch": 0.35,
    "drain": 0.35,
}


def _is_dry(feature) -> bool:
    """Curso intermitente: no semiarido brasileiro e a maioria.

    `intermittent=yes` significa leito seco a maior parte do ano. Pintar de azul
    um rio que so tem agua na cheia e erro grosseiro de leitura da paisagem -
    o que se ve de cima e areia.
    """
    tags = getattr(feature, "tags", {}) or {}
    return tags.get("intermittent") == "yes" or tags.get("seasonal") == "yes"


def generate_water(builder: MeshBuilder, ctx: GenerationContext, waters, rivers):
    """Desenha o canal e devolve a uniao usada para recortar o terreno."""
    if not ctx.settings.water:
        return None

    parts = []
    secos = []  # leitos intermitentes: areia, nao agua
    # osm_id -> geometria, para casar cada corpo com a cor amostrada nele.
    por_id: dict[int, object] = {}

    for water in waters:
        if water.geometry is None or water.geometry.is_empty:
            continue
        if _is_dry(water):
            secos.append(water.geometry)
            continue
        parts.append(water.geometry)
        por_id[water.osm_id] = water.geometry

    segments = max(ctx.detail["buffer_segments"], 2)
    detail = ctx.settings.detail
    for river in rivers:
        line = river.centerline
        if line is None or line.length < 2.0:
            continue
        tipo = river.tags.get("waterway", "")
        # Canal nao meandra: e vala escavada em linha reta.
        if tipo not in STRAIGHT_WATERWAYS:
            line = smooth_river(line, detail)
        width = river.width or WATERWAY_WIDTH.get(tipo, 4.0)
        faixa = line.buffer(
            max(width, 1.5) / 2.0, quad_segs=segments, cap_style=1, join_style=1
        )
        if _is_dry(river):
            secos.append(faixa)
            continue
        parts.append(faixa)
        por_id[river.osm_id] = faixa

    # O leito seco entra como solo exposto, no nivel do chao: sem lamina, sem
    # escavacao profunda, so a marca clara do talvegue.
    if secos:
        leito = ctx.simplify(unary_union(secos), factor=0.3)
        if not leito.is_empty:
            builder.add_flat(
                ctx.palette.dirt, leito, layers.Z_GROUND + 0.01, drape=ctx.draped
            )
            log.info("Leitos intermitentes: %.2f ha secos", leito.area / 1e4)

    if not parts:
        return None

    merged = ctx.simplify(unary_union(parts), factor=0.3)
    if merged.is_empty:
        return None

    palette = ctx.palette

    if ctx.draped:
        # Com relevo, quem escava o canal e o proprio terreno: cada corpo d'agua
        # rebaixa a grade para o seu nivel e a lamina fica plana por cima. Talude
        # e leito separados nao fazem falta - o DEM ja da a forma do vale.
        return _generate_water_on_terrain(builder, ctx, merged, por_id)

    bank_width = layers.BANK_WIDTH

    # O leito e a area que sobra depois de recuar a largura do talude. Em corregos
    # estreitos ela some, entao o talude vai estreitando ate caber.
    bed = None
    while bank_width > 0.4:
        candidate = merged.buffer(-bank_width)
        if not candidate.is_empty and candidate.area > merged.area * 0.08:
            bed = candidate
            break
        bank_width *= 0.6

    if bed is None:
        # Curso d'agua raso demais para escavar: lamina rasa com parede vertical.
        shallow_z = layers.Z_WATER * 0.5
        builder.add_flat(palette.water, merged, shallow_z)
        builder.add_walls(palette.bank, merged, shallow_z, layers.Z_GROUND)
        return merged

    try:
        ring = merged.difference(bed)
    except Exception:  # noqa: BLE001 - topologia ruim
        ring = None

    if ring is not None and not ring.is_empty:
        builder.add_slope(
            palette.bank,
            ring,
            bed,
            outer_z=layers.Z_GROUND,
            inner_z=layers.Z_WATER_BED,
            width=bank_width,
        )

    builder.add_flat(palette.water_bed, bed, layers.Z_WATER_BED)
    # A lamina cobre o canal inteiro e encosta no talude, como agua de verdade.
    builder.add_flat(_material_for(ctx, merged, por_id), merged, layers.Z_WATER)

    return merged


def _material_for(ctx, body, por_id):
    """Material amostrado do corpo d'agua que cai dentro deste pedaco.

    Depois da uniao, os corpos perdem o osm_id: o que sobra e um MultiPolygon.
    A associacao volta pelo ponto interno de cada geometria original - basta um
    acerto para o pedaco herdar a cor medida naquele rio.
    """
    materiais = getattr(ctx, "water_materials", None)
    if not materiais:
        return ctx.palette.water
    for osm_id, geom in por_id.items():
        material = materiais.get(osm_id)
        if material is None or geom is None or geom.is_empty:
            continue
        try:
            if body.contains(geom.representative_point()):
                return material
        except Exception:  # noqa: BLE001 - topologia ruim
            continue
    return ctx.palette.water


def _generate_water_on_terrain(
    builder: MeshBuilder, ctx: GenerationContext, merged, por_id=None
):
    """Agua sobre relevo: escava a grade e poe uma lamina plana por corpo d'agua.

    Cada corpo tem o seu nivel, tirado do percentil baixo das alturas sob ele -
    usar a media faria a agua subir a encosta em vales estreitos.
    """
    from shapely.geometry import MultiPolygon, Polygon

    terrain = ctx.terrain
    por_id = por_id or {}

    if isinstance(merged, Polygon):
        bodies = [merged]
    elif isinstance(merged, MultiPolygon):
        bodies = list(merged.geoms)
    else:
        bodies = [g for g in getattr(merged, "geoms", []) if isinstance(g, Polygon)]

    depth = abs(layers.Z_WATER_BED - layers.Z_WATER)
    for body in bodies:
        if body.is_empty or body.area < 4.0:
            continue
        level = terrain.level_over(body, percentile=15.0)
        terrain.carve(body, depth=depth, level=level)
        builder.add_flat(_material_for(ctx, body, por_id), body, level + layers.Z_WATER)

    return merged
