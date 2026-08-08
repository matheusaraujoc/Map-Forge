"""Geracao procedural dos edificios.

Cada poligono do OSM vira uma construcao: classificacao por area, altura
(das tags quando existem, procedural quando nao), extrusao, telhado e aberturas.
Tudo derivado da seed, de modo que o mesmo projeto sempre gera a mesma cidade.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from shapely.geometry import Polygon

from ..core.features import Building, BuildingClass
from ..core.mesh import Material, MeshBuilder
from . import layers
from .context import GenerationContext

log = logging.getLogger(__name__)

LEVEL_HEIGHT = 3.1
GROUND_LEVEL_HEIGHT = 3.9

# Faixa de pavimentos por porte da construcao.
CLASS_LEVELS: dict[BuildingClass, tuple[int, int]] = {
    BuildingClass.HOUSE: (1, 2),
    BuildingClass.TOWNHOUSE: (2, 4),
    BuildingClass.BLOCK: (4, 14),
    BuildingClass.MALL: (1, 3),
}

# Faixa para contorno detectado na imagem, onde a area nao e informativa.
DETECTED_LEVELS = (1, 2)

# building=* que sobrescreve a faixa vinda da area.
TYPE_LEVELS: dict[str, tuple[int, int]] = {
    "house": (1, 2),
    "detached": (1, 2),
    "bungalow": (1, 1),
    "hut": (1, 1),
    "shed": (1, 1),
    "garage": (1, 1),
    "garages": (1, 1),
    "carport": (1, 1),
    "roof": (1, 1),
    "terrace": (2, 3),
    "semidetached_house": (1, 2),
    "residential": (2, 6),
    "apartments": (4, 12),
    "dormitory": (3, 8),
    "hotel": (5, 16),
    "commercial": (2, 8),
    "office": (5, 20),
    "retail": (1, 3),
    "supermarket": (1, 2),
    "kiosk": (1, 1),
    "industrial": (1, 3),
    "warehouse": (1, 2),
    "hangar": (1, 1),
    "school": (1, 4),
    "university": (2, 6),
    "hospital": (3, 10),
    "church": (1, 2),
    "chapel": (1, 1),
    "cathedral": (2, 3),
    "mosque": (1, 2),
    "temple": (1, 2),
    "stadium": (1, 3),
    "train_station": (1, 3),
    "civic": (2, 5),
    "public": (2, 5),
}

# Tipos com telhado sempre plano (galpoes, lajes, estruturas grandes).
FLAT_ROOF_TYPES = {
    "industrial",
    "warehouse",
    "hangar",
    "retail",
    "supermarket",
    "commercial",
    "office",
    "roof",
    "parking",
    "stadium",
    "hospital",
    "apartments",
}


def _to_number(value) -> float | None:
    """Le um numero de uma tag do OSM ('12', '12 m', '12.5')."""
    if not value:
        return None
    import re

    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _choose_levels(building: Building, ctx: GenerationContext, rng) -> float:
    """Numero de pavimentos: tags do OSM primeiro, senao procedural."""
    if building.levels and building.levels >= 1:
        return float(building.levels)

    if building.tags.get("source") == "deteccao":
        # Contorno vindo da deteccao por imagem: a area nao diz nada sobre
        # altura, porque manchas vizinhas se fundem. Classificar por area faria
        # um quarteirao de casas terreas virar torre de dez andares.
        low, high = DETECTED_LEVELS
    else:
        low, high = TYPE_LEVELS.get(
            building.building_type, CLASS_LEVELS[building.classify()]
        )

    # Constroi um viés: quanto maior a area dentro da faixa da classe, mais alto.
    weight = rng.beta(2.0, 2.6)
    levels = low + weight * (high - low)
    return max(1.0, round(levels))


def _building_height(building: Building, ctx: GenerationContext, rng) -> tuple[float, float]:
    """Retorna (altura_total, altura_do_pavimento).

    Ordem de confianca: tag do OSM > sombra medida na imagem > procedural.
    """
    scale = ctx.style.height_scale * ctx.settings.building_height_scale

    if building.height and building.height > 1.0:
        return building.height * scale, LEVEL_HEIGHT

    measured = ctx.shadow_heights.get(building.osm_id)
    if measured and measured > 2.0:
        return measured * scale, LEVEL_HEIGHT

    levels = _choose_levels(building, ctx, rng)
    height = GROUND_LEVEL_HEIGHT + (levels - 1) * LEVEL_HEIGHT
    # Pequena variacao para a silhueta nao ficar mecanica.
    height *= 1.0 + rng.uniform(-0.06, 0.06)
    return max(2.5, height * scale), LEVEL_HEIGHT


def _rectangularity(poly: Polygon) -> float:
    try:
        obb = poly.minimum_rotated_rectangle
    except Exception:  # noqa: BLE001
        return 0.0
    return poly.area / obb.area if obb.area > 0 else 0.0


def _convexity(poly: Polygon) -> float:
    hull = poly.convex_hull
    return poly.area / hull.area if hull.area > 0 else 0.0


# roof:shape do OSM -> forma que sabemos construir.
ROOF_SHAPE_MAP = {
    "flat": "flat",
    "gabled": "gable",
    "hipped": "hip",
    "pyramidal": "hip",
    "half-hipped": "gable",
    "gambrel": "mansard",
    "mansard": "mansard",
    "skillion": "skillion",
    "lean_to": "skillion",
    "shed": "skillion",
    "monopitch": "skillion",
    "dome": "hip",
    "round": "hip",
    "onion": "hip",
    "sawtooth": "flat",
    "cone": "hip",
}


def _pick_roof(building: Building, ctx: GenerationContext, rng, poly: Polygon) -> str:
    """Forma do telhado: tag do OSM quando existe, senao sorteio pelo estilo.

    A escolha e filtrada pela forma do contorno: duas aguas so fecha bem em
    planta retangular, e piramidal so em planta convexa. Quando nao fecha, cai
    para a proxima forma que fecha - por isso um L nunca sai com cumeeira torta.
    """
    tagged = ROOF_SHAPE_MAP.get((building.tags.get("roof:shape") or "").lower())
    if tagged:
        choice = tagged
    elif building.building_type in FLAT_ROOF_TYPES:
        return "flat"
    elif poly.area > 1200:
        return "flat"
    else:
        weights = ctx.style.roof_weights
        options = [k for k, w in weights.items() if w > 0]
        if not options:
            return "flat"
        probs = np.array([weights[k] for k in options], dtype=float)
        probs /= probs.sum()
        choice = options[int(rng.choice(len(options), p=probs))]

    rect = _rectangularity(poly)
    if choice in {"gable", "mansard", "skillion"} and rect < 0.72:
        choice = "hip"
    if choice == "hip" and _convexity(poly) < 0.86:
        choice = "flat"
    return choice


# ---------------------------------------------------------------- primitivas


def _add_face(builder: MeshBuilder, material: Material, points, ref_normal) -> None:
    """Adiciona um poligono 3D convexo (leque de triangulos) orientado por ref_normal."""
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) < 3:
        return
    normal = np.cross(pts[1] - pts[0], pts[2] - pts[0])
    if np.dot(normal, ref_normal) < 0:
        pts = pts[::-1]
    faces = np.array([[0, i, i + 1] for i in range(1, len(pts) - 1)], dtype=np.int64)
    builder.add_mesh(material, pts, faces)


def _flat_roof(
    builder: MeshBuilder,
    ctx: GenerationContext,
    poly: Polygon,
    z: float,
    roof_mat: Material,
    wall_mat: Material,
    parapet: bool,
) -> float:
    """Laje. Com platibanda quando o predio e grande o bastante para justificar."""
    if parapet and ctx.detail["roof_detail"]:
        inner = poly.buffer(-0.4)
        if not inner.is_empty and inner.area > poly.area * 0.35:
            ring = poly.difference(inner)
            builder.add_flat(roof_mat, inner, z)
            builder.add_prism(wall_mat, ring, z, z + 0.7, cap_top=True, top_material=wall_mat)
            return z + 0.7
    builder.add_flat(roof_mat, poly, z)
    return z


def _hip_roof(
    builder: MeshBuilder,
    poly: Polygon,
    z: float,
    height: float,
    roof_mat: Material,
) -> float:
    """Telhado piramidal: apice sobre o centroide, uma agua por aresta."""
    coords = np.asarray(poly.exterior.coords, dtype=np.float64)[:-1]
    if len(coords) < 3:
        return z
    center = np.asarray(poly.representative_point().coords[0], dtype=np.float64)
    apex = np.array([center[0], center[1], z + height])

    verts = [apex]
    faces = []
    base = np.column_stack([coords, np.full(len(coords), z)])
    verts.extend(base)
    n = len(coords)
    for i in range(n):
        a, b = 1 + i, 1 + (i + 1) % n
        tri = np.array([verts[0], verts[a], verts[b]])
        normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        faces.append([0, a, b] if normal[2] >= 0 else [0, b, a])
    builder.add_mesh(roof_mat, np.array(verts), np.array(faces, dtype=np.int64))
    return z + height


def _gable_roof(
    builder: MeshBuilder,
    poly: Polygon,
    z: float,
    height: float,
    roof_mat: Material,
    wall_mat: Material,
) -> float:
    """Duas aguas sobre a caixa orientada minima; as pontas viram empenas."""
    obb = poly.minimum_rotated_rectangle
    if not isinstance(obb, Polygon):
        return z
    corners = np.asarray(obb.exterior.coords, dtype=np.float64)[:-1]
    if len(corners) != 4:
        return z

    # Reordena para que a → b seja o lado longo (a cumeeira fica paralela a ele).
    if np.linalg.norm(corners[1] - corners[0]) < np.linalg.norm(corners[2] - corners[1]):
        corners = np.roll(corners, -1, axis=0)
    a, b, c, d = corners

    def at(point, height_z):
        return np.array([point[0], point[1], height_z])

    ridge0 = at((a + d) / 2.0, z + height)
    ridge1 = at((b + c) / 2.0, z + height)
    up = np.array([0.0, 0.0, 1.0])

    _add_face(builder, roof_mat, [at(a, z), at(b, z), ridge1, ridge0], up)
    _add_face(builder, roof_mat, [at(c, z), at(d, z), ridge0, ridge1], up)

    # Empenas: normal apontando para fora do volume.
    out1 = np.array([b[0] - a[0], b[1] - a[1], 0.0])
    out1 = out1 / (np.linalg.norm(out1) or 1.0)
    _add_face(builder, wall_mat, [at(b, z), at(c, z), ridge1], out1)
    _add_face(builder, wall_mat, [at(d, z), at(a, z), ridge0], -out1)
    return z + height


def _mansard_roof(
    builder: MeshBuilder,
    poly: Polygon,
    z: float,
    height: float,
    roof_mat: Material,
) -> float:
    """Mansarda: saia inclinada ingreme e um terraco no topo.

    Feita como dois troncos: contorno cheio -> contorno recuado (saia), depois
    tampa plana. Da a silhueta de sobrado antigo sem custo de triangulos.
    """
    inset = min(np.sqrt(poly.area) * 0.18, 2.6)
    top = poly.buffer(-inset)
    if top.is_empty or not isinstance(top, Polygon) or top.area < poly.area * 0.2:
        return _hip_roof(builder, poly, z, height, roof_mat)

    skirt_h = height * 0.72
    _add_loft(builder, roof_mat, poly, top, z, z + skirt_h)
    builder.add_flat(roof_mat, top, z + skirt_h)
    return z + skirt_h


def _add_loft(
    builder: MeshBuilder,
    material: Material,
    lower: Polygon,
    upper: Polygon,
    lower_z: float,
    upper_z: float,
) -> None:
    """Superficie inclinada entre dois contornos concentricos.

    Usa o vertice mais proximo do anel de cima para cada aresta de baixo. E uma
    aproximacao, mas os dois aneis vem de um buffer do mesmo poligono, entao a
    correspondencia e boa o bastante e nunca cruza.
    """
    low = np.asarray(lower.exterior.coords, dtype=np.float64)[:-1]
    high = np.asarray(upper.exterior.coords, dtype=np.float64)[:-1]
    if len(low) < 3 or len(high) < 3:
        return

    # Para cada vertice de baixo, o mais proximo em cima.
    deltas = low[:, None, :] - high[None, :, :]
    nearest = np.argmin((deltas**2).sum(axis=2), axis=1)

    vertices, faces = [], []
    for i in range(len(low)):
        j = (i + 1) % len(low)
        a, b = low[i], low[j]
        c, d = high[nearest[j]], high[nearest[i]]
        base = len(vertices)
        vertices.extend(
            [
                [a[0], a[1], lower_z],
                [b[0], b[1], lower_z],
                [c[0], c[1], upper_z],
                [d[0], d[1], upper_z],
            ]
        )
        faces.extend([[base, base + 1, base + 2], [base, base + 2, base + 3]])

    if not faces:
        return
    vertices = np.array(vertices)
    faces = np.array(faces, dtype=np.int64)
    # Orienta para fora: normal com componente horizontal saindo do centroide.
    center = np.array([lower.centroid.x, lower.centroid.y])
    tri = vertices[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    outward = tri[:, :, :2].mean(axis=1) - center
    flip = (normals[:, :2] * outward).sum(axis=1) < 0
    faces[flip] = faces[flip][:, [0, 2, 1]]
    builder.add_mesh(material, vertices, faces)


def _skillion_roof(
    builder: MeshBuilder,
    poly: Polygon,
    z: float,
    height: float,
    roof_mat: Material,
    wall_mat: Material,
    rng,
) -> float:
    """Uma agua so, caindo para um dos lados da caixa orientada."""
    obb = poly.minimum_rotated_rectangle
    if not isinstance(obb, Polygon):
        return z
    corners = np.asarray(obb.exterior.coords, dtype=np.float64)[:-1]
    if len(corners) != 4:
        return z
    if np.linalg.norm(corners[1] - corners[0]) < np.linalg.norm(corners[2] - corners[1]):
        corners = np.roll(corners, -1, axis=0)
    a, b, c, d = corners
    if rng.random() < 0.5:  # de que lado a agua desce
        a, b, c, d = c, d, a, b

    def at(point, height_z):
        return np.array([point[0], point[1], height_z])

    up = np.array([0.0, 0.0, 1.0])
    high_z = z + height
    _add_face(builder, roof_mat, [at(a, z), at(b, z), at(c, high_z), at(d, high_z)], up)

    # Empenas triangulares nas duas laterais.
    side = np.array([c[0] - b[0], c[1] - b[1], 0.0])
    norm = np.linalg.norm(side)
    if norm > 1e-6:
        side /= norm
        _add_face(builder, wall_mat, [at(b, z), at(c, high_z), at(c, z)], side)
        _add_face(builder, wall_mat, [at(d, z), at(a, z), at(d, high_z)], -side)
    return high_z


def _add_plinth(
    builder: MeshBuilder,
    poly: Polygon,
    base_z: float,
    material: Material,
) -> None:
    """Embasamento: faixa saliente rente ao chao, como tem quase toda casa."""
    skirt = poly.buffer(0.18)
    if skirt.is_empty or not isinstance(skirt, Polygon):
        return
    builder.add_prism(material, skirt, base_z, base_z + 0.45, cap_top=True)


def _add_balconies(
    builder: MeshBuilder,
    poly: Polygon,
    base_z: float,
    top_z: float,
    level_height: float,
    material: Material,
    rng,
) -> None:
    """Sacadas na fachada mais longa, uma por pavimento.

    So em predio alto o bastante para ter varanda; e o detalhe que tira a cara
    de caixa lisa de quem tem muitos andares.
    """
    coords = np.asarray(poly.exterior.coords, dtype=np.float64)
    edges = coords[1:] - coords[:-1]
    lengths = np.hypot(edges[:, 0], edges[:, 1])
    if len(lengths) == 0:
        return
    i = int(np.argmax(lengths))
    if lengths[i] < 7.0:
        return

    direction = edges[i] / lengths[i]
    normal = np.array([direction[1], -direction[0]])
    depth = 1.1
    width = min(lengths[i] * 0.55, 8.0)
    center = coords[i] + edges[i] * 0.5

    levels = int((top_z - base_z) // level_height)
    for level in range(1, min(levels, 24)):
        z0 = base_z + level * level_height + 0.1
        if z0 + 1.0 > top_z - 0.5:
            break
        inner_a = center - direction * (width / 2)
        inner_b = center + direction * (width / 2)
        outer_a = inner_a + normal * depth
        outer_b = inner_b + normal * depth
        slab = Polygon([inner_a, inner_b, outer_b, outer_a])
        if slab.is_valid and not slab.is_empty:
            builder.add_prism(material, slab, z0, z0 + 0.22, cap_top=True, cap_bottom=True)
            # Guarda-corpo.
            rail = Polygon(
                [
                    outer_a,
                    outer_b,
                    outer_b - normal * 0.12,
                    outer_a - normal * 0.12,
                ]
            )
            if rail.is_valid and not rail.is_empty:
                builder.add_prism(material, rail, z0 + 0.22, z0 + 1.15, cap_top=True)


def _add_chimney(
    builder: MeshBuilder,
    poly: Polygon,
    roof_z: float,
    material: Material,
    rng,
) -> None:
    """Chamine numa casa de telhado inclinado."""
    point = poly.representative_point()
    offset = np.sqrt(poly.area) * 0.22
    angle = rng.uniform(0, 2 * np.pi)
    cx = point.x + np.cos(angle) * offset
    cy = point.y + np.sin(angle) * offset
    half = rng.uniform(0.28, 0.42)
    stack = Polygon(
        [(cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half), (cx - half, cy + half)]
    )
    builder.add_prism(material, stack, roof_z - 0.8, roof_z + rng.uniform(0.9, 1.8), cap_top=True)


def _add_windows(
    builder: MeshBuilder,
    ctx: GenerationContext,
    poly: Polygon,
    base_z: float,
    top_z: float,
    level_height: float,
    window_mat: Material,
    rng,
) -> None:
    """Janelas como placas finas rentes a fachada, uma faixa por pavimento."""
    total = top_z - base_z
    n_levels = int(total // level_height)
    if n_levels < 1:
        return
    n_levels = min(n_levels, 30)

    spacing = rng.uniform(3.0, 3.8)
    win_w = 1.15
    win_h = 1.45
    offset = 0.07

    coords = np.asarray(poly.exterior.coords, dtype=np.float64)
    quads: list[np.ndarray] = []

    for i in range(len(coords) - 1):
        p0, p1 = coords[i], coords[i + 1]
        edge = p1 - p0
        length = float(np.hypot(*edge))
        if length < 2.2:
            continue
        direction = edge / length
        normal = np.array([direction[1], -direction[0]])  # anel CCW -> aponta para fora

        count = int((length - 1.6) // spacing)
        if count < 1:
            continue
        margin = (length - (count - 1) * spacing) / 2.0 if count > 1 else length / 2.0

        for w in range(count):
            center = p0 + direction * (margin + w * spacing) + normal * offset
            left = center - direction * (win_w / 2.0)
            right = center + direction * (win_w / 2.0)
            for level in range(n_levels):
                z0 = base_z + level * level_height + 1.0
                z1 = z0 + win_h
                if z1 > top_z - 0.4:
                    break
                quads.append(
                    np.array(
                        [
                            [left[0], left[1], z0],
                            [right[0], right[1], z0],
                            [right[0], right[1], z1],
                            [left[0], left[1], z1],
                        ]
                    )
                )

    if not quads:
        return
    verts = np.concatenate(quads, axis=0)
    base_idx = np.arange(len(quads)) * 4
    faces = np.concatenate(
        [
            np.column_stack([base_idx, base_idx + 1, base_idx + 2]),
            np.column_stack([base_idx, base_idx + 2, base_idx + 3]),
        ],
        axis=0,
    )
    builder.add_mesh(window_mat, verts, faces)


def _add_door(
    builder: MeshBuilder,
    poly: Polygon,
    base_z: float,
    door_mat: Material,
) -> None:
    """Uma porta na aresta mais longa, rente a fachada."""
    coords = np.asarray(poly.exterior.coords, dtype=np.float64)
    edges = coords[1:] - coords[:-1]
    lengths = np.hypot(edges[:, 0], edges[:, 1])
    if len(lengths) == 0:
        return
    i = int(np.argmax(lengths))
    if lengths[i] < 3.0:
        return
    direction = edges[i] / lengths[i]
    normal = np.array([direction[1], -direction[0]])
    center = coords[i] + edges[i] * 0.5 + normal * 0.08
    half = 0.55
    left = center - direction * half
    right = center + direction * half
    z0, z1 = base_z + 0.02, base_z + 2.15
    builder.add_quad(
        door_mat,
        np.array([left[0], left[1], z0]),
        np.array([right[0], right[1], z0]),
        np.array([right[0], right[1], z1]),
        np.array([left[0], left[1], z1]),
    )


# ------------------------------------------------------------------ pipeline


def generate_buildings(
    builder: MeshBuilder,
    ctx: GenerationContext,
    buildings: list[Building],
) -> int:
    if not ctx.settings.buildings or not buildings:
        return 0

    palette = ctx.palette
    min_area = ctx.detail["min_building_area"]
    do_windows = ctx.detail["windows"]
    n_walls = palette.wall_count()
    n_roofs = palette.roof_count()
    generated = 0
    total = len(buildings)

    for index, building in enumerate(buildings):
        poly: Polygon = building.footprint
        if poly is None or poly.is_empty or poly.area < min_area:
            continue

        poly = ctx.simplify(poly)
        if poly.is_empty or not isinstance(poly, Polygon):
            continue

        rng = ctx.rng(building.osm_id, int(poly.area))
        wall_mat = palette.wall(int(rng.integers(0, n_walls)))
        # A cor amostrada do satelite tem prioridade sobre a paleta do estilo.
        roof_mat = ctx.roof_materials.get(building.osm_id) or palette.roof(
            int(rng.integers(0, n_roofs))
        )

        height, level_height = _building_height(building, ctx, rng)
        base_z = layers.Z_GROUND + building.min_height
        if ctx.draped:
            # O predio e rigido: assenta no ponto mais baixo do contorno e enterra
            # um pouco, senao numa encosta ele fica sobre palafitas de um lado.
            base_z += ctx.terrain.min_over(poly) - 0.6
        wall_top = base_z + height

        builder.add_walls(wall_mat, poly, base_z, wall_top)

        roof = _pick_roof(building, ctx, rng, poly)
        # roof:height / roof:levels do OSM dao a altura da cobertura direto.
        tagged_pitch = _to_number(building.tags.get("roof:height"))
        if tagged_pitch is None:
            levels = _to_number(building.tags.get("roof:levels"))
            tagged_pitch = levels * LEVEL_HEIGHT if levels else None

        roof_top = wall_top
        if roof == "gable":
            pitch = tagged_pitch or min(0.42 * math.sqrt(poly.area), 4.2)
            roof_top = _gable_roof(builder, poly, wall_top, max(1.4, pitch), roof_mat, wall_mat)
        elif roof == "hip":
            pitch = tagged_pitch or min(0.34 * math.sqrt(poly.area), 3.6)
            roof_top = _hip_roof(builder, poly, wall_top, max(1.2, pitch), roof_mat)
        elif roof == "mansard":
            pitch = tagged_pitch or min(0.40 * math.sqrt(poly.area), 3.8)
            roof_top = _mansard_roof(builder, poly, wall_top, max(1.6, pitch), roof_mat)
        elif roof == "skillion":
            pitch = tagged_pitch or min(0.30 * math.sqrt(poly.area), 3.0)
            roof_top = _skillion_roof(
                builder, poly, wall_top, max(1.0, pitch), roof_mat, wall_mat, rng
            )
        else:
            parapet = poly.area > 120 and height > 7.0
            roof_top = _flat_roof(builder, ctx, poly, wall_top, roof_mat, wall_mat, parapet)

        if do_windows and poly.area >= 30:
            _add_windows(
                builder, ctx, poly, base_z, wall_top, level_height, palette.window, rng
            )

        if ctx.detail["roof_detail"]:
            _add_door(builder, poly, base_z, palette.door)
            if ctx.detail.get("building_detail"):
                if height > 4.0 and poly.area > 45:
                    _add_plinth(builder, poly, base_z, palette.curb)
                if roof in {"gable", "hip"} and poly.area < 400 and rng.random() < 0.45:
                    _add_chimney(builder, poly, roof_top, wall_mat, rng)
                if height > 14.0 and poly.area > 150 and rng.random() < 0.55:
                    _add_balconies(
                        builder, poly, base_z, wall_top, level_height, wall_mat, rng
                    )

        generated += 1
        if index % 400 == 0:
            ctx.report(f"edificios {index}/{total}", index / max(total, 1))

    return generated
