"""A deteccao por imagem so vale onde nenhuma fonte de contorno chegou.

Ela tem 49,6% de precisao medida contra o Overture: sobre um bairro ja mapeado,
nao acrescenta predio que faltasse - espalha retangulo claro sobre areia e solo
exposto, que foi o defeito relatado como "quadrados brancos pelo mapa".
"""

from __future__ import annotations

from shapely.geometry import Polygon, box

from mapforge.core.features import Building, FeatureKind, MapData
from mapforge.core.geo import BBox
from mapforge.pipeline import GAP_CELL_M, _fill_only_gaps

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


def _mapa(*footprints) -> MapData:
    data = MapData(bbox=BBOX)
    for i, fp in enumerate(footprints):
        data.buildings.append(
            Building(osm_id=i + 1, kind=FeatureKind.BUILDING, tags={}, footprint=fp)
        )
    return data


def _quadrado(x: float, y: float, lado: float = 10.0) -> Polygon:
    return box(x, y, x + lado, y + lado)


def test_sem_nenhum_contorno_tudo_e_mantido():
    """Cidade que nenhuma fonte cobre: a deteccao e o que povoa a cena."""
    detectados = [_quadrado(0, 0), _quadrado(50, 50), _quadrado(-120, 80)]

    assert _fill_only_gaps(detectados, _mapa(), BBOX) == detectados


def test_onde_ja_ha_contorno_a_deteccao_e_descartada():
    existente = _quadrado(0, 0, 20)
    detectados = [_quadrado(5, 5), _quadrado(30, 30)]  # mesma celula do existente

    assert _fill_only_gaps(detectados, _mapa(existente), BBOX) == []


def test_a_deteccao_sobrevive_na_celula_vizinha_vazia():
    """O corte e por regiao: um bairro coberto nao apaga o bairro vizinho vazio."""
    existente = _quadrado(0, 0, 20)
    longe = _quadrado(GAP_CELL_M * 1.5, GAP_CELL_M * 1.5)
    detectados = [_quadrado(5, 5), longe]

    aceitos = _fill_only_gaps(detectados, _mapa(existente), BBOX)

    assert aceitos == [longe]


def test_lista_vazia_nao_quebra():
    assert _fill_only_gaps([], _mapa(_quadrado(0, 0)), BBOX) == []


def test_predio_sem_contorno_e_ignorado_na_contagem():
    """Um Building sem footprint nao pode contar como cobertura."""
    data = MapData(bbox=BBOX)
    data.buildings.append(
        Building(osm_id=1, kind=FeatureKind.BUILDING, tags={}, footprint=None)
    )
    detectados = [_quadrado(0, 0)]

    assert _fill_only_gaps(detectados, data, BBOX) == detectados
