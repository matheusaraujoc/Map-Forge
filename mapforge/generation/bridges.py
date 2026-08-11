"""Pontes: o vao sobre a agua e o apoio do viaduto tagueado.

Havia um sistema de ponte aqui antes, e ele fazia uma coisa so: fechar o vao por
baixo com uma laje. A premissa estava escrita no codigo - "a pista permanece no
nivel do solo, quem esta rebaixado e o rio" - e ela e falsa quando ha relevo.

Com relevo, a pista e **assentada no terreno**, e o terreno debaixo do rio foi
escavado alguns metros antes, pela propria geracao da agua. A pista entao
acompanhava a escavacao e **mergulhava no leito**: a rua descia dentro do rio,
atravessava o fundo e subia do outro lado, com a laje descendo junto. Nao era
uma ponte; era um vau.

Uma ponte precisa de tres coisas que a laje sozinha nao dava:

1. **Vao plano.** O tabuleiro fica no nivel dos dois encontros - as margens -,
   nao no nivel do fundo. Para isso o trecho de pista sobre a agua **sai** da
   superficie assentada e e substituido por geometria propria, com altura
   absoluta.
2. **Guarda-corpo.** Sem mureta lateral a ponte le como uma tira de asfalto
   flutuando. O guarda-corpo acompanha so as bordas do vao - as duas entradas
   ficam abertas, senao a ponte vira caixa fechada.
3. **Apoio.** Vao longo pede pilar. O pilar nasce no leito ja escavado e sobe
   ate o fundo do tabuleiro, o que tambem resolve a limitacao antiga dos
   viadutos tagueados, que ficavam suspensos no ar sem nada embaixo.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..core.mesh import MeshBuilder
from . import layers
from .context import GenerationContext

log = logging.getLogger(__name__)

# Area minima de travessia que ganha tabuleiro.
#
# Baixa de proposito. A tentacao e reservar a ponte para o rio de verdade e
# deixar a vala passar batido - mas a vala tambem e escavada (o leito desce
# 1,05 m sempre, seja rio ou valeta), entao sem tabuleiro a pista mergulha nela
# do mesmo jeito. Medido em Ouro Preto com o limiar em 10 m2: 39 vertices de
# pista ainda dentro da agua, todos em travessias pequenas. Um tabuleiro de 3 m2
# custa doze triangulos.
MIN_SPAN_M2 = 2.0

# Quanto o tabuleiro avanca sobre cada margem, alem do talude, para apoiar em
# terra firme.
APPROACH_M = 5.0

# Largura da faixa de encontro: a entrada da ponte, de onde sai a altura dela.
#
# A faixa e recortada **dentro do proprio tabuleiro**, onde ele encosta no resto
# da malha viaria. A primeira versao amostrava um anel de pista de 14 m em volta
# da travessia, e isso e um erro grosseiro em cidade de montanha: em Ouro Preto
# o anel pegava a ladeira que passa perto do corrego, e o tabuleiro subia com
# ela. Medido: **19 de 27 tabuleiros mais de 6 m acima do proprio vao**, dois
# deles 99 e 121 m no ar. A entrada da ponte e a unica amostra que descreve por
# onde a pista de fato chega nela.
ENCONTRO_M = 3.0

# Teto de seguranca: o tabuleiro nao passa disto acima da margem da travessia.
# Existe porque a amostra pode falhar (travessia sem continuacao de pista, no
# limite da regiao), e uma ponte no ar e pior que uma ponte baixa.
MAX_RISE_M = 5.0

# Guarda-corpo.
PARAPET_W = 0.32
PARAPET_H = 0.95
# Recuo da entrada: o anel do guarda-corpo e cortado onde a ponte encontra o
# resto da pista, senao ele fecha a passagem.
ENTRY_CLEAR_M = 0.7

# Pilares.
PILLAR_SPACING_M = 24.0
PILLAR_HALF = 0.62
# Vao mais curto que isto nao ganha pilar - ponte de bairro vence de uma vez.
PILLAR_MIN_SPAN_M = 26.0


def _polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def _sample_heights(ctx: GenerationContext, geom) -> np.ndarray:
    """Alturas do terreno sobre uma geometria (contorno + miolo)."""
    if not ctx.draped or geom is None or geom.is_empty:
        return np.zeros(1)

    from shapely import contains_xy

    pontos: list[np.ndarray] = []
    for poly in _polygons(geom):
        borda = np.asarray(poly.exterior.coords, dtype=np.float64)
        if len(borda):
            pontos.append(borda[:, :2])
    minx, miny, maxx, maxy = geom.bounds
    xs = np.linspace(minx, maxx, 12)
    ys = np.linspace(miny, maxy, 12)
    grade_x, grade_y = np.meshgrid(xs, ys)
    plano = np.column_stack([grade_x.ravel(), grade_y.ravel()])
    try:
        dentro = contains_xy(geom, plano[:, 0], plano[:, 1])
        if dentro.any():
            pontos.append(plano[dentro])
    except Exception:  # noqa: BLE001 - topologia ruim: fica so com o contorno
        pass

    if not pontos:
        return np.zeros(1)
    todos = np.concatenate(pontos, axis=0)
    return np.asarray(ctx.terrain.height(todos[:, 0], todos[:, 1]), dtype=np.float64)


def _deck_level(ctx: GenerationContext, apoio, vao) -> float:
    """Altura do tabuleiro: o nivel dos encontros, nao o do fundo do rio.

    Percentil alto de proposito. As duas margens raramente estao na mesma cota,
    e uma ponte plana no nivel da margem mais baixa entraria dentro da mais alta.
    Ficar no nivel da mais alta deixa um degrau pequeno de um lado, que a pista
    assentada absorve nos poucos metros do encontro.
    """
    if not ctx.draped:
        return layers.Z_ROAD

    alturas = _sample_heights(ctx, apoio)
    if len(alturas) < 3:
        alturas = _sample_heights(ctx, vao)
    return float(np.percentile(alturas, 78.0)) + layers.Z_ROAD


def _parapets(builder: MeshBuilder, ctx: GenerationContext, deck, resto, topo: float) -> None:
    """Mureta nas laterais do tabuleiro, aberta nas duas entradas."""
    try:
        miolo = deck.buffer(-PARAPET_W)
        anel = deck.difference(miolo) if not miolo.is_empty else deck
    except Exception:  # noqa: BLE001 - topologia ruim
        return
    if anel.is_empty:
        return

    if resto is not None and not resto.is_empty:
        try:
            anel = anel.difference(resto.buffer(ENTRY_CLEAR_M))
        except Exception:  # noqa: BLE001 - segue com o anel inteiro
            pass
    if anel.is_empty:
        return

    builder.add_prism(
        ctx.palette.curb, anel, topo, topo + PARAPET_H, cap_top=True, drape=False
    )


def _pillar_spots(vao, espacamento: float) -> list[tuple[float, float]]:
    """Pontos ao longo do eixo maior do vao onde cabe um pilar."""
    from shapely import affinity, contains_xy

    try:
        obb = vao.minimum_rotated_rectangle
        cantos = np.asarray(obb.exterior.coords, dtype=np.float64)[:-1]
    except Exception:  # noqa: BLE001 - topologia ruim
        return []
    if len(cantos) != 4:
        return []

    lado_a, lado_b = cantos[1] - cantos[0], cantos[2] - cantos[1]
    comp_a, comp_b = float(np.linalg.norm(lado_a)), float(np.linalg.norm(lado_b))
    comprimento = max(comp_a, comp_b)
    if comprimento < PILLAR_MIN_SPAN_M:
        return []

    maior = lado_a if comp_a >= comp_b else lado_b
    angulo = math.degrees(math.atan2(float(maior[1]), float(maior[0])))
    centro = vao.centroid
    alinhado = affinity.rotate(vao, -angulo, origin=centro)
    minx, _, maxx, _ = alinhado.bounds

    n = max(int(round((maxx - minx) / espacamento)), 1)
    if n < 2:
        return []

    from shapely.geometry import MultiPoint, box as caixa

    _, miny, _, maxy = alinhado.bounds
    candidatos = []
    for i in range(1, n):
        x = minx + (maxx - minx) * i / n
        # Ponto no meio da secao do vao naquela distancia ao longo do eixo.
        recorte = alinhado.intersection(caixa(x - 0.5, miny - 1.0, x + 0.5, maxy + 1.0))
        if recorte.is_empty:
            continue
        ponto = recorte.representative_point()
        candidatos.append((float(ponto.x), float(ponto.y)))

    if not candidatos:
        return []

    # Volta ao referencial do mundo de uma vez so.
    girados = affinity.rotate(MultiPoint(candidatos), angulo, origin=centro)
    pares = [(float(p.x), float(p.y)) for p in girados.geoms]
    dentro = contains_xy(
        vao, np.array([p[0] for p in pares]), np.array([p[1] for p in pares])
    )
    return [p for p, ok in zip(pares, dentro) if ok]


def _pillars(builder: MeshBuilder, ctx: GenerationContext, vao, topo: float) -> int:
    """Pilar do leito ate o fundo do tabuleiro."""
    pontos = _pillar_spots(vao, PILLAR_SPACING_M)
    if not pontos:
        return 0

    fundo_laje = topo - layers.DECK_THICKNESS
    material = ctx.palette.curb
    total = 0
    for x, y in pontos:
        leito = ctx.ground_at(x, y) if ctx.draped else layers.Z_WATER_BED
        if fundo_laje - leito < 0.6:
            continue  # vao raso: o pilar viraria um calco
        secao = Polygon(
            [
                (x - PILLAR_HALF, y - PILLAR_HALF),
                (x + PILLAR_HALF, y - PILLAR_HALF),
                (x + PILLAR_HALF, y + PILLAR_HALF),
                (x - PILLAR_HALF, y + PILLAR_HALF),
            ]
        )
        builder.add_prism(material, secao, leito - 0.4, fundo_laje, cap_top=False, drape=False)
        total += 1
    return total


def _deck_material(deck, surfaces: dict, padrao):
    """Material do pavimento que mais cobre o tabuleiro."""
    melhor, area_melhor = padrao, 0.0
    for material, geom in surfaces.items():
        if geom is None or geom.is_empty:
            continue
        try:
            area = deck.intersection(geom).area
        except Exception:  # noqa: BLE001 - topologia ruim
            continue
        if area > area_melhor:
            melhor, area_melhor = material, area
    return melhor


def build_water_bridges(
    builder: MeshBuilder,
    ctx: GenerationContext,
    roads_union,
    water_union,
    surfaces: dict,
    default_material,
):
    """Constroi as pontes onde a pista cruza agua.

    Devolve a uniao dos tabuleiros. Quem chama **precisa subtrair** essa uniao da
    superficie assentada: e essa subtracao que tira a pista do fundo do rio.
    """
    if roads_union is None or water_union is None:
        return None
    if roads_union.is_empty or water_union.is_empty:
        return None

    try:
        travessias = roads_union.intersection(water_union)
    except Exception:  # noqa: BLE001 - topologia ruim
        return None
    if travessias.is_empty:
        return None

    # Largura do talude que a escavacao da agua abriu em volta do rio.
    #
    # O **vao tem de cobri-lo**. Se o tabuleiro parasse na beira da agua, a pista
    # assentada ainda desceria a rampa inteira antes de encontra-lo, e sobraria
    # um degrau na entrada da ponte. Cobrindo o talude, a entrada do tabuleiro
    # cai em terreno que a escavacao nao tocou - que e de onde a altura da ponte
    # e medida, logo abaixo.
    passo = float(getattr(ctx.terrain, "step", 0.0) or 0.0) if ctx.draped else 0.0
    talude = max(passo * 1.6, 6.0) if ctx.draped else 0.0
    alcance = APPROACH_M + talude

    tabuleiros = []
    pilares = 0
    for vao in _polygons(travessias):
        if vao.area < MIN_SPAN_M2:
            continue
        try:
            deck = vao.buffer(alcance, quad_segs=1, join_style=2).intersection(roads_union)
        except Exception:  # noqa: BLE001 - topologia ruim
            continue
        # A intersecao pode trazer pedacos soltos da malha viaria vizinha; fica
        # so o que encosta no proprio vao.
        pedacos = [p for p in _polygons(deck) if p.intersects(vao)]
        if not pedacos:
            continue
        deck = unary_union(pedacos)

        # Encontro: a faixa do proprio tabuleiro onde ele encosta no resto da
        # pista. Como o tabuleiro ja cobre o talude, o terreno ali nao foi
        # tocado pela escavacao - e como e faixa **do tabuleiro**, ela descreve
        # por onde a pista chega na ponte, e nao o que passa perto dela.
        try:
            resto = roads_union.difference(deck)
        except Exception:  # noqa: BLE001
            resto = None

        apoio = None
        if resto is not None and not resto.is_empty:
            try:
                apoio = deck.intersection(resto.buffer(ENCONTRO_M))
            except Exception:  # noqa: BLE001
                apoio = None
        if apoio is None or apoio.is_empty:
            try:
                apoio = deck.difference(water_union)
            except Exception:  # noqa: BLE001
                apoio = deck

        topo = _deck_level(ctx, apoio, vao)
        # Trava: ponte no ar e pior que ponte baixa.
        teto = float(_sample_heights(ctx, vao).max()) + MAX_RISE_M + layers.Z_ROAD
        topo = min(topo, teto)

        material = _deck_material(deck, surfaces, default_material)
        simplificado = ctx.simplify(deck)

        # Tabuleiro: laje macica com o pavimento por cima. Sem drape - e o que
        # faz a ponte ser plana em vez de acompanhar o fundo escavado.
        builder.add_prism(
            ctx.palette.curb,
            simplificado,
            topo - layers.DECK_THICKNESS,
            topo,
            cap_top=True,
            cap_bottom=True,
            top_material=material,
            drape=False,
        )
        _parapets(builder, ctx, simplificado, resto, topo)
        pilares += _pillars(builder, ctx, vao, topo)
        tabuleiros.append(deck)

    if not tabuleiros:
        return None

    log.info("Pontes: %d vaos sobre agua, %d pilares", len(tabuleiros), pilares)
    return unary_union(tabuleiros)


def build_viaduct_supports(builder: MeshBuilder, ctx: GenerationContext, elevated) -> int:
    """Guarda-corpo e pilares dos viadutos tagueados no OSM.

    O tabuleiro deles ja e desenhado em `roads.py`, assentado no relevo. O que
    faltava - e estava anotado como limitacao conhecida - era o que segura a
    estrutura: eles ficavam suspensos no ar. O pilar sai do terreno e sobe ate o
    fundo da laje, na mesma altura relativa em que ela foi desenhada.
    """
    total = 0
    for z, _key, surface in elevated:
        if surface is None or surface.is_empty:
            continue
        for parte in _polygons(surface):
            pontos = _pillar_spots(parte, PILLAR_SPACING_M)
            for x, y in pontos:
                chao = ctx.ground_at(x, y)
                fundo = chao + z - layers.Z_BRIDGE_DECK
                if fundo - chao < 1.2:
                    continue
                secao = Polygon(
                    [
                        (x - PILLAR_HALF, y - PILLAR_HALF),
                        (x + PILLAR_HALF, y - PILLAR_HALF),
                        (x + PILLAR_HALF, y + PILLAR_HALF),
                        (x - PILLAR_HALF, y + PILLAR_HALF),
                    ]
                )
                builder.add_prism(
                    ctx.palette.curb, secao, chao - 0.3, fundo, cap_top=False, drape=False
                )
                total += 1

    if total:
        log.info("Viadutos: %d pilares", total)
    return total
