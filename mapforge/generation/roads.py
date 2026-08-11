"""Geracao das ruas: pista, calcada, meio-fio e sinalizacao.

As vias sao suavizadas, agrupadas por tipo de pavimento, sofrem buffer pela
largura da classe e sao unidas antes de virar malha. Unir antes de triangular
elimina as costuras nos cruzamentos e reduz muito a contagem de triangulos.

Superficies de pavimentos diferentes sao subtraidas em ordem de prioridade, de
modo que uma calcada nunca fique flutuando por cima de uma avenida.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString
from shapely.ops import unary_union

from ..core.features import Road, RoadClass
from ..core.mesh import Material, MeshBuilder
from . import layers
from .bridges import build_viaduct_supports, build_water_bridges
from .context import GenerationContext
from .curves import offset_polyline, smooth_road

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoadSpec:
    """Perfil transversal de uma classe viaria."""

    lanes: int
    lane_width: float
    sidewalk: float
    surface: str  # chave do material padrao
    markings: bool = False  # recebe sinalizacao horizontal
    edge_lines: bool = False  # recebe faixa de bordo continua


ROAD_SPECS: dict[RoadClass, RoadSpec] = {
    RoadClass.MOTORWAY: RoadSpec(3, 3.75, 0.0, "major", markings=True, edge_lines=True),
    RoadClass.TRUNK: RoadSpec(2, 3.65, 0.0, "major", markings=True, edge_lines=True),
    RoadClass.PRIMARY: RoadSpec(2, 3.50, 3.0, "major", markings=True, edge_lines=True),
    RoadClass.SECONDARY: RoadSpec(2, 3.40, 2.6, "road", markings=True, edge_lines=True),
    RoadClass.TERTIARY: RoadSpec(2, 3.20, 2.2, "road", markings=True),
    RoadClass.RESIDENTIAL: RoadSpec(2, 2.90, 1.8, "road", markings=True),
    RoadClass.SERVICE: RoadSpec(1, 3.20, 0.0, "road"),
    RoadClass.PEDESTRIAN: RoadSpec(1, 7.00, 0.0, "foot"),
    RoadClass.FOOTWAY: RoadSpec(1, 2.00, 0.0, "foot"),
    RoadClass.CYCLEWAY: RoadSpec(1, 2.40, 0.0, "foot"),
    RoadClass.TRACK: RoadSpec(1, 3.00, 0.0, "dirt"),
}

# surface=* do OSM -> chave de material. Ouro Preto inteira e "sett".
SURFACE_MAP: dict[str, str] = {
    "asphalt": None,  # mantem o padrao da classe
    "concrete": None,
    "concrete:plates": None,
    "paved": None,
    "chipseal": None,
    "paving_stones": "cobble",
    "sett": "cobble",
    "cobblestone": "cobble",
    "unhewn_cobblestone": "cobble",
    "bricks": "cobble",
    "unpaved": "dirt",
    "gravel": "dirt",
    "fine_gravel": "dirt",
    "compacted": "dirt",
    "dirt": "dirt",
    "earth": "dirt",
    "ground": "dirt",
    "mud": "dirt",
    "sand": "dirt",
    "grass": "dirt",
}

# Quem fica por cima quando duas superficies se sobrepoem.
SURFACE_PRIORITY = ["major", "road", "cobble", "dirt", "foot"]

# Tolerancia de simplificacao das vias, como fracao da do nivel de detalhe.
# Baixa de proposito: a tolerancia cheia comeria os arcos das esquinas.
ROAD_SIMPLIFY = 0.2


def road_half_width(road: Road) -> float:
    """Meia-largura da pista, priorizando as tags width/lanes do OSM."""
    spec = ROAD_SPECS[road.road_class]
    if road.width and road.width > 0.5:
        return road.width / 2.0
    lanes = road.lanes if road.lanes and road.lanes > 0 else spec.lanes
    if road.oneway and not road.lanes:
        lanes = max(1, spec.lanes - 1)
    return max(1.0, lanes * spec.lane_width / 2.0)


def road_surface_key(road: Road) -> str:
    """Material do pavimento: tag surface quando informativa, senao a classe."""
    spec = ROAD_SPECS[road.road_class]
    override = SURFACE_MAP.get((road.tags.get("surface") or "").lower())
    return override or spec.surface


def road_role(road: Road) -> str:
    """Papel da via no desenho: 'surface', 'sidewalk' ou 'crossing'.

    Centros historicos costumam ter a calcada e a faixa de pedestre mapeadas como
    highway=footway. Tratadas como pavimento comum elas viram fitas soltas por
    cima do asfalto; encaminhadas para a camada certa, somam ao inves de poluir.
    """
    if road.road_class is not RoadClass.FOOTWAY:
        return "surface"
    kind = (road.tags.get("footway") or road.tags.get("path") or "").lower()
    if kind == "crossing" or road.tags.get("crossing"):
        return "crossing"
    if kind == "sidewalk":
        return "sidewalk"
    return "surface"


def _elevation(road: Road) -> float:
    if road.bridge:
        return layers.Z_BRIDGE + max(road.layer - 1, 0) * layers.LAYER_STEP
    if road.layer > 0:
        return road.layer * layers.LAYER_STEP
    return layers.Z_ROAD


@dataclass
class RoadResult:
    """Geometria util para os demais geradores."""

    surface_union: object | None = None  # pistas no nivel do solo
    paved_union: object | None = None  # pistas + calcadas no nivel do solo
    # (eixo, meia-largura, classe) - usado pela iluminacao publica.
    centerlines: list[tuple[LineString, float, RoadClass]] = field(default_factory=list)


# --------------------------------------------------------------- sinalizacao


def _sample_along(coords: np.ndarray, distances: np.ndarray):
    """Posicao e direcao unitaria em cada distancia ao longo da polilinha."""
    segments = np.diff(coords, axis=0)
    seg_len = np.linalg.norm(segments, axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(seg_len)])
    idx = np.clip(np.searchsorted(cumulative, distances, side="right") - 1, 0, len(segments) - 1)
    t = (distances - cumulative[idx]) / np.maximum(seg_len[idx], 1e-9)
    position = coords[idx] + segments[idx] * t[:, None]
    direction = segments[idx] / np.maximum(seg_len[idx], 1e-9)[:, None]
    return position, direction


def _drape(builder: MeshBuilder, vertices: np.ndarray) -> np.ndarray:
    """Assenta vertices ja prontos sobre o relevo (no-op no terreno plano)."""
    if builder.terrain is None:
        return vertices
    vertices = vertices.copy()
    vertices[:, 2] += builder.terrain.height(vertices[:, 0], vertices[:, 1])
    return vertices + [0.0, 0.0, builder.drape_bias]


def _emit(
    builder: MeshBuilder,
    material: Material,
    vertices: np.ndarray,
    faces: np.ndarray,
    avoid=None,
) -> None:
    """Assenta e envia uma malha ja triangulada, respeitando o contorno.

    A sinalizacao e construida como faixas prontas, sem passar por add_flat,
    entao o recorte da regiao desenhada precisa ser aplicado aqui - senao a
    pintura de solo avanca alguns metros para fora do terreno.

    `avoid` e a area onde a faixa nao pode ser desenhada. Serve a agua: a
    sinalizacao e assentada no relevo, e sobre um rio o relevo e o **leito
    escavado**, entao a faixa central acompanhava a pista para dentro do rio e
    reaparecia no fundo, debaixo da lamina. Medido em Ouro Preto: 47 vertices de
    pintura submersos, a 0,47 m em media da superficie da agua.
    """
    vertices = _drape(builder, vertices)
    if len(faces):
        import shapely

        def _cantos_dentro(corte):
            pontos = vertices[faces]
            return shapely.contains_xy(
                corte, pontos[:, :, 0].ravel(), pontos[:, :, 1].ravel()
            ).reshape(len(faces), 3)

        # O contorno da regiao mantem o triangulo com os **tres** cantos dentro;
        # a area proibida descarta o que tiver **qualquer** canto dentro. Testar
        # o centroide deixaria passar metade de um triangulo que cruza a borda.
        if builder.clip is not None:
            faces = faces[_cantos_dentro(builder.clip).all(axis=1)]
            if not len(faces):
                return
        if avoid is not None and not avoid.is_empty:
            faces = faces[~_cantos_dentro(avoid).any(axis=1)]
            if not len(faces):
                return

        # Compacta: sem isto os vertices das faces descartadas continuariam no
        # buffer, sem triangulo nenhum, e viajariam ate o arquivo exportado.
        usados = np.unique(faces)
        remapa = np.full(len(vertices), -1, dtype=np.int64)
        remapa[usados] = np.arange(len(usados))
        vertices = vertices[usados]
        faces = remapa[faces]
    builder.add_mesh(material, vertices, faces)


def _densify_coords(builder: MeshBuilder, coords: np.ndarray) -> np.ndarray:
    """Reamostra o eixo para a sinalizacao acompanhar o relevo.

    A faixa de bordo e uma fita entre duas polilinhas com os mesmos vertices do
    eixo. Num trecho reto de 200 m o eixo tem dois pontos, e a fita passaria
    reta por cima do morro.
    """
    if builder.terrain is None or builder.drape_edge <= 0 or len(coords) < 2:
        return coords
    line = LineString(coords)
    if line.length <= builder.drape_edge:
        return coords
    from shapely import segmentize

    return np.asarray(segmentize(line, builder.drape_edge).coords, dtype=np.float64)


def _quads_to_mesh(quads: np.ndarray, z: float) -> tuple[np.ndarray, np.ndarray]:
    """Converte (n, 4, 2) em malha plana na altura z, com winding anti-horario."""
    n = len(quads)
    verts = np.column_stack([quads.reshape(-1, 2), np.full(n * 4, z)])
    base = np.arange(n) * 4
    faces = np.concatenate(
        [
            np.column_stack([base, base + 1, base + 2]),
            np.column_stack([base, base + 2, base + 3]),
        ],
        axis=0,
    )
    return verts, faces


def _dashes(
    coords: np.ndarray,
    line_width: float,
    dash: float = 3.0,
    gap: float = 5.0,
    skip_ends: float = 7.0,
) -> np.ndarray | None:
    """Tracejado central. As pontas ficam livres para nao invadir o cruzamento."""
    total = float(np.linalg.norm(np.diff(coords, axis=0), axis=1).sum())
    usable = total - 2 * skip_ends
    if usable < dash:
        return None

    starts = np.arange(skip_ends, total - skip_ends - dash, dash + gap)
    if len(starts) == 0:
        return None
    ends = starts + dash

    p_start, direction = _sample_along(coords, starts)
    p_end, _ = _sample_along(coords, ends)
    normal = np.column_stack([-direction[:, 1], direction[:, 0]]) * (line_width / 2.0)

    return np.stack(
        [p_start - normal, p_end - normal, p_end + normal, p_start + normal], axis=1
    )


def _flat_strip(left: np.ndarray, right: np.ndarray, z: float):
    """Faixa continua entre duas polilinhas paralelas de mesmo tamanho."""
    n = min(len(left), len(right))
    if n < 2:
        return None
    left, right = left[:n], right[:n]
    verts = np.concatenate(
        [
            np.column_stack([left, np.full(n, z)]),
            np.column_stack([right, np.full(n, z)]),
        ]
    )
    idx = np.arange(n - 1)
    faces = np.concatenate(
        [
            np.column_stack([idx, idx + n, idx + n + 1]),
            np.column_stack([idx, idx + n + 1, idx + 1]),
        ],
        axis=0,
    )
    # Orienta pelo sinal da area do primeiro triangulo.
    a, b, c = verts[faces[0, 0]], verts[faces[0, 1]], verts[faces[0, 2]]
    if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) < 0:
        faces = faces[:, [0, 2, 1]]
    return verts, faces


def _add_crossing(
    builder: MeshBuilder,
    material: Material,
    line: LineString,
    coords: np.ndarray,
    avoid=None,
) -> None:
    """Faixa de pedestre: barras paralelas ao sentido da travessia."""
    from shapely.ops import substring

    if line.length < 2.5:
        return
    # Recua as pontas para a zebra nao invadir a calcada.
    try:
        trimmed = substring(line, 0.8, line.length - 0.8)
    except Exception:  # noqa: BLE001
        trimmed = line
    if trimmed.is_empty or trimmed.length < 1.5:
        return
    coords = _densify_coords(builder, np.asarray(trimmed.coords, dtype=np.float64))
    if len(coords) < 2:
        return

    bar_half = 0.22
    for offset in np.arange(-1.7, 1.71, 0.85):
        inner = offset_polyline(coords, offset - bar_half)
        outer = offset_polyline(coords, offset + bar_half)
        strip = _flat_strip(inner, outer, layers.Z_ROAD_LINE)
        if strip is not None:
            _emit(builder, material, strip[0], strip[1], avoid=avoid)


def _add_markings(
    builder: MeshBuilder,
    material: Material,
    road: Road,
    coords: np.ndarray,
    half_width: float,
    avoid=None,
) -> None:
    spec = ROAD_SPECS[road.road_class]
    z = layers.Z_ROAD_LINE

    # Eixo tracejado: so faz sentido em pista de mao dupla com largura suficiente.
    if spec.markings and not road.oneway and half_width >= 2.6:
        quads = _dashes(coords, line_width=0.14)
        if quads is not None:
            verts, faces = _quads_to_mesh(quads, z)
            _emit(builder, material, verts, faces, avoid=avoid)

    # Faixas de bordo continuas.
    if spec.edge_lines and half_width >= 3.0:
        coords = _densify_coords(builder, coords)
        for side in (-1.0, 1.0):
            distance = side * (half_width - 0.45)
            inner = offset_polyline(coords, distance - 0.06)
            outer = offset_polyline(coords, distance + 0.06)
            strip = _flat_strip(inner, outer, z)
            if strip is not None:
                _emit(builder, material, strip[0], strip[1], avoid=avoid)


# ------------------------------------------------------------------ pipeline


def generate_roads(
    builder: MeshBuilder,
    ctx: GenerationContext,
    roads: list[Road],
    water_union=None,
    building_union=None,
) -> RoadResult:
    if not roads or not ctx.settings.roads:
        return RoadResult()

    segments = ctx.detail["buffer_segments"]
    detail = ctx.settings.detail
    palette = ctx.palette
    materials: dict[str, Material] = {
        "major": palette.asphalt_major,
        "road": palette.asphalt,
        "foot": palette.footway,
        "cobble": palette.cobble,
        "dirt": palette.dirt,
    }
    # Onde a foto informou a cor do pavimento, ela manda: o asfalto gasto de uma
    # cidade pequena nao e o cinza do estilo, e a terra tem a cor do solo dali.
    materials.update(getattr(ctx, "road_materials", None) or {})

    ground_surfaces: dict[str, list] = {key: [] for key in SURFACE_PRIORITY}
    elevated: list[tuple[float, str, object]] = []
    sidewalk_extents: list = []
    marking_jobs: list[tuple[Road, np.ndarray, float]] = []
    crossings: list[tuple[LineString, np.ndarray]] = []
    centerlines: list[tuple[LineString, float]] = []

    for road in roads:
        if road.tunnel:
            continue  # tuneis nao aparecem na superficie
        line: LineString = road.centerline
        if line is None or line.length < 1.0:
            continue

        spec = ROAD_SPECS[road.road_class]
        half = road_half_width(road)
        line = smooth_road(line, half, detail)
        coords = np.asarray(line.coords, dtype=np.float64)

        role = road_role(road)
        if role == "crossing":
            crossings.append((line, coords))
            continue

        try:
            surface = line.buffer(half, quad_segs=segments, cap_style=1, join_style=1)
        except Exception:  # noqa: BLE001 - geometria degenerada
            continue
        if surface.is_empty:
            continue

        if role == "sidewalk":
            # Passeio mapeado: entra na mesma uniao das calcadas geradas, o que
            # evita duas superficies concorrendo na mesma altura.
            sidewalk_extents.append(surface)
            continue

        z = _elevation(road)
        if z > layers.Z_ROAD + 0.01:
            elevated.append((z, road_surface_key(road), surface))
            continue

        ground_surfaces[road_surface_key(road)].append(surface)
        centerlines.append((line, half, road.road_class))

        if ctx.detail["road_markings"] and spec.markings:
            marking_jobs.append((road, coords, half))

        if ctx.settings.sidewalks and spec.sidewalk > 0:
            walk = line.buffer(half + spec.sidewalk, quad_segs=segments, cap_style=1, join_style=1)
            if not walk.is_empty:
                sidewalk_extents.append(walk)

    # --- pistas no solo, em ordem de prioridade ---
    #
    # A geometria e resolvida antes de virar malha porque a ponte precisa entrar
    # no meio: o trecho de pista sobre a agua sai daqui e e substituido por um
    # tabuleiro plano. Assentar primeiro e corrigir depois nao funciona - a
    # pista ja teria descido dentro do leito escavado.
    merged_by_key: dict[str, object] = {}
    covered = None
    for key in SURFACE_PRIORITY:
        parts = ground_surfaces.get(key)
        if not parts:
            continue
        merged = ctx.simplify(unary_union(parts), factor=ROAD_SIMPLIFY)
        if covered is not None and not merged.is_empty:
            try:
                merged = merged.difference(covered)
            except Exception:  # noqa: BLE001 - mantem a superficie cheia
                pass
        if merged.is_empty:
            continue
        merged_by_key[key] = merged
        covered = merged if covered is None else unary_union([covered, merged])

    surface_union = covered

    # --- pontes sobre agua ---
    bridge_union = None
    if ctx.settings.water and water_union is not None and surface_union is not None:
        try:
            bridge_union = build_water_bridges(
                builder,
                ctx,
                surface_union,
                water_union,
                {materials[k]: g for k, g in merged_by_key.items()},
                materials["road"],
            )
        except Exception as exc:  # noqa: BLE001 - sem ponte a cena ainda fecha
            log.warning("Geracao de pontes falhou: %s", exc)

    # --- pistas assentadas, ja sem o vao das pontes ---
    for key in SURFACE_PRIORITY:
        merged = merged_by_key.get(key)
        if merged is None:
            continue
        if bridge_union is not None:
            try:
                merged = merged.difference(bridge_union)
            except Exception:  # noqa: BLE001 - mantem a superficie cheia
                pass
        if merged.is_empty:
            continue
        builder.add_flat(materials[key], merged, layers.Z_ROAD, drape=ctx.draped)

    # --- calcadas: faixa lateral elevada, o degrau ja e o meio-fio ---
    paved_union = surface_union
    if sidewalk_extents and surface_union is not None:
        extent = ctx.simplify(unary_union(sidewalk_extents))
        sidewalk = extent
        # A agua entra no recorte junto com a pista: a faixa de calcada e mais
        # larga que o asfalto, entao sobre um rio ela sobrava para os lados do
        # tabuleiro e mergulhava no leito escavado - a mesma falha da pista, so
        # que sem ponte para substitui-la.
        for cut in (surface_union, building_union, water_union):
            if cut is None or cut.is_empty:
                continue
            try:
                sidewalk = sidewalk.difference(cut)
            except Exception:  # noqa: BLE001
                pass
        if not sidewalk.is_empty:
            if ctx.detail["curb_walls"]:
                builder.add_prism(
                    palette.curb,
                    sidewalk,
                    layers.Z_GROUND,
                    layers.Z_CURB,
                    cap_top=True,
                    top_material=palette.sidewalk,
                    drape=ctx.draped,
                )
            else:
                # O degrau do meio-fio tem 12 cm. As paredes verticais dele
                # custavam 215 mil triangulos em Araioses - 41% da cena inteira -
                # porque o perimetro da calcada acompanha cada reentrancia de
                # cada casa e de cada esquina. A calcada vira uma superficie no
                # nivel do degrau: some a parede, fica a faixa.
                builder.add_flat(
                    palette.sidewalk, sidewalk, layers.Z_CURB, drape=ctx.draped
                )
        paved_union = extent

    # --- sinalizacao horizontal ---
    #
    # A pintura e assentada no relevo, e sobre um rio o relevo e o leito
    # escavado. Sem este veto ela desce dentro da agua junto com o terreno,
    # enquanto a pista ja subiu para a ponte.
    sem_pintura = water_union if ctx.settings.water else None
    for road, coords, half in marking_jobs:
        try:
            _add_markings(builder, palette.road_line, road, coords, half, avoid=sem_pintura)
        except Exception as exc:  # noqa: BLE001 - sinalizacao e cosmetica
            log.debug("Sinalizacao ignorada em %s: %s", road.osm_id, exc)

    if ctx.detail["road_markings"]:
        for line, coords in crossings:
            try:
                _add_crossing(builder, palette.road_line, line, coords, avoid=sem_pintura)
            except Exception as exc:  # noqa: BLE001
                log.debug("Faixa de pedestre ignorada: %s", exc)

    # --- viadutos e pontes tagueadas ---
    for z, key, surface in elevated:
        simplified = ctx.simplify(surface)
        builder.add_prism(
            palette.curb,
            simplified,
            z - layers.Z_BRIDGE_DECK,
            z,
            cap_top=True,
            cap_bottom=True,
            top_material=materials[key],
            drape=ctx.draped,
        )
    if elevated:
        try:
            build_viaduct_supports(builder, ctx, elevated)
        except Exception as exc:  # noqa: BLE001 - o tabuleiro ja esta desenhado
            log.warning("Pilares de viaduto falharam: %s", exc)

    return RoadResult(
        surface_union=surface_union, paved_union=paved_union, centerlines=centerlines
    )


def generate_railways(builder: MeshBuilder, ctx: GenerationContext, railways) -> None:
    """Ferrovias: leito de lastro com dois trilhos elevados."""
    if not railways:
        return
    segments = ctx.detail["buffer_segments"]
    palette = ctx.palette
    detail = ctx.settings.detail

    beds, rails = [], []
    for railway in railways:
        line = railway.centerline
        if line is None or line.length < 2.0:
            continue
        line = smooth_road(line, railway.gauge_width, detail)
        beds.append(line.buffer(railway.gauge_width, quad_segs=segments, cap_style=2))
        coords = np.asarray(line.coords, dtype=np.float64)
        for offset in (-0.72, 0.72):
            rail_line = LineString(offset_polyline(coords, offset))
            rails.append(rail_line.buffer(0.08, quad_segs=1, cap_style=2))

    if beds:
        builder.add_flat(
            palette.dirt, ctx.simplify(unary_union(beds)), layers.Z_RAIL_BED, drape=ctx.draped
        )
    if rails:
        builder.add_prism(
            palette.rail,
            unary_union(rails),
            layers.Z_RAIL_BED,
            layers.Z_RAIL,
            drape=ctx.draped,
        )


