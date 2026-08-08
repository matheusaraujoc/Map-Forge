"""Perfil urbano: o teto de pavimentos medido nos proprios contornos."""

import numpy as np
from shapely.geometry import Polygon

from mapforge.core.features import Building, FeatureKind, MapData
from mapforge.core.geo import BBox
from mapforge.generation.buildings import _choose_levels
from mapforge.generation.context import GenerationSettings
from mapforge.generation.urban import MIN_SAMPLE, SCALES, profile_for

BBOX = BBox(-2.9100, -41.9180, -2.8974, -41.9054)  # ~1,4 km


def _quadrado(lado: float, i: int) -> Polygon:
    x = (i % 40) * 60.0 - 600.0
    y = (i // 40) * 60.0 - 600.0
    return Polygon([(x, y), (x + lado, y), (x + lado, y + lado), (x, y + lado)])


def _mapa(lados) -> MapData:
    md = MapData(bbox=BBOX)
    for i, lado in enumerate(lados):
        md.buildings.append(
            Building(
                osm_id=i + 1,
                kind=FeatureKind.BUILDING,
                tags={},
                footprint=_quadrado(lado, i),
            )
        )
    return md


def test_povoado_de_casas_pequenas_nao_ganha_torre():
    # 400 casas de ~100 m2: p99 fica bem abaixo do primeiro corte.
    perfil = profile_for(_mapa([10.0] * 400), BBOX)
    assert perfil.key == "povoado"
    assert perfil.max_levels == 2


def test_um_galpao_isolado_nao_promove_o_povoado():
    # O p99 resiste ao contorno unico gigante; o maximo nao resistiria.
    lados = [10.0] * 400 + [70.0]
    perfil = profile_for(_mapa(lados), BBOX)
    assert perfil.key == "povoado"


def test_contornos_grandes_promovem_a_regiao():
    # 100 predios de 1.600 m2: p99 acima do ultimo corte.
    perfil = profile_for(_mapa([40.0] * 100), BBOX)
    assert perfil.key == "grande"
    assert perfil.max_levels == 14


def test_amostra_pequena_cai_no_palpite_do_meio():
    perfil = profile_for(_mapa([10.0] * (MIN_SAMPLE - 1)), BBOX)
    assert perfil is SCALES[1]


def test_ocupacao_saturada_levanta_o_piso():
    # Contornos medios (p99 ~ 900 m2) mas cobrindo quase metade do solo.
    md = MapData(bbox=BBOX)
    lado = 30.0
    for i in range(300):
        x = (i % 20) * 32.0 - 320.0
        y = (i // 20) * 32.0 - 240.0
        md.buildings.append(
            Building(
                osm_id=i + 1,
                kind=FeatureKind.BUILDING,
                tags={},
                footprint=Polygon(
                    [(x, y), (x + lado, y), (x + lado, y + lado), (x, y + lado)]
                ),
            )
        )
    perfil = profile_for(md, BBox(-2.9010, -41.9090, -2.8974, -41.9054))
    assert perfil.occupancy >= 42.0
    assert perfil.max_levels >= SCALES[2].max_levels


def test_o_teto_do_perfil_limita_os_pavimentos_escolhidos():
    """Um contorno de 1.000 m2 pede a faixa BLOCK (4 a 14) - o povoado corta."""

    class Ctx:
        class style:
            height_scale = 1.0

        settings = GenerationSettings()
        shadow_heights: dict = {}
        urban = SCALES[0]  # povoado

    predio = Building(
        osm_id=1,
        kind=FeatureKind.BUILDING,
        tags={},
        footprint=_quadrado(32.0, 0),  # 1.024 m2
    )
    rng = np.random.default_rng(1)
    for _ in range(30):
        assert _choose_levels(predio, Ctx, rng) <= 2

    Ctx.urban = SCALES[3]  # cidade grande
    assert max(_choose_levels(predio, Ctx, rng) for _ in range(60)) > 2
