"""Vegetacao: arvores individuais do OSM + distribuicao procedural em areas verdes.

As arvores sao instancias de poucos prototipos low-poly, assadas na malha final
com posicao, escala e rotacao proprias.
"""

from __future__ import annotations

import logging

import numpy as np
from shapely import contains_xy
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..core.mesh import MeshBuilder
from . import layers
from .context import GenerationContext

log = logging.getLogger(__name__)

# Espacamento base (m) por tipo de area verde, antes do ajuste de detalhe/densidade.
SPACING = {"forest": 0.78, "park": 1.7}

# Area minima de uma mancha para receber arvore: o tamanho de uma copa, nao o do
# espacamento. Um canto de quintal de 20 m2 tem uma mangueira.
MIN_PATCH_M2 = 18.0

# Mistura de especies por contexto, e a faixa de altura de cada uma (metros).
# Bosque tem mais conifera e nenhuma palmeira de calcada; parque urbano tem
# copa larga e alguma palmeira; rua tem arvore podada, mais baixa.
SPECIES_MIX: dict[str, dict[str, float]] = {
    "forest": {"broadleaf": 0.42, "pine": 0.22, "conifer": 0.18, "umbrella": 0.12, "shrub": 0.06},
    "park": {"broadleaf": 0.40, "umbrella": 0.22, "palm": 0.14, "cypress": 0.10,
             "conifer": 0.08, "shrub": 0.06},
    "street": {"broadleaf": 0.46, "umbrella": 0.24, "palm": 0.18, "cypress": 0.12},
}

SPECIES_HEIGHT: dict[str, tuple[float, float]] = {
    "broadleaf": (6.5, 12.5),
    "conifer": (7.0, 14.0),
    "pine": (9.0, 17.0),
    "cypress": (6.0, 11.0),
    "palm": (7.0, 14.0),
    "umbrella": (6.0, 10.0),
    "shrub": (1.6, 2.8),
}

# Fator aplicado a faixa de altura conforme o contexto.
CONTEXT_SCALE = {"forest": 1.12, "park": 1.0, "street": 0.85}


def _ngon(sides: int, radius: float, z0: float, z1: float) -> tuple[np.ndarray, np.ndarray]:
    """Prisma de base regular (tronco)."""
    angles = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    ring = np.column_stack([np.cos(angles) * radius, np.sin(angles) * radius])
    bottom = np.column_stack([ring, np.full(sides, z0)])
    top = np.column_stack([ring, np.full(sides, z1)])
    verts = np.concatenate([bottom, top], axis=0)

    idx = np.arange(sides)
    nxt = (idx + 1) % sides
    faces = [
        np.column_stack([idx, nxt, nxt + sides]),
        np.column_stack([idx, nxt + sides, idx + sides]),
    ]
    # Tampa superior (leque).
    cap = np.column_stack(
        [np.full(sides - 2, sides), np.arange(1, sides - 1) + sides, np.arange(2, sides) + sides]
    )
    faces.append(cap)
    return verts, np.concatenate(faces, axis=0).astype(np.int64)


def _cone(sides: int, radius: float, z0: float, z1: float) -> tuple[np.ndarray, np.ndarray]:
    angles = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    ring = np.column_stack([np.cos(angles) * radius, np.sin(angles) * radius, np.full(sides, z0)])
    verts = np.concatenate([ring, np.array([[0.0, 0.0, z1]])], axis=0)
    idx = np.arange(sides)
    nxt = (idx + 1) % sides
    side_faces = np.column_stack([idx, nxt, np.full(sides, sides)])
    base = np.column_stack([np.full(sides - 2, 0), np.arange(2, sides), np.arange(1, sides - 1)])
    return verts, np.concatenate([side_faces, base], axis=0).astype(np.int64)


def _blob(subdivisions: int, radius: float, center_z: float) -> tuple[np.ndarray, np.ndarray]:
    """Copa arredondada low-poly."""
    return _flattened_blob(subdivisions, radius, center_z, 1.15)


def _flattened_blob(
    subdivisions: int, radius: float, center_z: float, squash: float
) -> tuple[np.ndarray, np.ndarray]:
    """Esfera low-poly esticada ou achatada no eixo vertical."""
    from trimesh.creation import icosphere

    sphere = icosphere(subdivisions=subdivisions, radius=radius)
    verts = np.asarray(sphere.vertices, dtype=np.float64).copy()
    verts[:, 2] = verts[:, 2] * squash + center_z
    return verts, np.asarray(sphere.faces, dtype=np.int64)


