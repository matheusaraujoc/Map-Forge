"""Geracao da agua: lagos, reservatorios e rios.

O canal e escavado de verdade: o terreno e recortado, as margens descem em
talude ate o leito e a lamina fica alguns decimetros abaixo do solo. E isso que
faz um rio parecer um rio e nao uma fita azul colada no chao - e o que da
sentido as pontes geradas em roads.py.
"""

from __future__ import annotations

import logging

import numpy as np
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


# Comprimento de um trecho de rio, em metros.
#
# Um nivel unico por corpo d'agua so funciona em lago. Num rio de 900 m descendo
# um vale, a lamina plana ou flutua acima do terreno na cabeceira ou fica
# enterrada na foz. O corpo e fatiado ao longo do proprio eixo e cada trecho
# ganha o seu nivel.
REACH_M = 140.0

# Teto de trechos por corpo: acima disto o custo de escavar nao paga o ganho.
MAX_REACHES = 48


def _long_axis(body):
    """Direcao do eixo maior do corpo, em graus. Zero quando indefinida."""
    import math

    try:
        obb = body.minimum_rotated_rectangle
        cantos = np.asarray(obb.exterior.coords, dtype=np.float64)[:-1]
    except Exception:  # noqa: BLE001 - topologia ruim
        return 0.0
    if len(cantos) != 4:
        return 0.0
    lado_a, lado_b = cantos[1] - cantos[0], cantos[2] - cantos[1]
    maior = lado_a if np.linalg.norm(lado_a) >= np.linalg.norm(lado_b) else lado_b
    return math.degrees(math.atan2(float(maior[1]), float(maior[0])))


def _slice_body(body, reach_m: float):
    """Fatia o corpo em trechos ao longo do eixo maior, de montante a jusante.

    Devolve a lista de pedacos na ordem do eixo. Corpo curto (lago, acude) volta
    inteiro numa fatia so - fatiar um lago seria inventar desnivel onde a agua e
    de fato uma superficie unica.
    """
    from shapely import affinity
    from shapely.geometry import box as caixa

    angulo = _long_axis(body)
    centro = body.centroid
    alinhado = affinity.rotate(body, -angulo, origin=centro)
    minx, miny, maxx, maxy = alinhado.bounds
    comprimento = maxx - minx
    largura = maxy - miny

    # Lago: tao largo quanto comprido. A agua parada tem um nivel so.
    if comprimento < reach_m * 1.5 or comprimento < largura * 2.0:
        return [body]

    n = min(max(int(round(comprimento / reach_m)), 2), MAX_REACHES)
    passo = comprimento / n

    partes = []
    for i in range(n):
        faixa = caixa(minx + i * passo, miny - 1.0, minx + (i + 1) * passo, maxy + 1.0)
        try:
            pedaco = alinhado.intersection(faixa)
        except Exception:  # noqa: BLE001 - topologia ruim: desiste de fatiar
            return [body]
        if pedaco.is_empty or pedaco.area < 1.0:
            continue
        partes.append(affinity.rotate(pedaco, angulo, origin=centro))
    return partes or [body]


def _reach_levels(partes, terrain) -> list[float]:
    """Nivel de cada trecho, suavizado e obrigado a nao subir para jusante.

    Duas coisas separam isto de medir cada trecho isoladamente:

    - **suavizacao**: o DEM tem ruido, e trecho a trecho ele produz degraus de
      alguns decimetros que aparecem como escada na lamina;
    - **monotonia**: agua nao sobe. Jusante e a ponta mais baixa; dali para tras
      cada trecho e obrigado a ficar no maximo no nivel do anterior. Sem isto um
      pico de DEM no meio do rio levanta uma represa que nao existe.
    """
    brutos = [terrain.level_over(p, percentile=15.0) for p in partes]
    if len(brutos) < 3:
        return brutos

    suaves = []
    for i in range(len(brutos)):
        janela = brutos[max(i - 1, 0) : i + 2]
        suaves.append(float(np.mean(janela)))

    # Jusante e a ponta mais baixa; percorre de montante para la.
    if suaves[-1] <= suaves[0]:
        for i in range(1, len(suaves)):
            suaves[i] = min(suaves[i], suaves[i - 1])
    else:
        for i in range(len(suaves) - 2, -1, -1):
            suaves[i] = min(suaves[i], suaves[i + 1])
    return suaves


def _generate_water_on_terrain(
    builder: MeshBuilder, ctx: GenerationContext, merged, por_id=None
):
    """Agua sobre relevo: escava a grade e poe a lamina por trecho de rio.

    Cada trecho tem o seu nivel, tirado do percentil baixo das alturas sob ele -
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
    trechos = 0
    for body in bodies:
        if body.is_empty or body.area < 4.0:
            continue
        # O material sai do corpo inteiro, uma vez so: resolver por trecho faria
        # o mesmo rio trocar de cor no meio.
        material = _material_for(ctx, body, por_id)
        partes = _slice_body(body, REACH_M)
        niveis = _reach_levels(partes, terrain)
        trechos += len(partes)

        for i, (parte, nivel) in enumerate(zip(partes, niveis)):
            # O leito e escavado ate o nivel do trecho **mais baixo da
            # vizinhanca**, e nao ate o do proprio trecho.
            #
            # Escavar cada trecho ate o proprio nivel parece obvio e produz um
            # acude a cada emenda: o leito do trecho de cima fica `profundidade`
            # abaixo do nivel dele, o que ainda pode estar acima da lamina do
            # trecho de baixo. Num vale com 1,3 m de queda por trecho e 0,5 m de
            # lamina, sobra 0,8 m de leito seco atravessado no meio do rio.
            # Medido na cena de teste: 106 de 328 vertices de leito acima da
            # agua, mesmo com a escavacao ja corrigida dentro de cada trecho.
            vizinho = min(niveis[max(i - 1, 0)], niveis[min(i + 1, len(niveis) - 1)])
            terrain.carve(parte, depth=depth, level=min(nivel, vizinho))
            builder.add_flat(material, parte, nivel + layers.Z_WATER)

    if trechos > len(bodies):
        log.info("Agua: %d corpos em %d trechos de nivel proprio", len(bodies), trechos)
    return merged
