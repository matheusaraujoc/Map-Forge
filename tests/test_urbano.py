"""Testes dos equipamentos urbanos, estruturas verticais e elementos rurais."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from mapforge.core.features import (
    Building,
    Farmland,
    FeatureKind,
    MapData,
    Road,
    RoadClass,
    Structure,
    StructureKind,
)
from mapforge.core.geo import BBox, LocalProjection
from mapforge.data.parser import parse_osm
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation.archetypes import archetype_for
from mapforge.generation.structures import DEFAULT_HEIGHT

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


def _scene(data: MapData, **kwargs) -> object:
    settings = GenerationSettings(seed=3, detail="high", **kwargs)
    return build_scene(data, GenerationContext(BBOX, settings))


# ---------------------------------------------------------------- arquetipos


@pytest.mark.parametrize(
    "tags,esperado",
    [
        ({"amenity": "hospital"}, "hospital"),
        ({"amenity": "police"}, "policia"),
        ({"amenity": "fire_station"}, "bombeiros"),
        ({"emergency": "fire_station"}, "bombeiros"),
        ({"amenity": "place_of_worship"}, "igreja"),
        ({"amenity": "school"}, "escola"),
        ({"amenity": "townhall"}, "prefeitura"),
        ({"amenity": "fuel"}, "posto"),
        ({"building": "church"}, "igreja"),
        ({"building": "barn"}, "celeiro"),
        ({"building": "warehouse"}, "galpao"),
        ({"tourism": "hotel"}, "hotel"),
        ({"shop": "bakery"}, "loja"),
    ],
)
def test_arquetipo_reconhecido(tags, esperado):
    archetype = archetype_for(tags)
    assert archetype is not None
    assert archetype.name == esperado


def test_predio_comum_nao_tem_arquetipo():
    assert archetype_for({"building": "yes"}) is None
    assert archetype_for({}) is None


def test_amenity_vence_building_generico():
    """`building=yes` num hospital e comum; o amenity e que informa."""
    archetype = archetype_for({"building": "yes", "amenity": "hospital"})
    assert archetype.name == "hospital"


def test_quartel_tem_faixa_de_altura_baixa():
    assert archetype_for({"amenity": "fire_station"}).levels[1] <= 2


def test_hospital_pode_ser_mais_alto_que_creche():
    assert (
        archetype_for({"amenity": "hospital"}).levels[1]
        > archetype_for({"amenity": "kindergarten"}).levels[1]
    )


def test_galpao_tem_telhado_ditado_pela_funcao():
    assert archetype_for({"building": "warehouse"}).roof == "skillion"


def test_quartel_tem_cor_propria():
    cor = archetype_for({"amenity": "fire_station"}).wall_color
    assert cor is not None
    assert cor[0] > cor[1] and cor[0] > cor[2]  # vermelho domina


def _predio(tags: dict, area: float = 400.0) -> MapData:
    lado = area**0.5
    data = MapData(bbox=BBOX)
    data.buildings.append(
        Building(osm_id=1, kind=FeatureKind.BUILDING, tags=tags, footprint=box(0, 0, lado, lado))
    )
    return data


def test_igreja_ganha_campanario():
    """A torre e o marco visual: sem ela a igreja vira galpao."""
    igreja = _scene(_predio({"amenity": "place_of_worship"}), roads=False, terrain=False,
                    vegetation=False, water=False)
    comum = _scene(_predio({"building": "yes"}), roads=False, terrain=False,
                   vegetation=False, water=False)
    assert igreja.bounds()[1][2] > comum.bounds()[1][2] + 3.0


def test_quartel_usa_o_material_do_arquetipo():
    scene = _scene(_predio({"amenity": "fire_station"}), roads=False, terrain=False,
                   vegetation=False, water=False)
    assert any(n.startswith("wall_bombeiros") for n in scene.groups)


def test_predio_sem_arquetipo_usa_a_paleta_do_estilo():
    scene = _scene(_predio({"building": "yes"}), roads=False, terrain=False,
                   vegetation=False, water=False)
    assert not any("bombeiros" in n or "hospital" in n for n in scene.groups)


# ------------------------------------------------------------- estruturas


def _estrutura(kind: StructureKind, height=None) -> MapData:
    data = MapData(bbox=BBOX)
    data.structures.append(
        Structure(
            osm_id=1,
            kind=FeatureKind.BUILDING,
            tags={},
            position=Point(0, 0),
            structure=kind,
            height=height,
        )
    )
    return data


@pytest.mark.parametrize("kind", list(StructureKind))
def test_toda_estrutura_gera_geometria(kind):
    scene = _scene(_estrutura(kind), roads=False, terrain=False, vegetation=False,
                   water=False, buildings=False)
    assert scene.triangle_count > 8, kind


@pytest.mark.parametrize("kind", list(StructureKind))
def test_altura_da_estrutura_fica_na_ordem_esperada(kind):
    scene = _scene(_estrutura(kind), roads=False, terrain=False, vegetation=False,
                   water=False, buildings=False)
    topo = scene.bounds()[1][2]
    padrao = DEFAULT_HEIGHT[kind]
    assert 0.5 * padrao < topo < 2.0 * padrao, f"{kind}: topo {topo:.1f} m"


def test_altura_das_tags_e_respeitada_na_torre():
    scene = _scene(_estrutura(StructureKind.MAST, height=70.0), roads=False, terrain=False,
                   vegetation=False, water=False, buildings=False)
    assert scene.bounds()[1][2] == pytest.approx(70.0, rel=0.2)


def test_estruturas_desligadas_nao_geram_nada():
    scene = _scene(_estrutura(StructureKind.MAST), structures=False, roads=False,
                   terrain=False, vegetation=False, water=False, buildings=False)
    assert scene.triangle_count == 0


def test_torre_de_telefonia_e_mais_alta_que_poste():
    torre = _scene(_estrutura(StructureKind.MAST), roads=False, terrain=False,
                   vegetation=False, water=False, buildings=False)
    poste = _scene(_estrutura(StructureKind.POLE), roads=False, terrain=False,
                   vegetation=False, water=False, buildings=False)
    assert torre.bounds()[1][2] > poste.bounds()[1][2] * 3


# --------------------------------------------------------- postes de rua


def _com_avenida() -> MapData:
    data = MapData(bbox=BBOX)
    data.roads.append(
        Road(
            osm_id=1,
            kind=FeatureKind.ROAD,
            tags={},
            centerline=LineString([(-400, 0), (400, 0)]),
            road_class=RoadClass.PRIMARY,
        )
    )
    return data


def test_avenida_recebe_iluminacao():
    scene = _scene(_com_avenida(), buildings=False, terrain=False, vegetation=False, water=False)
    assert scene.metadata["generated"]["lamps"] > 5


def test_iluminacao_pode_ser_desligada():
    scene = _scene(_com_avenida(), street_lamps=False, buildings=False, terrain=False,
                   vegetation=False, water=False)
    assert scene.metadata["generated"]["lamps"] == 0


def test_viela_nao_recebe_iluminacao():
    data = MapData(bbox=BBOX)
    data.roads.append(
        Road(osm_id=1, kind=FeatureKind.ROAD, tags={},
             centerline=LineString([(-200, 0), (200, 0)]), road_class=RoadClass.FOOTWAY)
    )
    scene = _scene(data, buildings=False, terrain=False, vegetation=False, water=False)
    assert scene.metadata["generated"]["lamps"] == 0


def test_detalhe_baixo_nao_gera_mobiliario():
    settings = GenerationSettings(seed=3, detail="low", buildings=False, terrain=False,
                                  vegetation=False, water=False)
    scene = build_scene(_com_avenida(), GenerationContext(BBOX, settings))
    assert scene.metadata["generated"]["lamps"] == 0


# ------------------------------------------------------------------ rural


def test_lavoura_vira_superficie_propria():
    data = MapData(bbox=BBOX)
    data.farmlands.append(
        Farmland(osm_id=1, kind=FeatureKind.PARK, tags={"landuse": "farmland"},
                 geometry=box(-200, -200, 200, 200), crop="farmland")
    )
    scene = _scene(data, buildings=False, roads=False, vegetation=False, water=False)
    assert "farmland" in scene.groups


def test_parser_le_torre_e_lavoura():
    fixture = {
        "elements": [
            {
                "type": "node",
                "id": 1,
                "lat": -20.3857,
                "lon": -43.5036,
                "tags": {"man_made": "mast", "tower:type": "communication", "height": "45"},
            },
            {"type": "node", "id": 2, "lat": -20.3858, "lon": -43.5037, "tags": {"power": "tower"}},
            {
                "type": "way",
                "id": 3,
                "tags": {"landuse": "farmland"},
                "geometry": [
                    {"lat": -20.3870, "lon": -43.5050},
                    {"lat": -20.3870, "lon": -43.5040},
                    {"lat": -20.3860, "lon": -43.5040},
                    {"lat": -20.3860, "lon": -43.5050},
                    {"lat": -20.3870, "lon": -43.5050},
                ],
            },
        ]
    }
    data = parse_osm(fixture, BBOX, LocalProjection.for_bbox(BBOX))
    kinds = {s.structure for s in data.structures}
    assert StructureKind.MAST in kinds
    assert StructureKind.PYLON in kinds
    assert len(data.farmlands) == 1
    mast = next(s for s in data.structures if s.structure is StructureKind.MAST)
    assert mast.height == pytest.approx(45.0)


def test_torre_mapeada_como_area_guarda_o_contorno():
    fixture = {
        "elements": [
            {
                "type": "way",
                "id": 5,
                "tags": {"man_made": "water_tower"},
                "geometry": [
                    {"lat": -20.3858, "lon": -43.5038},
                    {"lat": -20.3858, "lon": -43.5036},
                    {"lat": -20.3856, "lon": -43.5036},
                    {"lat": -20.3856, "lon": -43.5038},
                    {"lat": -20.3858, "lon": -43.5038},
                ],
            }
        ]
    }
    data = parse_osm(fixture, BBOX, LocalProjection.for_bbox(BBOX))
    assert len(data.structures) == 1
    estrutura = data.structures[0]
    assert estrutura.structure is StructureKind.WATER_TOWER
    assert estrutura.footprint is not None
    # E nao pode ter virado edificio.
    assert len(data.buildings) == 0