def _stacked_cones(sides: int, layers: int, radius: float, z0: float, z1: float):
    """Copa em camadas: da a silhueta escalonada de pinheiro/araucaria."""
    verts, faces = [], []
    offset = 0
    span = (z1 - z0) / layers
    for i in range(layers):
        # Cada camada e um cone achatado, menor conforme sobe.
        shrink = 1.0 - 0.62 * (i / max(layers - 1, 1))
        base_z = z0 + i * span * 0.78
        v, f = _cone(sides, radius * shrink, base_z, base_z + span * 1.35)
        verts.append(v)
        faces.append(f + offset)
        offset += len(v)
    return np.concatenate(verts), np.concatenate(faces)


def _palm(fronds: int, trunk_top: float):
    """Palmeira: folhas como laminas finas caindo a partir do topo do estipe."""
    verts, faces = [], []
    offset = 0
    for i in range(fronds):
        angle = 2 * np.pi * i / fronds
        direction = np.array([np.cos(angle), np.sin(angle)])
        tip = direction * 0.34
        mid = direction * 0.20
        half = 0.045
        side = np.array([-direction[1], direction[0]]) * half
        # Triangulo alongado com uma dobra: sobe um pouco e depois cai.
        v = np.array(
            [
                [side[0], side[1], trunk_top],
                [-side[0], -side[1], trunk_top],
                [mid[0], mid[1], trunk_top + 0.10],
                [tip[0], tip[1], trunk_top - 0.06],
            ]
        )
        f = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)
        verts.append(v)
        faces.append(f + offset)
        offset += len(v)
    return np.concatenate(verts), np.concatenate(faces)


class TreePrototypes:
    """Prototipos normalizados: altura total = 1.0 unidade.

    Cada especie tem um par (tronco, copa) proprio, porque a proporcao do
    tronco muda muito: palmeira e quase so estipe, arbusto quase nao tem.
    """

    def __init__(self, detail_level: str):
        subdivisions = 0 if detail_level == "low" else 1
        sides = 5 if detail_level == "low" else 6

        self.trunks = {
            "broadleaf": _ngon(sides, 0.055, 0.0, 0.45),
            "conifer": _ngon(sides, 0.050, 0.0, 0.30),
            "pine": _ngon(sides, 0.048, 0.0, 0.34),
            "cypress": _ngon(sides, 0.040, 0.0, 0.18),
            "palm": _ngon(sides, 0.042, 0.0, 0.74),
            "umbrella": _ngon(sides, 0.060, 0.0, 0.56),
            "shrub": _ngon(max(sides - 1, 4), 0.035, 0.0, 0.12),
        }
        self.canopies = {
            "broadleaf": _blob(subdivisions, 0.30, 0.66),
            "conifer": _cone(sides + 1, 0.26, 0.28, 1.0),
            "pine": _stacked_cones(sides + 1, 3 if detail_level != "low" else 2, 0.30, 0.30, 1.0),
            # Cipreste: fuso alto e estreito.
            "cypress": _cone(sides + 1, 0.14, 0.16, 1.0),
            "palm": _palm(7 if detail_level != "low" else 5, 0.72),
            # Copa de guarda-chuva: esfera bem achatada e larga.
            "umbrella": _flattened_blob(subdivisions, 0.38, 0.78, 0.45),
            "shrub": _blob(subdivisions, 0.34, 0.42),
        }

    def trunk(self, species: str):
        return self.trunks.get(species, self.trunks["broadleaf"])

    def canopy(self, species: str):
        return self.canopies.get(species, self.canopies["broadleaf"])


def _scatter(poly: Polygon, spacing: float, rng: np.random.Generator) -> np.ndarray:
    """Distribui pontos dentro do poligono. Retorna array (n, 2).

    Grade jitterada com deslocamento grande e espacamento variando por ponto:
    uma grade regular deixa fileiras visiveis de cima, que e o que mais denuncia
    vegetacao gerada por computador.
    """
    minx, miny, maxx, maxy = poly.bounds
    if maxx - minx < spacing * 0.5 or maxy - miny < spacing * 0.5:
        point = poly.representative_point()
        return np.array([[point.x, point.y]])

    # Amostra mais denso que o alvo e depois rejeita: o excesso vira folga para
    # o jitter forte sem abrir buracos.
    passo = spacing * 0.8
    xs = np.arange(minx + passo * 0.5, maxx, passo)
    ys = np.arange(miny + passo * 0.5, maxy, passo)
    if len(xs) == 0 or len(ys) == 0:
        return np.zeros((0, 2))

    grid_x, grid_y = np.meshgrid(xs, ys)
    points = np.column_stack([grid_x.ravel(), grid_y.ravel()])

    # Deslocamento em cada eixo, para sumir com o alinhamento da grade. A folga
    # encolhe em mancha pequena: com jitter fixo de 70% do passo, um quintal de
    # 15 m jogava quase todo ponto para fora do poligono e ficava sem arvore -
    # era o que deixava a cidade pelada mesmo com a copa toda detectada.
    largura = min(maxx - minx, maxy - miny)
    folga = passo * (0.7 if largura > 4 * passo else 0.3)
    points += rng.uniform(-folga, folga, size=points.shape)
    # E descarta parte, para a densidade nao virar textura regular.
    points = points[rng.random(len(points)) < 0.72]

    if len(points):
        points = points[contains_xy(poly, points[:, 0], points[:, 1])]
    if len(points):
        return points

    # Mancha pequena que perdeu todos os pontos: vale uma arvore no centro.
    ponto = poly.representative_point()
    return np.array([[ponto.x, ponto.y]])


