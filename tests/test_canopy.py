"""Deteccao de vegetacao na imagem e o orcamento de arvores."""

import numpy as np
from shapely.geometry import Polygon

from mapforge.core.features import Forest, Park, FeatureKind, MapData
from mapforge.core.geo import BBox
from mapforge.generation.vegetation import SPACING, _scatter, _spacing_for_budget
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
    # 40 x 40 m inteiros: o fechamento nao pode comer a celula da borda.
    assert polys[0].area == 1_600


def test_imagem_sem_verde_nao_inventa_vegetacao():
    cinza = GeoFalso(np.full((60, 60, 3), 150, dtype=np.uint8))
    mapa = detect_canopy(cinza, map_data=None)

    assert not mapa.canopy.any()
    assert not mapa.grass.any()
    assert mapa.canopy_polygons == []


def _mapa_com_mata(area_m2: float) -> MapData:
    lado = float(np.sqrt(area_m2))
    md = MapData(bbox=BBOX)
    md.forests.append(
        Forest(
            osm_id=-1,
            kind=FeatureKind.FOREST,
            tags={"source": "deteccao"},
            geometry=Polygon([(0, 0), (lado, 0), (lado, lado), (0, lado)]),
        )
    )
    return md


def test_orcamento_nao_mexe_quando_ja_cabe():
    assert _spacing_for_budget(_mapa_com_mata(10_000), 12.0, 40_000) == 1.0


def test_orcamento_afasta_as_arvores_em_vez_de_cortar_a_lista():
    md = _mapa_com_mata(4_000_000)  # 400 ha
    fator = _spacing_for_budget(md, 12.0, 5_000)

    assert fator > 1.0
    # Com o espacamento corrigido, a estimativa passa a caber no teto.
    passo = 12.0 * fator * SPACING["forest"]
    assert md.forests[0].geometry.area / (passo * passo) * 0.72 <= 5_001


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


def test_parque_e_mata_tem_espacamentos_diferentes():
    md = MapData(bbox=BBOX)
    quadrado = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    md.forests.append(
        Forest(osm_id=-1, kind=FeatureKind.FOREST, tags={}, geometry=quadrado)
    )
    md.parks.append(Park(osm_id=-2, kind=FeatureKind.PARK, tags={}, geometry=quadrado))

    # Mata e mais densa que parque, entao pesa mais no orcamento.
    assert SPACING["forest"] < SPACING["park"]
    assert _spacing_for_budget(md, 12.0, 1) > 1.0
