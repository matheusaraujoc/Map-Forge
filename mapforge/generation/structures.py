"""Estruturas verticais isoladas: torres, silos, postes.

Sao os elementos que dao leitura de "cidade habitada" sem serem edificios. A
posicao vem do OSM, nao e inventada - onde o mapa diz que ha uma torre de
telefonia, ha uma torre de telefonia.

A separacao entre urbano e rural nao e uma regra escrita aqui: ela cai
naturalmente do dado. Silo e moinho so existem onde alguem mapeou lavoura;
antena e outdoor so existem onde ha cidade.
"""

from __future__ import annotations

import logging

import numpy as np
from shapely.geometry import Polygon

from ..core.features import StructureKind
from ..core.mesh import Material, MeshBuilder
from . import layers
from .context import GenerationContext

log = logging.getLogger(__name__)

# Altura padrao (metros) quando o OSM nao informa.
DEFAULT_HEIGHT = {
    StructureKind.MAST: 42.0,
    StructureKind.WATER_TOWER: 18.0,
    StructureKind.SILO: 12.0,
    StructureKind.CHIMNEY: 28.0,
    StructureKind.PYLON: 32.0,
    StructureKind.POLE: 9.0,
    StructureKind.WINDMILL: 16.0,
    StructureKind.BILLBOARD: 7.0,
}

# Raio padrao da base, quando nao ha contorno mapeado.
DEFAULT_RADIUS = {
    StructureKind.MAST: 1.4,
    StructureKind.WATER_TOWER: 2.6,
    StructureKind.SILO: 3.4,
    StructureKind.CHIMNEY: 1.8,
    StructureKind.PYLON: 3.0,
    StructureKind.POLE: 0.16,
    StructureKind.WINDMILL: 2.4,
    StructureKind.BILLBOARD: 0.3,
}


def _ngon(center, radius: float, sides: int = 8) -> Polygon:
    angles = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    return Polygon(
        np.column_stack([center[0] + np.cos(angles) * radius, center[1] + np.sin(angles) * radius])
    )


def _square(center, half: float) -> Polygon:
    return Polygon(
        [
            (center[0] - half, center[1] - half),
            (center[0] + half, center[1] - half),
            (center[0] + half, center[1] + half),
            (center[0] - half, center[1] + half),
        ]
    )


def _lattice(
    builder: MeshBuilder,
    material: Material,
    center,
    base_z: float,
    height: float,
    base_half: float,
    top_half: float,
    segments: int = 4,
) -> None:
    """Torre trelicada: quatro montantes que convergem para o topo.

    Feita com prismas finos em vez de trelica de verdade - de longe le igual e
    custa duas ordens de grandeza menos triangulos.
    """
    for level in range(segments):
        t0 = level / segments
        t1 = (level + 1) / segments
        half0 = base_half + (top_half - base_half) * t0
        half1 = base_half + (top_half - base_half) * t1
        z0 = base_z + height * t0
        z1 = base_z + height * t1

        # Quatro montantes inclinados, um por canto.
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            a = np.array([center[0] + sx * half0, center[1] + sy * half0])
            b = np.array([center[0] + sx * half1, center[1] + sy * half1])
            _tapered_post(builder, material, a, b, z0, z1, 0.16)

        # Anel horizontal marcando o modulo.
        ring_half = half1
        ring = _square(center, ring_half)
        inner = _square(center, max(ring_half - 0.18, 0.02))
        band = ring.difference(inner)
        if not band.is_empty:
            builder.add_prism(material, band, z1 - 0.18, z1, cap_top=True)


