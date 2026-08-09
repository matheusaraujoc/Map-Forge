"""Vegetacao: arvores individuais do OSM + distribuicao procedural em areas verdes.

As arvores sao instancias de poucos prototipos low-poly, assadas na malha final
com posicao, escala e rotacao proprias.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import shapely
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


# Sobreposicao minima entre o fundo da copa e o topo do tronco, em fracao da
# altura da arvore. Encostar exatamente deixa uma linha de luz na juncao.
CANOPY_OVERLAP = 0.04


def _anchor_canopy(canopy, trunk_top: float, overlap: float = CANOPY_OVERLAP):
    """Desce a copa ate ela alcancar o tronco, se estiver flutuando."""
    verts, faces = canopy
    fundo = float(np.asarray(verts)[:, 2].min())
    folga = fundo - (trunk_top - overlap)
    if folga <= 0.0:
        return canopy
    baixados = np.asarray(verts, dtype=np.float64).copy()
    baixados[:, 2] -= folga
    return baixados, faces


class TreePrototypes:
    """Prototipos normalizados: altura total = 1.0 unidade.

    Cada especie tem um par (tronco, copa) proprio, porque a proporcao do
    tronco muda muito: palmeira e quase so estipe, arbusto quase nao tem.
    """

    def __init__(self, detail_level: str, trunk_sides: int | None = None):
        subdivisions = 0 if detail_level == "low" else 1
        sides = 5 if detail_level == "low" else 6
        tronco_lados = trunk_sides or sides

        lados = tronco_lados
        self.trunks = {
            "broadleaf": _ngon(lados, 0.055, 0.0, 0.45),
            "conifer": _ngon(lados, 0.050, 0.0, 0.30),
            "pine": _ngon(lados, 0.048, 0.0, 0.34),
            "cypress": _ngon(lados, 0.040, 0.0, 0.18),
            "palm": _ngon(lados, 0.042, 0.0, 0.74),
            "umbrella": _ngon(lados, 0.060, 0.0, 0.56),
            "shrub": _ngon(max(lados - 1, 3), 0.035, 0.0, 0.12),
        }
        # Variantes por especie. Uma copa so por especie deixa a mata com cara de
        # carimbo: mil arvores identicas giradas. Duas ou tres silhuetas por
        # especie custam zero - continuam instanciadas - e quebram a repeticao,
        # que e o que mais denuncia vegetacao gerada por computador.
        self.canopies: dict[str, list] = {
            "broadleaf": [
                _blob(subdivisions, 0.30, 0.66),
                # Copa mais larga e baixa: mangueira, oiti, arvore de calcada
                # podada em taca.
                _flattened_blob(subdivisions, 0.34, 0.62, 0.78),
                # Copa alta e estreita, de arvore espremida entre casas.
                _flattened_blob(subdivisions, 0.24, 0.70, 1.35),
            ],
            "conifer": [
                _cone(sides + 1, 0.26, 0.28, 1.0),
                _cone(sides + 1, 0.22, 0.34, 1.0),
            ],
            "pine": [
                _stacked_cones(sides + 1, 3 if detail_level != "low" else 2, 0.30, 0.30, 1.0),
                _stacked_cones(sides + 1, 4 if detail_level != "low" else 2, 0.26, 0.26, 1.0),
            ],
            # Cipreste: fuso alto e estreito.
            "cypress": [_cone(sides + 1, 0.14, 0.16, 1.0)],
            "palm": [
                _palm(7 if detail_level != "low" else 5, 0.72),
                # Palmeira de estipe mais alto e menos folhas: carnauba, babacu.
                _palm(6 if detail_level != "low" else 4, 0.82),
            ],
            # Copa de guarda-chuva: esfera bem achatada e larga.
            "umbrella": [
                _flattened_blob(subdivisions, 0.38, 0.78, 0.45),
                _flattened_blob(subdivisions, 0.44, 0.72, 0.34),
            ],
            "shrub": [
                _blob(subdivisions, 0.34, 0.42),
                _flattened_blob(subdivisions, 0.40, 0.38, 0.62),
            ],
        }

        # Ancora toda copa no tronco.
        #
        # Cada copa e construida com um centro e um achatamento proprios, e o
        # fundo dela sai de `centro - raio * achatamento`. Tres combinacoes
        # caiam *acima* do topo do tronco - copa de guarda-chuva, conifera
        # variante 2 e arbusto variante 2 - e a arvore aparecia com a copa
        # flutuando meio metro acima da haste. Em vez de acertar os numeros um a
        # um e torcer para a proxima variante nao repetir o erro, a ancoragem e
        # verificada aqui: se o fundo da copa nao alcanca o tronco, a copa desce.
        for especie, formas in self.canopies.items():
            topo = float(self.trunks[especie][0][:, 2].max())
            self.canopies[especie] = [
                _anchor_canopy(forma, topo) for forma in formas
            ]

    def trunk(self, species: str):
        return self.trunks.get(species, self.trunks["broadleaf"])

    def variants(self, species: str) -> list:
        return self.canopies.get(species, self.canopies["broadleaf"])

    def canopy(self, species: str, variant: int = 0):
        opcoes = self.variants(species)
        return opcoes[variant % len(opcoes)]


# Detalhe por contexto, que e o mesmo principio do dossel levado ao individuo:
# a arvore de rua e vista de perto e merece a copa cheia (96 triangulos); a
# arvore de dentro de uma mata aparece como uma mancha no meio de outras mil e
# resolve com a copa de 20 faces. Trocar so isso corta dois tercos do custo da
# vegetacao em massa, sem diferenca perceptivel de cima.
MASS_CONTEXTS = {"forest"}

# Tronco magro para vegetacao em massa.
#
# No prototipo barato o tronco e 13 dos 33 triangulos. A tentacao e remove-lo -
# de cima ele esta debaixo da copa e nao aparece. Mas o mapa e olhado em
# perspectiva obliqua, e sem tronco a copa fica **flutuando**, que e pior que
# gastar os triangulos. Entao ele encolhe em vez de sumir: prisma de 3 lados
# (7 triangulos) no lugar do de 5 (13). A esta distancia ninguem conta as faces
# de um cilindro de 20 cm.
MASS_TRUNK_SIDES = 3


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

# --- dossel ---
#
# Uma arvore custa 96 triangulos. Uma mata de 13 ha pedia 5.000 arvores so para
# ela, e a vegetacao passou a ser metade da malha inteira - o que e desperdicio,
# porque o miolo de uma mata fechada nunca e visto como arvore individual. De
# qualquer angulo se ve duas coisas: o topo do dossel e a silhueta da borda.
#
# Entao o miolo vira uma superficie unica e ondulada na altura das copas (2
# triangulos por celula), e so a faixa da borda recebe arvore de verdade. Em
# Araioses isso troca ~500 mil triangulos por ~200 mil, e ainda fecha o dossel,
# que com arvore solta ficava esburacado.

# Area a partir da qual vale a pena separar miolo de borda.
SHELL_MIN_AREA = 2_500.0
# Largura da faixa de borda que continua recebendo arvore individual.
SHELL_BORDER_M = 13.0
# Lado da celula do dossel. Menor ondula mais fino e custa mais - e como o
# dossel nao e instanciado, cada celula custa vertice proprio no arquivo. 8 m
# sustenta as duas frequencias da ondulacao sem serrilhar.
SHELL_CELL_M = 8.0
# Altura media do dossel e amplitude da ondulacao, em metros.
SHELL_HEIGHT = 11.0
SHELL_RELIEF = 2.6
# Faixa em que o dossel sobe do chao ate a altura cheia. Com borda em degrau a
# mata virava um planalto de topo reto; com a subida ela vira uma elevacao.
SHELL_TAPER_M = 16.0
# Quantas cores o dossel usa, amostradas da propria foto.
SHELL_COLORS = 4


def _split_shell(poly: Polygon, band: float):
    """Separa o miolo (vira dossel) da faixa de borda (recebe arvore).

    Devolve (miolo, faixa). Miolo None quando a mancha e estreita demais para
    ter miolo - quintal e capao fino sao so borda, e continuam com arvore.
    """
    if poly.area < SHELL_MIN_AREA:
        return None, poly
    try:
        miolo = poly.buffer(-band)
    except Exception:  # noqa: BLE001 - topologia ruim
        return None, poly
    if miolo.is_empty or miolo.area < SHELL_MIN_AREA * 0.35:
        return None, poly
    try:
        faixa = poly.difference(miolo)
    except Exception:  # noqa: BLE001
        return None, poly
    return miolo, faixa


def _canopy_surface(
    builder: MeshBuilder,
    ctx: GenerationContext,
    geom,
    rng: np.random.Generator,
    palette,
) -> int:
    """Constroi o dossel sobre o miolo de uma mata. Retorna os triangulos.

    A ondulacao vem da soma de duas senoides com fase sorteada. Nao e ruido de
    verdade, mas basta: o que se quer e que o topo nao seja um plano, e duas
    frequencias ja quebram qualquer alinhamento visivel de cima.
    """
    minx, miny, maxx, maxy = geom.bounds
    passo = SHELL_CELL_M
    xs = np.arange(minx, maxx + passo, passo)
    ys = np.arange(miny, maxy + passo, passo)
    if len(xs) < 2 or len(ys) < 2:
        return 0

    grid_x, grid_y = np.meshgrid(xs, ys)
    plano_x, plano_y = grid_x.ravel(), grid_y.ravel()

    dentro = contains_xy(geom, plano_x, plano_y).reshape(grid_y.shape)
    if not dentro.any():
        return 0

    # Altura: chao + copa ondulada.
    chao = np.zeros(plano_x.shape)
    if ctx.draped:
        chao = ctx.terrain.height(plano_x, plano_y)

    fase = rng.uniform(0.0, 2 * np.pi, size=4)
    onda = (
        np.sin(plano_x / 27.0 + fase[0]) * np.cos(plano_y / 31.0 + fase[1])
        + 0.55 * np.sin(plano_x / 13.0 + fase[2]) * np.sin(plano_y / 11.0 + fase[3])
    )

    # Perto da borda o dossel desce ate o chao. Sem isso ele vira um planalto de
    # topo reto com parede vertical em volta - a mata ficava com cara de mesa
    # verde no modelo, que foi exatamente o defeito que apareceu em Araioses.
    borda = shapely.distance(shapely.points(plano_x, plano_y), geom.boundary)
    subida = np.clip(borda / SHELL_TAPER_M, 0.0, 1.0)
    # Curva suave em vez de rampa reta: encosta arredondada.
    subida = subida * subida * (3.0 - 2.0 * subida)

    topo = chao + layers.Z_GREEN + (SHELL_HEIGHT + onda * SHELL_RELIEF) * subida

    verts = np.column_stack([plano_x, plano_y, topo])
    largura = len(xs)

    # Uma celula so vira quadrado quando os quatro cantos estao dentro; assim a
    # borda do dossel acompanha o contorno em vez de estourar para fora.
    linhas, colunas = np.nonzero(
        dentro[:-1, :-1] & dentro[1:, :-1] & dentro[:-1, 1:] & dentro[1:, 1:]
    )
    if not len(linhas):
        return 0

    canto = linhas * largura + colunas
    faces = np.concatenate(
        [
            np.column_stack([canto, canto + 1, canto + largura + 1]),
            np.column_stack([canto, canto + largura + 1, canto + largura]),
        ]
    )

    # A cor de cada celula vem da propria foto naquele ponto: mata de varzea,
    # capoeira e mata fechada tem verdes bem diferentes, e uma cor so para tudo
    # e o que mais denuncia o dossel como superficie pintada.
    materiais, tom = _shell_materials(ctx, plano_x[canto], plano_y[canto], palette)

    total = 0
    for i, material in enumerate(materiais):
        parte = faces[np.tile(tom == i, 2)]
        if len(parte):
            builder.add_mesh(material, verts, parte)
            total += len(parte)
    return total


def _shell_materials(ctx, xs: np.ndarray, ys: np.ndarray, palette):
    """Materiais do dossel amostrados na imagem. Devolve (materiais, indice)."""
    n_estilo = max(palette.canopy_count(), 1)
    if ctx.imagery is None:
        # Sem foto, cai nas cores do estilo com uma variacao espacial lenta.
        tom = ((np.sin(xs / 34.0) + np.cos(ys / 29.0) + 2.0) / 4.0 * n_estilo)
        return (
            [palette.canopy(i) for i in range(n_estilo)],
            tom.astype(np.int64) % n_estilo,
        )

    from ..core.mesh import Material
    from ..imagery.sampling import blend, harmonize

    from .coloring import quantize

    geo = ctx.imagery
    array = geo.array
    altura, largura = array.shape[:2]
    cols = np.empty(len(xs), dtype=np.int64)
    linhas = np.empty(len(ys), dtype=np.int64)
    for i, (x, y) in enumerate(zip(xs, ys)):
        c, r = geo.to_pixel(float(x), float(y))
        cols[i], linhas[i] = c, r
    cols = np.clip(cols, 0, largura - 1)
    linhas = np.clip(linhas, 0, altura - 1)

    cores = array[linhas, cols].astype(np.float64) / 255.0
    rotulos, centros = quantize(cores, SHELL_COLORS, seed=ctx.settings.seed)

    amount = ctx.settings.area_blend
    materiais = [
        Material(
            name=f"dossel_sat_{i:02d}",
            color=blend(
                palette.canopy(i % n_estilo).color,
                harmonize(tuple(float(c) for c in centro), saturation=1.1),
                amount,
            ),
            roughness=palette.style.roughness,
        )
        for i, centro in enumerate(centros)
    ]
    return materiais, rotulos


@dataclass
class _GreenArea:
    """Uma area verde ja resolvida: o que vira dossel e o que recebe arvore."""

    kind: str
    osm_id: int
    scatter: object  # geometria que recebe arvore (pode ser vazia)
    shell: object = None  # miolo que vira dossel, ou None
    scatter_area: float = 0.0


def _plan_areas(ctx, map_data, base_spacing: float, exclude) -> list[_GreenArea]:
    """Resolve, para cada area verde, o que e dossel e o que e arvore."""
    usa_dossel = getattr(ctx.settings, "canopy_shell", True)
    plano: list[_GreenArea] = []

    for kind, features in (("forest", map_data.forests), ("park", map_data.parks)):
        for feature in features:
            geom = feature.geometry
            if geom is None or geom.is_empty or geom.area < MIN_PATCH_M2:
                continue
            if feature.tags.get("leisure") in {"pitch", "playground", "golf_course"}:
                continue  # campos e quadras ficam gramados

            # A mancha vinda da imagem ja nasce recortada contra telhado, via e
            # agua - foi assim que ela foi encontrada. Recortar de novo, agora
            # com a folga de 1,5 m do `exclude`, corroia a borda de cada quintal
            # e apagava a arvore que existe de verdade colada na casa.
            if exclude is not None and feature.tags.get("source") != "deteccao":
                try:
                    geom = geom.difference(exclude)
                except Exception:  # noqa: BLE001 - segue com a area original
                    pass
            if geom.is_empty:
                continue

            # So mata tem dossel: parque e gramado com arvore solta, nao copa
            # fechada, e cobri-lo com uma superficie apagaria o gramado.
            miolo = None
            espalhar = geom
            if usa_dossel and kind == "forest":
                partes_miolo, partes_faixa = [], []
                for poly in _as_polygons(geom):
                    dentro, faixa = _split_shell(poly, SHELL_BORDER_M)
                    if dentro is not None:
                        partes_miolo.append(dentro)
                    if faixa is not None and not faixa.is_empty:
                        partes_faixa.append(faixa)
                if partes_miolo:
                    miolo = unary_union(partes_miolo)
                    espalhar = unary_union(partes_faixa) if partes_faixa else None

            plano.append(
                _GreenArea(
                    kind=kind,
                    osm_id=feature.osm_id,
                    scatter=espalhar,
                    shell=miolo,
                    scatter_area=(
                        espalhar.area if espalhar is not None and not espalhar.is_empty else 0.0
                    ),
                )
            )
    return plano


def _spacing_for_budget(areas_por_tipo, arvores_mapeadas, base_spacing, max_trees) -> float:
    """Fator a aplicar no espacamento para o total caber no orcamento.

    `areas_por_tipo` e uma sequencia de (kind, area em m2) - ja so a area que
    vai receber arvore, sem o miolo que virou dossel.

    Devolve 1.0 quando ja cabe. Como a contagem cai com o quadrado do
    espacamento, o fator e a raiz da razao entre o estimado e o teto.
    """
    if max_trees <= 0:
        return 1.0

    estimado = 0.0
    for kind, area in areas_por_tipo:
        passo = base_spacing * SPACING[kind]
        if passo > 0:
            estimado += area / (passo * passo) * SCATTER_YIELD
    estimado += arvores_mapeadas

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

    # --- plano ---
    #
    # Decide antes o que vira dossel e o que vira arvore, porque o orcamento
    # precisa contar so a area que de fato recebe arvore.
    plano = _plan_areas(ctx, map_data, base_spacing, exclude)

    # --- orcamento ---
    #
    # Mesmo com o dossel, uma regiao de mata pode pedir dezenas de milhares de
    # arvores. Truncar a lista no teto deixaria metade da mata pelada, entao em
    # vez de cortar arvores no fim, afastamos todas no comeco: o espacamento
    # cresce ate a conta fechar, e a copa cresce junto para o dossel continuar
    # fechando. Uma mata rala de arvores grandes le como mata; meia mata cheia e
    # meia mata vazia nao le como nada.
    orcamento = _spacing_for_budget(
        [(item.kind, item.scatter_area) for item in plano],
        len(map_data.trees),
        base_spacing,
        settings.max_trees,
    )
    base_spacing *= orcamento
    crown_scale = min(orcamento, 1.45)
    if orcamento > 1.02:
        log.info(
            "Vegetacao: espacamento x%.2f para caber em %d arvores", orcamento, settings.max_trees
        )
    # Dois conjuntos de modelo: o do nivel de detalhe pedido para arvore vista de
    # perto, e o barato para vegetacao em massa dentro de mata.
    prototypes = {
        "detalhe": TreePrototypes(settings.detail),
        "massa": TreePrototypes("low", trunk_sides=MASS_TRUNK_SIDES),
    }
    palette = ctx.palette
    n_canopies = palette.canopy_count()

    # O impostor (arvore como imagem num quad de 2 triangulos) foi implementado
    # e descartado: o quad horizontal deita em vista obliqua, que e como este
    # mapa e olhado, e nem o viewport do projeto nem o renderizador de software
    # fazem alpha test - entao o recorte aparecia como quadrado preto. O modulo
    # `generation/impostor.py` ficou documentado para quem quiser retomar.
    atlas = None
    impostor_faces = 0

    # (posicao x, y, z, escala, rotacao) por (grupo de detalhe, especie, tronco)
    placements: dict[tuple[str, str, bool], list[np.ndarray]] = {}

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
        grupo = "massa" if context in MASS_CONTEXTS else "detalhe"
        com_tronco = True  # o tronco encolhe na massa, mas nunca some
        for index, name in enumerate(names):
            mask = chosen == index
            if not mask.any():
                continue
            low, high = SPECIES_HEIGHT[name]
            heights = rng.uniform(low * scale, high * scale, size=int(mask.sum()))
            placements.setdefault((grupo, name, com_tronco), []).append(
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
    shell_faces = 0
    for item in plano:
        spacing = base_spacing * SPACING[item.kind]
        rng = ctx.rng(item.osm_id, int(item.scatter_area) + 1)

        if item.shell is not None and not item.shell.is_empty:
            shell_faces += _canopy_surface(builder, ctx, item.shell, rng, palette)

        if item.scatter is None or item.scatter.is_empty:
            continue
        for poly in _as_polygons(item.scatter):
            # O piso e o tamanho de uma copa, nao o do espacamento. Descartar
            # tudo que for menor que o espacamento apagava justamente a arvore
            # de quintal - que e a maioria da vegetacao de uma cidade pequena, e
            # que sai fatiada em cacos ao recortar contra as casas.
            if poly.area < MIN_PATCH_M2:
                continue
            points = _scatter(poly, spacing, rng)
            if len(points) and ctx.imagery is not None:
                # A foto decide onde ha copa de verdade: campo de futebol e
                # patio dentro do parque deixam de receber arvore.
                verde = _greenness(ctx.imagery, points)
                points = points[rng.random(len(points)) < verde]
            place(points, rng, context=item.kind)

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
    for (grupo, species, com_tronco), chunks in placements.items():
        chunks = [c for c in chunks if len(c)]
        if not chunks:
            continue
        rows = np.concatenate(chunks, axis=0)
        if len(rows) + total > settings.max_trees:
            rows = rows[: max(settings.max_trees - total, 0)]
        if not len(rows):
            continue
        total += len(rows)

        modelos = prototypes[grupo]
        formas = modelos.variants(species)
        indices = np.arange(len(rows))
        variant_index = indices % len(formas)

        if com_tronco:
            builder.add_instances(palette.trunk, modelos.trunk(species), rows)

        # Duas repeticoes a quebrar: a cor e a silhueta. Dividir por (cor x
        # variante) da cor_n x variante_n combinacoes sem custar uma instancia a
        # mais - continua tudo assado na mesma malha.
        offset = abs(hash(species)) % max(n_canopies, 1)
        canopy_index = (indices + offset) % n_canopies
        for i in range(n_canopies):
            for v in range(len(formas)):
                subset = rows[(canopy_index == i) & (variant_index == v)]
                if len(subset):
                    builder.add_instances(palette.canopy(i), formas[v], subset)

    if impostor_faces:
        log.info(
            "Impostor: %d arvores em %d triangulos (%.1f por arvore)",
            total, impostor_faces, impostor_faces / max(total, 1),
        )
        ctx.report(f"{total} arvores (impostor, {impostor_faces} triangulos)", 1.0)
    elif shell_faces:
        # O dossel nao conta como arvore, mas conta muito no arquivo: sem ele
        # essas mesmas manchas custariam alguns milhares de instancias.
        log.info("Dossel: %d triangulos cobrindo o miolo das matas", shell_faces)
        ctx.report(f"{total} arvores + dossel ({shell_faces // 1000}k triangulos)", 1.0)
    else:
        ctx.report(f"{total} arvores", 1.0)
    return total