def _greenness(geo, points: np.ndarray) -> np.ndarray:
    """Quanto de verde ha na imagem em cada ponto, de 0 a 1.

    E o que faz a densidade parar de ser uniforme: dentro de um parque ha campo,
    quadra e mata, e plantar arvore igual nos tres e o que deixa a vegetacao com
    cara de carimbo.
    """
    array = geo.array
    height, width = array.shape[:2]
    cols, rows = [], []
    for x, y in points:
        c, r = geo.to_pixel(float(x), float(y))
        cols.append(c)
        rows.append(r)
    cols = np.clip(np.asarray(cols), 0, width - 1).astype(np.int64)
    rows = np.clip(np.asarray(rows), 0, height - 1).astype(np.int64)

    rgb = array[rows, cols].astype(np.float64) / 255.0
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    # Excesso de verde normalizado: positivo em folhagem, negativo em asfalto,
    # telhado e solo exposto.
    excess = 2.0 * g - r - b
    return np.clip((excess + 0.02) / 0.16, 0.0, 1.0)


def _as_polygons(geom):
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        return [g for g in geom.geoms if isinstance(g, Polygon)]
    return []


# Fracao de pontos que sobrevive ao jitter e a rejeicao dentro de `_scatter`.
SCATTER_YIELD = 0.72


def _spacing_for_budget(map_data, base_spacing: float, max_trees: int) -> float:
    """Fator a aplicar no espacamento para o total caber no orcamento.

    Devolve 1.0 quando ja cabe. Como a contagem cai com o quadrado do
    espacamento, o fator e a raiz da razao entre o estimado e o teto.
    """
    if max_trees <= 0:
        return 1.0

    estimado = 0.0
    for kind, features in (("forest", map_data.forests), ("park", map_data.parks)):
        passo = base_spacing * SPACING[kind]
        if passo <= 0:
            continue
        area = sum(
            f.geometry.area for f in features if f.geometry is not None and not f.geometry.is_empty
        )
        estimado += area / (passo * passo) * SCATTER_YIELD
    estimado += len(map_data.trees)

    if estimado <= max_trees:
        return 1.0
    return float(np.sqrt(estimado / max_trees))


