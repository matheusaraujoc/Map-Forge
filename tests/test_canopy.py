"""Deteccao de vegetacao na imagem e o orcamento de arvores."""

import numpy as np
from shapely.geometry import Polygon

from mapforge.core.geo import BBox
from mapforge.generation.vegetation import (
    SHELL_BORDER_M,
    SPACING,
    _scatter,
    _spacing_for_budget,
    _split_shell,
)
from mapforge.imagery.canopy import CanopyMap, _polygonize, detect_canopy

BBOX = BBox(-2.9100, -41.9180, -2.8974, -41.9054)


class GeoFalso:
    """GeoImage minima: so o que canopy.py usa."""

    def __init__(self, array, meters_per_pixel=1.0):
        self.array = array
        self.meters_per_pixel = meters_per_pixel
        self.size = (array.shape[1], array.shape[0])


def _imagem(largura=120, altura=120):
    """Metade esquerda mata texturada, metade direita gramado liso."""
    rng = np.random.default_rng(4)
    img = np.zeros((altura, largura, 3), dtype=np.uint8)
    # Gramado: verde claro e homogeneo.
    img[:, :] = (120, 175, 90)
    # Mata: verde escuro com granulacao forte.
    ruido = rng.integers(-38, 38, size=(altura, largura // 2))
    img[:, : largura // 2, 0] = np.clip(28 + ruido, 0, 255)
    img[:, : largura // 2, 1] = np.clip(74 + ruido, 0, 255)
    img[:, : largura // 2, 2] = np.clip(34 + ruido, 0, 255)
    return GeoFalso(img)


def test_separa_copa_de_gramado():
    mapa = detect_canopy(_imagem(), map_data=None, cell_m=4.0, min_patch_m2=50.0)

    assert mapa.canopy.any() and mapa.grass.any()
    # A copa fica na metade esquerda, o gramado na direita.
    assert mapa.canopy[:, :60].mean() > 0.8
    assert mapa.canopy[:, 60:].mean() < 0.05
    assert mapa.grass[:, 60:].mean() > 0.8


def test_devolve_poligonos_em_metros():
    mapa = detect_canopy(_imagem(), map_data=None, cell_m=4.0, min_patch_m2=50.0)

    assert mapa.canopy_polygons
    total = sum(p.area for p in mapa.canopy_polygons)
    # A metade esquerda de uma imagem 120x120 a 1 m/px tem ~7.200 m2.
    assert 5_000 < total < 8_000
    assert mapa.canopy_area > 0
    assert "copa" in mapa.summary()


def test_celulas_vizinhas_viram_um_poligono_so():
    """As bordas partilhadas tem de bater bit a bit, senao a mata sai listrada."""
    mask = np.ones((40, 40), dtype=bool)
    polys = _polygonize(mask, GeoFalso(np.zeros((40, 40, 3), dtype=np.uint8)), 4.0, 10.0)

    assert len(polys) == 1
    # 40 x 40 m: o fechamento nao pode comer a celula da borda. O arredondamento
    # dos cantos tira alguns por cento, e so.
    assert 0.88 * 1_600 < polys[0].area <= 1_600


def test_contorno_sai_arredondado_e_nao_em_escada():
    """A uniao de celulas quadradas deixa degraus que aparecem no modelo 3D."""
    mask = np.zeros((60, 60), dtype=bool)
    # Diagonal em blocos: o caso que produz a escada.
    for i in range(12):
        mask[i * 5 : (i + 1) * 5, : (i + 1) * 5] = True

    geo = GeoFalso(np.zeros((60, 60, 3), dtype=np.uint8))
    polys = _polygonize(mask, geo, 5.0, 50.0)

    assert polys
    maior = max(polys, key=lambda p: p.area)
    # Uma escada de 12 degraus tem ~24 vertices so na diagonal; arredondada e
    # simplificada, o contorno inteiro cabe em bem menos.
    assert len(maior.exterior.coords) < 24


def test_imagem_sem_verde_nao_inventa_vegetacao():
    cinza = GeoFalso(np.full((60, 60, 3), 150, dtype=np.uint8))
    mapa = detect_canopy(cinza, map_data=None)

    assert not mapa.canopy.any()
    assert not mapa.grass.any()
    assert mapa.canopy_polygons == []


def test_orcamento_nao_mexe_quando_ja_cabe():
    assert _spacing_for_budget([("forest", 10_000.0)], 0, 12.0, 40_000) == 1.0


def test_orcamento_afasta_as_arvores_em_vez_de_cortar_a_lista():
    area = 4_000_000.0  # 400 ha
    fator = _spacing_for_budget([("forest", area)], 0, 12.0, 5_000)

    assert fator > 1.0
    # Com o espacamento corrigido, a estimativa passa a caber no teto.
    passo = 12.0 * fator * SPACING["forest"]
    assert area / (passo * passo) * 0.72 <= 5_001


def test_mancha_pequena_ainda_recebe_uma_arvore():
    """Com jitter largo, o quintal perdia todo ponto e ficava pelado."""
    from shapely.geometry import Point

    quintal = Polygon([(0, 0), (9, 0), (9, 9), (0, 9)])
    rng = np.random.default_rng(0)
    for _ in range(20):
        pontos = _scatter(quintal, 12.0, rng)
        assert len(pontos) >= 1
        for x, y in pontos:
            assert quintal.distance(Point(x, y)) < 1e-6


def test_mata_pesa_mais_que_parque_no_orcamento():
    # Mata e mais densa que parque, entao a mesma area pede mais arvore.
    assert SPACING["forest"] < SPACING["park"]
    mata = _spacing_for_budget([("forest", 100_000.0)], 0, 12.0, 100)
    parque = _spacing_for_budget([("park", 100_000.0)], 0, 12.0, 100)
    assert mata > parque > 1.0


def test_dossel_troca_o_miolo_da_mata_por_uma_superficie():
    """Mata grande: o miolo vira dossel, so a faixa da borda recebe arvore."""
    lado = 300.0
    mata = Polygon([(0, 0), (lado, 0), (lado, lado), (0, lado)])
    miolo, faixa = _split_shell(mata, SHELL_BORDER_M)

    assert miolo is not None
    assert miolo.area > 0.6 * mata.area  # o miolo e a maior parte
    assert abs(miolo.area + faixa.area - mata.area) < 1.0


def test_quintal_nao_tem_miolo():
    quintal = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    miolo, faixa = _split_shell(quintal, SHELL_BORDER_M)

    assert miolo is None
    assert faixa.equals(quintal)


class _CtxDossel:
    """Contexto minimo: so o que `_canopy_surface` consulta."""

    class _Palette:
        def canopy_count(self):
            return 2

        def canopy(self, i):
            from mapforge.core.mesh import Material

            return Material(name=f"copa{i}", color=(0.2, 0.5, 0.2))

    draped = False
    terrain = None
    imagery = None
    palette = _Palette()


def test_dossel_custa_muito_menos_que_arvore_individual():
    from mapforge.core.mesh import MeshBuilder
    from mapforge.generation.vegetation import SHELL_CELL_M, _canopy_surface

    miolo = Polygon([(0, 0), (200, 0), (200, 200), (0, 200)])  # 4 ha
    builder = MeshBuilder()
    faces = _canopy_surface(
        builder, _CtxDossel, miolo, np.random.default_rng(2), _CtxDossel.palette
    )

    # ~2 triangulos por celula, mais a saia.
    celulas = (200 / SHELL_CELL_M) ** 2
    assert faces > celulas  # cobriu a area
    assert faces < 4 * celulas  # sem explodir

    # A mesma area com arvore a cada 12,5 m custaria muito mais.
    arvores = (200 / 12.5) ** 2 * 0.72
    assert faces < arvores * 33  # 33 = triangulos do prototipo barato


def test_dossel_ondula_em_vez_de_ser_um_plano():
    from mapforge.core.mesh import MeshBuilder
    from mapforge.generation.vegetation import SHELL_RELIEF, _canopy_surface

    miolo = Polygon([(0, 0), (200, 0), (200, 200), (0, 200)])
    builder = MeshBuilder()
    _canopy_surface(builder, _CtxDossel, miolo, np.random.default_rng(5), _CtxDossel.palette)

    grupos = builder.build()
    alturas = np.concatenate([g.vertices[:, 2] for g in grupos.values()])
    # A ondulacao tem de aparecer, sem virar montanha.
    assert alturas.std() > 0.5
    assert alturas.max() - alturas.min() < 4 * SHELL_RELIEF + 12.0
