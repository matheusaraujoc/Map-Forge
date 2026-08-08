"""Testes do classificador 2D da imagem de satelite. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import LineString, box

from mapforge.core.features import FeatureKind, MapData, River, Road, RoadClass, Water
from mapforge.core.geo import BBox
from mapforge.imagery.sampling import GeoImage
from mapforge.imagery.segment import (
    CLASSES,
    INDEX,
    PALETTE,
    classify,
    local_variation,
    render,
    rgb_to_hsv,
)

BBOX = BBox(-2.8930, -41.9080, -2.8890, -41.9040)

TELHA = (198, 104, 72)
MATA = (36, 78, 40)
GRAMA = (120, 172, 92)
LAJE = (226, 224, 218)
ASFALTO = (74, 76, 82)
AGUA_AZUL = (52, 96, 142)
RIO_TURVO = (108, 116, 84)  # rio de planicie: marrom-esverdeado, nao azul
SOMBRA = (26, 26, 30)


# ------------------------------------------------------------------- HSV


def test_hsv_de_cores_conhecidas():
    rgb = np.array([[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]])
    hue, sat, val = rgb_to_hsv(rgb)
    assert hue[0, 0] == pytest.approx(0.0)
    assert hue[0, 1] == pytest.approx(120.0)
    assert hue[0, 2] == pytest.approx(240.0)
    assert np.allclose(sat, 1.0)
    assert np.allclose(val, 1.0)


def test_cinza_nao_tem_saturacao():
    _, sat, val = rgb_to_hsv(np.array([[[0.5, 0.5, 0.5]]]))
    assert sat[0, 0] == pytest.approx(0.0)
    assert val[0, 0] == pytest.approx(0.5)


def test_preto_nao_quebra():
    hue, sat, val = rgb_to_hsv(np.array([[[0.0, 0.0, 0.0]]]))
    assert np.isfinite(hue).all() and np.isfinite(sat).all() and np.isfinite(val).all()


# -------------------------------------------------------------- textura


def test_superficie_lisa_tem_variacao_baixa():
    liso = np.full((40, 40), 0.6)
    assert local_variation(liso, 2).mean() < 1e-6


def test_superficie_texturada_tem_variacao_alta():
    rng = np.random.default_rng(0)
    ruidoso = rng.normal(0.6, 0.12, size=(40, 40))
    assert local_variation(ruidoso, 2).mean() > 0.05


# ---------------------------------------------------------- classificacao


def _imagem(cor, tamanho=64) -> GeoImage:
    array = np.full((tamanho, tamanho, 3), cor, dtype=np.uint8)
    return GeoImage(image=Image.fromarray(array), bbox=BBOX, zoom=18, provider="teste")


def _classe_dominante(seg) -> str:
    contagem = seg.counts()
    chave = max(contagem, key=contagem.get)
    return chave


@pytest.mark.parametrize(
    "cor,esperado",
    [
        (TELHA, "telha"),
        (MATA, "mata"),
        (GRAMA, "grama"),
        (LAJE, "laje"),
        (AGUA_AZUL, "agua"),
        (SOMBRA, "sombra"),
    ],
)
def test_cor_chapada_cai_na_classe_certa(cor, esperado):
    assert _classe_dominante(classify(_imagem(cor))) == esperado


def test_telha_e_solo_vermelho_nao_caem_na_mesma_classe():
    """O caso dificil no Brasil: terra e vermelha como a ceramica.

    O que separa e a textura - telhado e superficie fabricada, lisa.
    """
    rng = np.random.default_rng(3)
    # Solo: mesma familia de matiz, porem texturado e menos saturado.
    base = np.array([168, 116, 92], dtype=np.float64)
    ruido = rng.normal(0, 26, size=(64, 64, 1))
    solo = np.clip(base + ruido, 0, 255).astype(np.uint8)
    geo_solo = GeoImage(image=Image.fromarray(solo), bbox=BBOX, zoom=18, provider="t")

    assert _classe_dominante(classify(_imagem(TELHA))) == "telha"
    assert _classe_dominante(classify(geo_solo)) != "telha"


def test_fracoes_somam_um():
    seg = classify(_imagem(TELHA))
    assert sum(seg.fractions().values()) == pytest.approx(1.0)


def test_mascara_seleciona_as_classes_pedidas():
    seg = classify(_imagem(MATA))
    assert seg.mask("mata").any()
    assert not seg.mask("agua").any()


def test_mascara_de_classe_inexistente_e_vazia():
    seg = classify(_imagem(MATA))
    assert not seg.mask("nao_existe").any()


def test_desenho_usa_a_paleta_das_classes():
    seg = classify(_imagem(MATA))
    rgb = seg.to_rgb()
    assert tuple(rgb[0, 0]) == CLASSES[INDEX["mata"]].color


# ------------------------------------------------- autoridade do mapa vetorial


def _map_data_com_rio() -> MapData:
    data = MapData(bbox=BBOX)
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    data.waters.append(
        Water(osm_id=1, kind=FeatureKind.WATER, tags={},
              geometry=box(-half_w, -half_h, half_w, 0))
    )
    return data


def test_rio_turvo_e_corrigido_pelo_mapa():
    """Regressao: rio de planicie e marrom-esverdeado e caia em vegetacao densa."""
    geo = _imagem(RIO_TURVO)
    sem_mapa = classify(geo)
    assert _classe_dominante(sem_mapa) != "agua"  # a cor sozinha erra

    com_mapa = classify(geo, _map_data_com_rio())
    assert com_mapa.mask("agua").any()


def test_via_do_mapa_sobrescreve_a_cor():
    data = MapData(bbox=BBOX)
    data.roads.append(
        Road(osm_id=1, kind=FeatureKind.ROAD, tags={},
             centerline=LineString([(-100, 0), (100, 0)]), road_class=RoadClass.PRIMARY)
    )
    seg = classify(_imagem(GRAMA), data)
    assert seg.mask("via").any()


def test_sem_mapa_nao_ha_classe_de_via():
    seg = classify(_imagem(GRAMA))
    assert not seg.mask("via").any()


# ------------------------------------------------------------------ desenho


def test_render_gera_arquivo(tmp_path):
    geo = _imagem(TELHA)
    caminho = render(classify(geo), tmp_path / "seg.png", geo=geo)
    assert caminho.exists()
    imagem = Image.open(caminho)
    # Lado a lado: mais largo que a imagem sozinha, e com faixa de legenda.
    assert imagem.width > geo.size[0]
    assert imagem.height > geo.size[1]


def test_render_so_do_mapa_fica_do_tamanho_da_imagem(tmp_path):
    geo = _imagem(TELHA)
    caminho = render(classify(geo), tmp_path / "seg.png", geo=geo, side_by_side=False)
    imagem = Image.open(caminho)
    assert imagem.width == max(geo.size[0], 420)


def test_render_desenha_os_contornos_detectados(tmp_path):
    geo = _imagem(TELHA)
    seg = classify(geo)
    sem = np.asarray(render(seg, None, geo=geo, side_by_side=False).convert("RGB"))
    com = np.asarray(
        render(seg, None, geo=geo, side_by_side=False, polygons=[box(-40, -40, 40, 40)]).convert("RGB")
    )
    assert not np.array_equal(sem, com)


def test_paleta_tem_uma_cor_por_classe():
    assert len(PALETTE) == len(CLASSES)
    assert len({c.color for c in CLASSES}) == len(CLASSES)