def _tapered_post(
    builder: MeshBuilder,
    material: Material,
    a: np.ndarray,
    b: np.ndarray,
    z0: float,
    z1: float,
    thickness: float,
) -> None:
    """Montante inclinado entre dois pontos, como uma caixa fina torcida."""
    half = thickness / 2.0
    corners_a = np.array([[-half, -half], [half, -half], [half, half], [-half, half]])
    bottom = np.column_stack([a + corners_a, np.full(4, z0)])
    top = np.column_stack([b + corners_a, np.full(4, z1)])
    vertices = np.concatenate([bottom, top], axis=0)

    idx = np.arange(4)
    nxt = (idx + 1) % 4
    faces = np.concatenate(
        [
            np.column_stack([idx, nxt, nxt + 4]),
            np.column_stack([idx, nxt + 4, idx + 4]),
        ],
        axis=0,
    )
    builder.add_mesh(material, vertices, faces)


# ------------------------------------------------------------------ desenhos


def _mast(builder, palette, center, base_z, height, radius) -> None:
    """Torre de telefonia: trelica com antenas no topo."""
    _lattice(builder, palette.rail, center, base_z, height * 0.92, radius, radius * 0.45)

    # Plataforma e antenas setoriais.
    top = base_z + height * 0.92
    platform = _ngon(center, radius * 0.75, sides=6)
    builder.add_prism(palette.rail, platform, top, top + 0.25, cap_top=True)
    for angle in (0.0, 2 * np.pi / 3, 4 * np.pi / 3):
        offset = np.array([np.cos(angle), np.sin(angle)]) * radius * 0.7
        panel = _square(center + offset, 0.22)
        builder.add_prism(palette.curb, panel, top + 0.25, top + height * 0.08 + 1.4, cap_top=True)


def _water_tower(builder, palette, center, base_z, height, radius) -> None:
    """Caixa d'agua elevada: reservatorio sobre pernas.

    Muito comum em cidade pequena brasileira, e um marco visual da silhueta.
    """
    leg_top = base_z + height * 0.62
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        a = np.array([center[0] + sx * radius * 0.8, center[1] + sy * radius * 0.8])
        b = np.array([center[0] + sx * radius * 0.42, center[1] + sy * radius * 0.42])
        _tapered_post(builder, palette.rail, a, b, base_z, leg_top, 0.28)

    tank = _ngon(center, radius, sides=10)
    builder.add_prism(palette.sidewalk, tank, leg_top, base_z + height, cap_top=True)
    # Cinta no meio do reservatorio.
    builder.add_prism(
        palette.rail, tank.buffer(0.12), leg_top + (height * 0.38) * 0.5, leg_top + (height * 0.38) * 0.5 + 0.2
    )


def _silo(builder, palette, center, base_z, height, radius) -> None:
    """Silo ou tanque: cilindro com topo conico."""
    body = _ngon(center, radius, sides=12)
    shoulder = base_z + height * 0.82
    builder.add_prism(palette.sidewalk, body, base_z, shoulder, cap_top=False)

    # Cone de fechamento.
    coords = np.asarray(body.exterior.coords, dtype=np.float64)[:-1]
    apex = np.array([center[0], center[1], base_z + height])
    verts = [apex] + [[x, y, shoulder] for x, y in coords]
    faces = []
    n = len(coords)
    for i in range(n):
        faces.append([0, 1 + i, 1 + (i + 1) % n])
    tri = np.array(verts)
    face_array = np.array(faces, dtype=np.int64)
    normals = np.cross(
        tri[face_array[:, 1]] - tri[face_array[:, 0]],
        tri[face_array[:, 2]] - tri[face_array[:, 0]],
    )
    face_array[normals[:, 2] < 0] = face_array[normals[:, 2] < 0][:, [0, 2, 1]]
    builder.add_mesh(palette.rail, tri, face_array)


def _chimney(builder, palette, center, base_z, height, radius) -> None:
    """Chamine industrial: tronco de cone alto."""
    base = _ngon(center, radius, sides=10)
    top = _ngon(center, radius * 0.55, sides=10)
    steps = 4
    for i in range(steps):
        t0, t1 = i / steps, (i + 1) / steps
        r0 = radius + (radius * 0.55 - radius) * t0
        r1 = radius + (radius * 0.55 - radius) * t1
        builder.add_prism(
            palette.curb,
            _ngon(center, (r0 + r1) / 2, sides=10),
            base_z + height * t0,
            base_z + height * t1,
            cap_top=(i == steps - 1),
        )
    del base, top


