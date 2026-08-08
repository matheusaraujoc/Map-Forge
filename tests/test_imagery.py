"""Testes de imagem de satelite: matematica de tiles, amostragem e cores. Sem rede."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import box

from mapforge.core.geo import BBox
from mapforge.generation.coloring import quantize
from mapforge.imagery.sampling import (
    GeoImage,
    blend,
    build_ground_texture,
    harmonize,
    sample_area_color,
    sample_roof_colors,
)
from mapforge.imagery.tiles import (
    TileError,
    choose_zoom,
    get_provider,
    is_placeholder,
    lonlat_to_tile,
    meters_per_pixel,
    quadkey,
)

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


# ----------------------------------------------------------- math dos tiles


def test_tile_do_meridiano_e_equador():
    x, y = lonlat_to_tile(0.0, 0.0, 1)
    assert x == pytest.approx(1.0)
    assert y == pytest.approx(1.0)


def test_tile_cresce_para_leste_e_para_o_sul():
    x0, y0 = lonlat_to_tile(-20.0, -43.0, 12)
    x1, y1 = lonlat_to_tile(-21.0, -42.0, 12)
    assert x1 > x0  # mais a leste
    assert y1 > y0  # mais ao sul


def test_resolucao_dobra_a_cada_zoom():
    a = meters_per_pixel(-20.0, 15)
    b = meters_per_pixel(-20.0, 16)
    assert a / b == pytest.approx(2.0)


def test_quadkey_conhecido():
    # Exemplo da documentacao do Bing Maps.
    assert quadkey(3, 5, 3) == "213"


def test_choose_zoom_respeita_o_teto_do_provedor():
    provider = get_provider("esri")
    tiny = BBox.from_center(-20.0, -43.0, 30)
    assert choose_zoom(tiny, provider) <= provider.max_zoom


def test_choose_zoom_cai_para_areas_grandes():
    provider = get_provider("esri")
    small = choose_zoom(BBox.from_center(-20.0, -43.0, 200), provider, max_pixels=2048)
    large = choose_zoom(BBox.from_center(-20.0, -43.0, 4000), provider, max_pixels=2048)
    assert large < small


def test_provedor_desconhecido_avisa():
    with pytest.raises(TileError, match="esri"):
        get_provider("inexistente")


def test_custom_sem_variavel_de_ambiente_falha(monkeypatch):
    monkeypatch.delenv("MAPFORGE_TILE_URL", raising=False)
    with pytest.raises(TileError, match="MAPFORGE_TILE_URL"):
        get_provider("custom")


def test_custom_usa_a_variavel_de_ambiente(monkeypatch):
    monkeypatch.setenv("MAPFORGE_TILE_URL", "https://exemplo/{z}/{x}/{y}.jpg")
    provider = get_provider("custom")
    assert provider.name == "custom"
    assert "{z}" in provider.url


# ------------------------------------------------- deteccao de tile ausente


def _encode(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array.astype(np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_tile_cinza_uniforme_e_placeholder():
    # E o que o Esri devolve onde nao tem cobertura: cinza chapado com um texto.
    gray = np.full((256, 256, 3), 204, dtype=np.uint8)
    gray[100:110, 40:200] = 255  # a faixa de texto
    assert is_placeholder(_encode(gray))


def test_imagem_de_satelite_nao_e_placeholder():
    rng = np.random.default_rng(0)
    scene = rng.integers(0, 255, size=(256, 256, 3))
    assert not is_placeholder(_encode(scene))


def test_conteudo_ilegivel_conta_como_placeholder():
    assert is_placeholder(b"nao e uma imagem")


# ------------------------------------------------------------- amostragem


def _geo_image(width=200, height=200) -> GeoImage:
    """Metade oeste vermelha, metade leste azul."""
    array = np.zeros((height, width, 3), dtype=np.uint8)
    array[:, : width // 2] = (200, 30, 30)
    array[:, width // 2 :] = (30, 30, 200)
    return GeoImage(image=Image.fromarray(array), bbox=BBOX, zoom=18, provider="teste")


def test_to_pixel_mapeia_os_cantos():
    geo = _geo_image()
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    assert geo.to_pixel(-half_w, half_h) == pytest.approx((0.0, 0.0))  # noroeste
    assert geo.to_pixel(half_w, -half_h) == pytest.approx((199.0, 199.0))  # sudeste


def test_amostra_o_lado_certo_da_imagem():
    geo = _geo_image()
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    oeste = sample_area_color(geo, box(-half_w * 0.8, -20, -half_w * 0.4, 20))
    leste = sample_area_color(geo, box(half_w * 0.4, -20, half_w * 0.8, 20))
    assert oeste[0] > oeste[2]  # vermelho domina
    assert leste[2] > leste[0]  # azul domina


def test_amostra_fora_da_imagem_devolve_none():
    geo = _geo_image()
    assert sample_area_color(geo, box(100_000, 100_000, 100_010, 100_010)) is None


def test_sample_roof_colors_por_edificio():
    from mapforge.core.features import Building, FeatureKind

    geo = _geo_image()
    half_w = BBOX.width_m / 2
    buildings = [
        Building(osm_id=1, kind=FeatureKind.BUILDING, tags={},
                 footprint=box(-half_w * 0.7, -30, -half_w * 0.3, 30)),
        Building(osm_id=2, kind=FeatureKind.BUILDING, tags={},
                 footprint=box(half_w * 0.3, -30, half_w * 0.7, 30)),
    ]
    colors = sample_roof_colors(geo, buildings)
    assert set(colors) == {1, 2}
    assert colors[1][0] > colors[1][2]
    assert colors[2][2] > colors[2][0]


# ------------------------------------------------------------------- cores


def test_harmonize_clareia_cor_escura_demais():
    escura = harmonize((0.01, 0.01, 0.01))
    luma = 0.2126 * escura[0] + 0.7152 * escura[1] + 0.0722 * escura[2]
    assert luma >= 0.17


def test_harmonize_escurece_cor_estourada():
    clara = harmonize((1.0, 1.0, 1.0))
    luma = 0.2126 * clara[0] + 0.7152 * clara[1] + 0.0722 * clara[2]
    assert luma <= 0.83


def test_harmonize_mantem_tudo_no_intervalo():
    for color in [(0.0, 0.0, 0.0), (1.0, 0.0, 0.5), (0.3, 0.9, 0.2)]:
        assert all(0.0 <= c <= 1.0 for c in harmonize(color))


def test_blend_nos_extremos():
    estilo, foto = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    assert blend(estilo, foto, 0.0) == pytest.approx(estilo)
    assert blend(estilo, foto, 1.0) == pytest.approx(foto)
    assert blend(estilo, foto, 0.5) == pytest.approx((0.5, 0.0, 0.5))


def test_quantize_devolve_k_centros():
    rng = np.random.default_rng(1)
    colors = np.vstack([
        rng.normal((0.8, 0.2, 0.2), 0.02, size=(60, 3)),
        rng.normal((0.2, 0.2, 0.8), 0.02, size=(60, 3)),
    ])
    labels, centers = quantize(colors, k=2, seed=3)
    assert len(centers) == 2
    assert len(labels) == len(colors)
    # Os dois agrupamentos precisam ficar separados.
    assert len(set(labels[:60])) == 1
    assert len(set(labels[60:])) == 1
    assert labels[0] != labels[-1]


def test_quantize_com_menos_amostras_que_k():
    labels, centers = quantize(np.array([[0.5, 0.5, 0.5]]), k=8)
    assert len(centers) == 1
    assert labels.tolist() == [0]


def test_build_ground_texture_limita_o_tamanho():
    geo = _geo_image(width=6000, height=3000)
    texture = build_ground_texture(geo, max_size=1024)
    assert max(texture.size) <= 1024
