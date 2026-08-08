"""Testes do detector de edificios na imagem de satelite. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import LineString, Polygon, box

from mapforge.core.features import (
    Building,
    FeatureKind,
    MapData,
    Road,
    RoadClass,
    Water,
)
from mapforge.core.geo import BBox
from mapforge.generation import GenerationContext, GenerationSettings
from mapforge.generation.buildings import DETECTED_LEVELS, _choose_levels
from mapforge.imagery.detect import (
    MIN_AREA_M2,
    SOLID_FILL,
    SPLIT_AREA_M2,
    _roof_likelihood,
    _split_block,
    detect_buildings,
)
from mapforge.imagery.sampling import GeoImage

# ~200 m de lado: com a imagem sintetica de 400 px da 0.5 m/px, dentro da faixa
# em que o detector opera.
BBOX = BBox(-2.8903, -41.9056, -2.8885, -41.9038)

CERAMICA = (196, 104, 72)  # telha de barro
VEGETACAO = (58, 104, 52)
LAJE = (208, 206, 200)
SOLO = (150, 132, 108)


# ---------------------------------------------------------- classificacao de pixel


def _pixel(color) -> np.ndarray:
    return np.array([[list(color)]], dtype=np.float64) / 255.0


def test_telha_ceramica_e_telhado():
    assert _roof_likelihood(_pixel(CERAMICA))[0, 0]


def test_laje_clara_e_telhado():
    assert _roof_likelihood(_pixel(LAJE))[0, 0]


def test_vegetacao_nao_e_telhado():
    assert not _roof_likelihood(_pixel(VEGETACAO))[0, 0]


def test_asfalto_escuro_nao_e_telhado():
    assert not _roof_likelihood(_pixel((62, 64, 68)))[0, 0]


def test_agua_nao_e_telhado():
    assert not _roof_likelihood(_pixel((48, 92, 128)))[0, 0]


# --------------------------------------------------------------- divisao da mancha


def _rect(width: float, height: float) -> Polygon:
    return box(0, 0, width, height)


def test_mancha_grande_e_irregular_vira_varias_casas():
    """Quarteirao de casas geminadas nao pode virar um predio so."""
    pieces = _split_block(_rect(42, 30), fill=0.6)
    assert len(pieces) > 4
    assert all(p.area > MIN_AREA_M2 for p in pieces)


def test_mancha_grande_mas_solida_continua_inteira():
    """Telhado limpo e cheio e galpao ou escola de verdade."""
    assert len(_split_block(_rect(42, 30), fill=SOLID_FILL + 0.05)) == 1


def test_casa_pequena_nao_e_dividida():
    assert len(_split_block(_rect(9, 8), fill=0.6)) == 1


def test_mancha_no_limiar_de_area_nao_e_dividida():
    lado = (SPLIT_AREA_M2 * 0.9) ** 0.5
    assert len(_split_block(_rect(lado, lado), fill=0.5)) == 1


def test_divisao_preserva_a_area_aproximada():
    original = _rect(40, 30)
    pieces = _split_block(original, fill=0.5)
    total = sum(p.area for p in pieces)
    # As frestas entre as casas tiram um pouco, mas nao pode sumir metade.
    assert 0.75 * original.area < total < original.area


def test_divisao_nao_produz_pecas_sobrepostas():
    pieces = _split_block(_rect(40, 30), fill=0.5)
    for i, a in enumerate(pieces):
        for b in pieces[i + 1 :]:
            assert a.intersection(b).area < 1e-6


# -------------------------------------------------------------------- deteccao


def _geo_image(scene: np.ndarray) -> GeoImage:
    return GeoImage(image=Image.fromarray(scene), bbox=BBOX, zoom=18, provider="teste")


def _scene_with_roofs(size: int = 400) -> tuple[np.ndarray, float]:
    """Fundo de vegetacao com uma fileira de telhados ao longo de uma rua."""
    scene = np.zeros((size, size, 3), dtype=np.uint8)
    scene[:, :] = VEGETACAO
    meters_per_pixel = BBOX.width_m / size

    # Rua horizontal no meio.
    faixa = int(6 / meters_per_pixel)
    meio = size // 2
    scene[meio - faixa // 2 : meio + faixa // 2, :] = (70, 72, 76)

    # Telhados de ~10x10 m logo acima da rua.
    lado = int(10 / meters_per_pixel)
    topo = meio - faixa // 2 - lado - 2
    for i in range(4):
        x0 = 40 + i * (lado + 12)
        scene[topo : topo + lado, x0 : x0 + lado] = CERAMICA
    return scene, meters_per_pixel


def _map_data_com_rua() -> MapData:
    data = MapData(bbox=BBOX)
    data.roads.append(
        Road(
            osm_id=1,
            kind=FeatureKind.ROAD,
            tags={},
            centerline=LineString([(-BBOX.width_m / 2, 0), (BBOX.width_m / 2, 0)]),
            road_class=RoadClass.RESIDENTIAL,
        )
    )
    return data


def test_detecta_os_telhados_da_cena_sintetica():
    scene, _ = _scene_with_roofs()
    result = detect_buildings(_geo_image(scene), _map_data_com_rua())
    assert len(result.polygons) >= 3


def test_nao_detecta_nada_em_cena_so_de_vegetacao():
    size = 400
    scene = np.zeros((size, size, 3), dtype=np.uint8)
    scene[:, :] = VEGETACAO
    result = detect_buildings(_geo_image(scene), _map_data_com_rua())
    assert result.polygons == []


def test_resolucao_grossa_desliga_a_deteccao():
    # Imagem minuscula para a mesma bbox: metros por pixel alto demais.
    scene = np.full((20, 20, 3), CERAMICA, dtype=np.uint8)
    result = detect_buildings(_geo_image(scene), _map_data_com_rua())
    assert result.polygons == []
    assert "resolucao" in result.note


def test_telhado_ja_mapeado_no_osm_nao_e_duplicado():
    scene, mpp = _scene_with_roofs()
    data = _map_data_com_rua()

    # Descobre onde o detector acha os telhados e marca um deles como existente.
    livre = detect_buildings(_geo_image(scene), data)
    assert livre.polygons
    alvo = livre.polygons[0]
    data.buildings.append(
        Building(osm_id=1, kind=FeatureKind.BUILDING, tags={}, footprint=alvo)
    )

    depois = detect_buildings(_geo_image(scene), data)
    assert len(depois.polygons) < len(livre.polygons)
    assert depois.rejected["ja_existe"] >= 1


def test_agua_do_mapa_bloqueia_deteccao():
    scene, _ = _scene_with_roofs()
    data = _map_data_com_rua()
    livre = len(detect_buildings(_geo_image(scene), data).polygons)

    data.waters.append(
        Water(
            osm_id=9,
            kind=FeatureKind.WATER,
            tags={},
            geometry=box(-BBOX.width_m / 2, -BBOX.height_m / 2, BBOX.width_m / 2, BBOX.height_m / 2),
        )
    )
    assert len(detect_buildings(_geo_image(scene), data).polygons) < livre


def test_telhado_longe_de_qualquer_via_e_descartado():
    scene, _ = _scene_with_roofs()
    data = _map_data_com_rua()
    livre = len(detect_buildings(_geo_image(scene), data).polygons)
    # Encolhendo o alcance ate quase zero, nada sobrevive.
    apertado = detect_buildings(_geo_image(scene), data, max_road_distance=0.5)
    assert len(apertado.polygons) < livre


def test_sem_imagem_devolve_vazio():
    result = detect_buildings(None, _map_data_com_rua())
    assert result.polygons == []
    assert result.note


# --------------------------------------------------------------------- altura


def _levels(source: str | None, area: float) -> float:
    tags = {"source": source} if source else {}
    lado = area**0.5
    building = Building(
        osm_id=1,
        kind=FeatureKind.BUILDING,
        tags=tags,
        footprint=box(0, 0, lado, lado),
        building_type="yes",
    )
    ctx = GenerationContext(BBOX, GenerationSettings(seed=4))
    return _choose_levels(building, ctx, ctx.rng(1))


def test_contorno_detectado_grande_continua_terreo():
    """A area de uma mancha detectada nao diz nada sobre altura."""
    niveis = _levels("deteccao", 1200.0)
    assert DETECTED_LEVELS[0] <= niveis <= DETECTED_LEVELS[1]


def test_predio_do_osm_com_a_mesma_area_pode_ser_alto():
    assert _levels(None, 1200.0) > DETECTED_LEVELS[1]


def test_altura_das_tags_vence_a_origem():
    building = Building(
        osm_id=1,
        kind=FeatureKind.BUILDING,
        tags={"source": "deteccao"},
        footprint=box(0, 0, 30, 30),
        levels=7,
    )
    ctx = GenerationContext(BBOX, GenerationSettings(seed=4))
    assert _choose_levels(building, ctx, ctx.rng(1)) == pytest.approx(7.0)