def _pylon(builder, palette, center, base_z, height, radius) -> None:
    """Torre de transmissao: trelica com dois bracos."""
    _lattice(builder, palette.rail, center, base_z, height, radius, radius * 0.35, segments=5)
    for level, span in ((0.62, 1.0), (0.82, 0.75)):
        z = base_z + height * level
        arm = Polygon(
            [
                (center[0] - radius * 3.0 * span, center[1] - 0.22),
                (center[0] + radius * 3.0 * span, center[1] - 0.22),
                (center[0] + radius * 3.0 * span, center[1] + 0.22),
                (center[0] - radius * 3.0 * span, center[1] + 0.22),
            ]
        )
        builder.add_prism(palette.rail, arm, z, z + 0.3, cap_top=True)


def _pole(builder, palette, center, base_z, height, radius) -> None:
    """Poste de energia, com braco."""
    builder.add_prism(palette.trunk, _ngon(center, radius, sides=6), base_z, base_z + height)
    arm = Polygon(
        [
            (center[0] - 0.9, center[1] - 0.08),
            (center[0] + 0.9, center[1] - 0.08),
            (center[0] + 0.9, center[1] + 0.08),
            (center[0] - 0.9, center[1] + 0.08),
        ]
    )
    builder.add_prism(palette.trunk, arm, base_z + height - 0.6, base_z + height - 0.45, cap_top=True)


def _windmill(builder, palette, center, base_z, height, radius) -> None:
    """Cata-vento de bombeamento, tipico de sitio."""
    _lattice(builder, palette.rail, center, base_z, height * 0.85, radius, radius * 0.3, segments=3)
    top = base_z + height * 0.85
    hub = _ngon(center, radius * 0.35, sides=8)
    builder.add_prism(palette.rail, hub, top, top + 0.4, cap_top=True)
    # Roda: um disco fino na vertical, aproximado por uma placa.
    blade = Polygon(
        [
            (center[0] - radius * 1.6, center[1] - 0.08),
            (center[0] + radius * 1.6, center[1] - 0.08),
            (center[0] + radius * 1.6, center[1] + 0.08),
            (center[0] - radius * 1.6, center[1] + 0.08),
        ]
    )
    builder.add_prism(palette.sidewalk, blade, top - radius * 1.2, top + radius * 1.2, cap_top=True)


def _billboard(builder, palette, center, base_z, height, radius) -> None:
    """Outdoor: painel sobre um mastro."""
    builder.add_prism(palette.rail, _ngon(center, radius, sides=6), base_z, base_z + height * 0.55)
    panel = Polygon(
        [
            (center[0] - 3.2, center[1] - 0.12),
            (center[0] + 3.2, center[1] - 0.12),
            (center[0] + 3.2, center[1] + 0.12),
            (center[0] - 3.2, center[1] + 0.12),
        ]
    )
    builder.add_prism(
        palette.sidewalk, panel, base_z + height * 0.55, base_z + height, cap_top=True
    )


BUILDERS = {
    StructureKind.MAST: _mast,
    StructureKind.WATER_TOWER: _water_tower,
    StructureKind.SILO: _silo,
    StructureKind.CHIMNEY: _chimney,
    StructureKind.PYLON: _pylon,
    StructureKind.POLE: _pole,
    StructureKind.WINDMILL: _windmill,
    StructureKind.BILLBOARD: _billboard,
}