def generate_vegetation(
    builder: MeshBuilder,
    ctx: GenerationContext,
    map_data,
    exclude=None,
) -> int:
    """Planta arvores nas areas verdes e nos nos natural=tree. Retorna o total."""
    if not ctx.settings.vegetation:
        return 0

    settings = ctx.settings
    density = max(settings.tree_density, 0.05)
    base_spacing = ctx.detail["tree_spacing"] / density

    # --- orcamento ---
    #
    # Com a vegetacao vindo da imagem, uma regiao de mata pode pedir dezenas de
    # milhares de arvores. Truncar a lista no teto deixaria metade da mata pelada,
    # entao em vez de cortar arvores no fim, afastamos todas no comeco: o
    # espacamento cresce ate a conta fechar, e a copa cresce junto para o dossel
    # continuar fechando. Uma mata rala de arvores grandes le como mata; meia
    # mata cheia e meia mata vazia nao le como nada.
    orcamento = _spacing_for_budget(map_data, base_spacing, settings.max_trees)
    base_spacing *= orcamento
    crown_scale = min(orcamento, 1.45)
    if orcamento > 1.02:
        log.info(
            "Vegetacao: espacamento x%.2f para caber em %d arvores", orcamento, settings.max_trees
        )
    prototypes = TreePrototypes(settings.detail)
    palette = ctx.palette
    n_canopies = palette.canopy_count()

    # (posicao x, y, z, escala, rotacao) por especie
    placements: dict[str, list[np.ndarray]] = {name: [] for name in SPECIES_HEIGHT}

    def place(points: np.ndarray, rng: np.random.Generator, context: str):
        """Sorteia especie por ponto e guarda a transformacao de cada arvore."""
        if len(points) == 0:
            return
        # A regiao inclina a mistura: palmeira no tropico, conifera no norte.
        mix = ctx.region.species_mix(SPECIES_MIX[context])
        names = [n for n in mix if mix[n] > 1e-6]
        if not names:
            return
        probs = np.array([mix[n] for n in names], dtype=float)
        probs /= probs.sum()
        chosen = rng.choice(len(names), size=len(points), p=probs)

        rotations = rng.uniform(0.0, 2 * np.pi, size=len(points))
        ground = np.full(len(points), layers.Z_GREEN)
        if ctx.draped:
            ground = ground + ctx.terrain.height(points[:, 0], points[:, 1])

        scale = CONTEXT_SCALE[context] * (crown_scale if context != "street" else 1.0)
        for index, name in enumerate(names):
            mask = chosen == index
            if not mask.any():
                continue
            low, high = SPECIES_HEIGHT[name]
            heights = rng.uniform(low * scale, high * scale, size=int(mask.sum()))
            placements[name].append(
                np.column_stack(
                    [
                        points[mask, 0],
                        points[mask, 1],
                        ground[mask],
                        heights,
                        rotations[mask],
                    ]
                )
            )

    # --- areas verdes ---
    for kind, features in (("forest", map_data.forests), ("park", map_data.parks)):
        spacing = base_spacing * SPACING[kind]
        for feature in features:
            geom = feature.geometry
            if geom is None or geom.is_empty or geom.area < MIN_PATCH_M2:
                continue
            # A mancha vinda da imagem ja nasce recortada contra telhado, via e
            # agua - foi assim que ela foi encontrada. Recortar de novo, agora
            # com a folga de 1,5 m do `exclude`, corroia a borda de cada quintal
            # e apagava a arvore que existe de verdade colada na casa.
            detectada = feature.tags.get("source") == "deteccao"
            if exclude is not None and not detectada:
                try:
                    geom = geom.difference(exclude)
                except Exception:  # noqa: BLE001 - segue com a area original
                    pass
            leisure = feature.tags.get("leisure")
            if leisure in {"pitch", "playground", "golf_course"}:
                continue  # campos e quadras ficam gramados

            rng = ctx.rng(feature.osm_id, int(geom.area))
            for poly in _as_polygons(geom):
                # O piso e o tamanho de uma copa, nao o do espacamento. Descartar
                # tudo que for menor que o espacamento apagava justamente a
                # arvore de quintal - que e a maioria da vegetacao de uma cidade
                # pequena, e que sai fatiada em cacos ao recortar contra as casas.
                if poly.area < MIN_PATCH_M2:
                    continue
                points = _scatter(poly, spacing, rng)
                if len(points) and ctx.imagery is not None:
                    # A foto decide onde ha copa de verdade: campo de futebol e
                    # patio dentro do parque deixam de receber arvore.
                    verde = _greenness(ctx.imagery, points)
                    points = points[rng.random(len(points)) < verde]
                place(points, rng, context=kind)

    # --- arvores mapeadas individualmente (quase sempre arborizacao de rua) ---
    if map_data.trees:
        rng = ctx.rng(7717, len(map_data.trees))
        points = np.array([[t.position.x, t.position.y] for t in map_data.trees])
        if exclude is not None and not exclude.is_empty and len(points):
            # Mesmo mapeada, arvore em cima de casa e erro de posicao no OSM.
            livre = ~contains_xy(exclude, points[:, 0], points[:, 1])
            points = points[livre]
        if len(points):
            place(points, rng, context="street")

    total = 0
    for species, chunks in placements.items():
        chunks = [c for c in chunks if len(c)]
        if not chunks:
            continue
        rows = np.concatenate(chunks, axis=0)
        if len(rows) + total > settings.max_trees:
            rows = rows[: max(settings.max_trees - total, 0)]
        if not len(rows):
            continue
        total += len(rows)

        builder.add_instances(palette.trunk, prototypes.trunk(species), rows)
        # Divide as copas entre as cores da paleta para quebrar a repeticao.
        offset = abs(hash(species)) % max(n_canopies, 1)
        canopy_index = (np.arange(len(rows)) + offset) % n_canopies
        proto = prototypes.canopy(species)
        for i in range(n_canopies):
            subset = rows[canopy_index == i]
            if len(subset):
                builder.add_instances(palette.canopy(i), proto, subset)

    ctx.report(f"{total} arvores", 1.0)
    return total
