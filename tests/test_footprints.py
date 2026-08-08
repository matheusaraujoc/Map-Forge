"""Testes dos contornos extras, formas de telhado e especies de arvore. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Point, Polygon, box

from mapforge.core.features import Building, FeatureKind, MapData, Park, Tree
from mapforge.core.geo import BBox, LocalProjection
from mapforge.data.footprints import merge_into, quadkey, quadkeys_for_bbox
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation.vegetation import SPECIES_HEIGHT, SPECIES_MIX, TreePrototypes

BBOX = BBox(-21.1150, -44.1830, -21.1060, -44.1720)


# ------------------------------------------------------------------ quadkeys


def test_quadkey_tem_o_tamanho_do_zoom():
    assert len(quadkey(-21.11, -44.17, zoom=9)) == 9
    assert len(quadkey(-21.11, -44.17, zoom=12)) == 12


def test_quadkey_conhecido_de_tiradentes():
    # Confirmado contra o indice publicado da Microsoft.
    assert quadkey(-21.1104, -44.1776, zoom=9) == "211022221"


def test_quadkey_usa_apenas_digitos_validos():
    key = quadkey(-19.95, -43.41, zoom=9)
    assert set(key) <= {"0", "1", "2", "3"}


def test_bbox_pequena_cabe_num_quadkey():
    keys = quadkeys_for_bbox(BBOX)
    assert len(keys) == 1


def test_bbox_na_divisa_pega_mais_de_um_quadkey():
    # Uma bbox larga o bastante cruza a fronteira entre tiles de z9.
    larga = BBox(-21.5, -45.5, -20.5, -43.5)
    assert len(quadkeys_for_bbox(larga)) > 1


# --------------------------------------------------------------------- fusao


def _map_data_com(*polys) -> MapData:
    data = MapData(bbox=BBOX)
    for i, poly in enumerate(polys):
        data.buildings.append(
            Building(osm_id=100 + i, kind=FeatureKind.BUILDING, tags={}, footprint=poly)
        )
    return data


def _footprint_latlon(projection: LocalProjection, poly: Polygon) -> dict:
    """Converte um poligono em metros de volta para graus, como vem do arquivo."""
    ring = [projection.unproject(x, y) for x, y in poly.exterior.coords]
    return {
        "polygon": Polygon([(lon, lat) for lat, lon in ring]),
        "confidence": 0.95,
        "height": -1.0,
    }


def test_merge_acrescenta_predio_que_o_osm_nao_tem():
    projection = LocalProjection.for_bbox(BBOX)
    data = _map_data_com(box(-100, -100, -80, -80))
    novo = _footprint_latlon(projection, box(50, 50, 70, 70))

    added = merge_into(data, [novo], projection)
    assert added == 1
    assert len(data.buildings) == 2


def test_merge_descarta_predio_que_o_osm_ja_tem():
    projection = LocalProjection.for_bbox(BBOX)
    existente = box(0, 0, 20, 20)
    data = _map_data_com(existente)
    # Mesmo predio, contorno levemente diferente.
    duplicado = _footprint_latlon(projection, box(1, 1, 21, 21))

    assert merge_into(data, [duplicado], projection) == 0
    assert len(data.buildings) == 1


def test_merge_marca_a_origem_e_usa_id_negativo():
    projection = LocalProjection.for_bbox(BBOX)
    data = _map_data_com()
    merge_into(data, [_footprint_latlon(projection, box(0, 0, 15, 15))], projection)

    novo = data.buildings[0]
    assert novo.osm_id < 0  # nao veio do OSM
    assert novo.tags["source"] == "microsoft"


def test_merge_ignora_contorno_minusculo():
    projection = LocalProjection.for_bbox(BBOX)
    data = _map_data_com()
    assert merge_into(data, [_footprint_latlon(projection, box(0, 0, 2, 2))], projection) == 0


def test_merge_sem_contornos_nao_faz_nada():
    data = _map_data_com(box(0, 0, 10, 10))
    assert merge_into(data, [], LocalProjection.for_bbox(BBOX)) == 0
    assert len(data.buildings) == 1


def test_merge_recorta_o_que_passa_da_bbox():
    projection = LocalProjection.for_bbox(BBOX)
    data = _map_data_com()
    half_w = BBOX.width_m / 2
    # Predio que atravessa a borda leste.
    atravessa = _footprint_latlon(projection, box(half_w - 10, 0, half_w + 40, 30))
    merge_into(data, [atravessa], projection)

    assert len(data.buildings) == 1
    assert data.buildings[0].footprint.bounds[2] <= half_w + 1e-6


def test_altura_do_conjunto_e_usada_quando_positiva():
    projection = LocalProjection.for_bbox(BBOX)
    data = _map_data_com()
    item = _footprint_latlon(projection, box(0, 0, 20, 20))
    item["height"] = 18.0
    merge_into(data, [item], projection)
    assert data.buildings[0].height == pytest.approx(18.0)


def test_altura_negativa_do_conjunto_e_ignorada():
    projection = LocalProjection.for_bbox(BBOX)
    data = _map_data_com()
    merge_into(data, [_footprint_latlon(projection, box(0, 0, 20, 20))], projection)
    # No Brasil o conjunto traz height = -1: nao pode virar altura de predio.
    assert data.buildings[0].height is None


# ------------------------------------------------------------- telhados novos


def _scene_com_telhado(shape: str, footprint: Polygon):
    data = MapData(bbox=BBOX)
    data.buildings.append(
        Building(
            osm_id=1,
            kind=FeatureKind.BUILDING,
            tags={"roof:shape": shape},
            footprint=footprint,
            height=9.0,
        )
    )
    settings = GenerationSettings(
        seed=3, detail="high", roads=False, water=False, vegetation=False, terrain=False
    )
    return build_scene(data, GenerationContext(BBOX, settings))


@pytest.mark.parametrize(
    "shape", ["flat", "gabled", "hipped", "pyramidal", "mansard", "gambrel", "skillion", "shed"]
)
def test_toda_forma_de_telhado_gera_geometria(shape):
    scene = _scene_com_telhado(shape, box(-15, -10, 15, 10))
    assert scene.triangle_count > 10


def test_mansarda_fica_mais_baixa_que_duas_aguas():
    """A mansarda tem terraco no topo; duas aguas sobe ate a cumeeira."""
    duas = _scene_com_telhado("gabled", box(-15, -10, 15, 10))
    mansarda = _scene_com_telhado("mansard", box(-15, -10, 15, 10))
    assert mansarda.bounds()[1][2] < duas.bounds()[1][2]


def test_uma_agua_e_assimetrica():
    scene = _scene_com_telhado("skillion", box(-15, -10, 15, 10))
    telhado = np.concatenate(
        [g.vertices for n, g in scene.groups.items() if n.startswith("roof_")]
    )
    # Uma agua so: um lado no topo da parede, o outro bem acima.
    assert telhado[:, 2].max() - telhado[:, 2].min() > 0.9


def test_forma_invalida_para_o_contorno_cai_para_outra():
    """Duas aguas num L viraria cumeeira torta; tem de virar piramidal ou plano."""
    ele = Polygon([(0, 0), (30, 0), (30, 12), (12, 12), (12, 30), (0, 30)])
    scene = _scene_com_telhado("gabled", ele)
    assert scene.triangle_count > 10  # gerou algo coerente, sem explodir


# ------------------------------------------------------------ especies de arvore


def test_todas_as_especies_tem_prototipo_valido():
    prototypes = TreePrototypes("high")
    for species in SPECIES_HEIGHT:
        for verts, faces in (prototypes.trunk(species), prototypes.canopy(species)):
            assert len(verts) >= 3
            assert len(faces) >= 1
            assert faces.max() < len(verts)  # nenhum indice fora do buffer


def test_prototipos_cabem_na_altura_normalizada():
    prototypes = TreePrototypes("high")
    for species in SPECIES_HEIGHT:
        verts = np.concatenate(
            [prototypes.trunk(species)[0], prototypes.canopy(species)[0]]
        )
        assert verts[:, 2].min() >= -0.05
        assert verts[:, 2].max() <= 1.25  # a copa pode passar um pouco de 1.0


def test_toda_mistura_de_especies_soma_um():
    for context, mix in SPECIES_MIX.items():
        assert sum(mix.values()) == pytest.approx(1.0, abs=1e-6), context
        for species in mix:
            assert species in SPECIES_HEIGHT, f"{context}: {species} sem faixa de altura"


def test_cipreste_e_mais_estreito_que_copa_larga():
    prototypes = TreePrototypes("high")
    largura = {}
    for species in ("cypress", "umbrella"):
        verts = prototypes.canopy(species)[0]
        largura[species] = verts[:, 0].max() - verts[:, 0].min()
    assert largura["cypress"] < largura["umbrella"]


def test_parque_produz_mais_de_uma_especie():
    data = MapData(bbox=BBOX)
    data.parks.append(
        Park(osm_id=1, kind=FeatureKind.PARK, tags={"leisure": "park"},
             geometry=box(-200, -200, 200, 200))
    )
    settings = GenerationSettings(
        seed=5, detail="high", roads=False, buildings=False, water=False, terrain=False
    )
    scene = build_scene(data, GenerationContext(BBOX, settings))
    assert scene.metadata["generated"]["trees"] > 50
    # Varias copas diferentes implicam varios grupos de material de copa.
    canopies = [n for n in scene.groups if n.startswith("canopy_")]
    assert len(canopies) >= 2


def test_arvore_de_rua_e_mais_baixa_que_a_de_bosque():
    from mapforge.generation.vegetation import CONTEXT_SCALE

    assert CONTEXT_SCALE["street"] < CONTEXT_SCALE["forest"]