def generate_structures(builder: MeshBuilder, ctx: GenerationContext, structures) -> int:
    """Constroi as estruturas verticais mapeadas no OSM."""
    if not structures or not ctx.settings.structures:
        return 0

    palette = ctx.palette
    built = 0

    for structure in structures:
        point = structure.position
        if point is None:
            continue
        center = np.array([point.x, point.y], dtype=np.float64)

        kind = structure.structure
        draw = BUILDERS.get(kind)
        if draw is None:
            continue

        rng = ctx.rng(structure.osm_id, int(kind.value.encode().hex()[:8], 16))
        height = structure.height or DEFAULT_HEIGHT[kind] * rng.uniform(0.82, 1.18)

        radius = DEFAULT_RADIUS[kind]
        if structure.footprint is not None and structure.footprint.area > 1.0:
            # Contorno mapeado manda no tamanho.
            radius = float(np.sqrt(structure.footprint.area / np.pi))
        radius = max(radius, 0.1)

        base_z = layers.Z_GROUND + ctx.ground_at(point.x, point.y)
        try:
            draw(builder, palette, center, base_z, height, radius)
            built += 1
        except Exception as exc:  # noqa: BLE001 - uma torre ruim nao derruba a cena
            log.debug("Estrutura %s ignorada: %s", structure.osm_id, exc)

    log.info("Estruturas construidas: %d", built)
    return built


# Classes viarias que recebem iluminacao publica, e o espacamento entre postes.
LIT_ROADS = {
    "motorway": 45.0,
    "trunk": 45.0,
    "primary": 38.0,
    "secondary": 38.0,
    "tertiary": 42.0,
    "residential": 50.0,
}


def generate_street_lamps(builder: MeshBuilder, ctx: GenerationContext, road_result) -> int:
    """Postes de iluminacao ao longo das vias, alternando de lado.

    Nao vem do OSM: pouquissimas cidades mapeiam poste. Vem do proprio tracado,
    que e o que a prefeitura tambem usa para decidir onde por.
    """
    if not ctx.settings.street_lamps or not ctx.detail.get("street_furniture"):
        return 0
    centerlines = getattr(road_result, "centerlines", None)
    if not centerlines:
        return 0

    from ..core.features import RoadClass
    from .roads import ROAD_SPECS

    palette = ctx.palette
    lamp_height = 8.2
    posts: list[np.ndarray] = []

    for index, (line, half_width, road_class) in enumerate(centerlines):
        spacing = LIT_ROADS.get(getattr(road_class, "value", ""), 0.0)
        if spacing <= 0 or line.length < spacing:
            continue
        rng = ctx.rng(9001, index)
        offset = half_width + 0.9
        distance = rng.uniform(spacing * 0.2, spacing * 0.8)
        side = 1.0
        while distance < line.length:
            point = line.interpolate(distance)
            ahead = line.interpolate(min(distance + 1.0, line.length))
            direction = np.array([ahead.x - point.x, ahead.y - point.y])
            norm = np.linalg.norm(direction)
            if norm > 1e-6:
                direction /= norm
                normal = np.array([-direction[1], direction[0]]) * offset * side
                posts.append(np.array([point.x + normal[0], point.y + normal[1]]))
            distance += spacing
            side = -side  # zigue-zague de um lado ao outro, como na rua

    if not posts:
        return 0

    for position in posts:
        base_z = layers.Z_CURB + ctx.ground_at(position[0], position[1])
        builder.add_prism(
            palette.rail, _ngon(position, 0.11, sides=5), base_z, base_z + lamp_height
        )
        # Braco curvo simplificado: um trecho horizontal com a luminaria.
        arm = Polygon(
            [
                (position[0] - 0.07, position[1] - 0.07),
                (position[0] + 1.5, position[1] - 0.07),
                (position[0] + 1.5, position[1] + 0.07),
                (position[0] - 0.07, position[1] + 0.07),
            ]
        )
        builder.add_prism(
            palette.rail, arm, base_z + lamp_height - 0.35, base_z + lamp_height - 0.2, cap_top=True
        )
        head = _square(np.array([position[0] + 1.45, position[1]]), 0.3)
        builder.add_prism(
            palette.window, head, base_z + lamp_height - 0.5, base_z + lamp_height - 0.35, cap_top=True
        )

    log.info("Postes de iluminacao: %d", len(posts))
    return len(posts)
