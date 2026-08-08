"""Testes de parser, geracao e exportacao. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import LineString, Point, box

from mapforge.core.features import Building, FeatureKind, Forest, MapData, Park, Road, RoadClass, Tree, Water
from mapforge.core.geo import BBox, LocalProjection
from mapforge.data.cache import Cache
from mapforge.data.parser import parse_osm
from mapforge.export import export_scene
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation.roads import road_half_width

BBOX = BBox(-25.4400, -49.2800, -25.4300, -49.2700)


# ------------------------------------------------------------------------ parser


def _node(lat, lon):
    return {"lat": lat, "lon": lon}


OVERPASS_FIXTURE = {
    "elements": [
        {
            "type": "way",
            "id": 1,
            "tags": {"building": "house", "building:levels": "2"},
            "geometry": [
                _node(-25.3500, -49.2750),
                _node(-25.3500, -49.2748),
                _node(-25.3502, -49.2748),
                _node(-25.3502, -49.2750),
                _node(-25.3500, -49.2750),
            ],
        },
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "residential", "lanes": "2"},
            "geometry": [_node(-25.3510, -49.2760), _node(-25.3490, -49.2740)],
        },
        {
            "type": "way",
            "id": 3,
            "tags": {"natural": "water"},
            "geometry": [
                _node(-25.3520, -49.2770),
                _node(-25.3520, -49.2765),
                _node(-25.3525, -49.2765),
                _node(-25.3525, -49.2770),
                _node(-25.3520, -49.2770),
            ],
        },
        {
            "type": "way",
            "id": 4,
            "tags": {"landuse": "forest"},
            "geometry": [
                _node(-25.3480, -49.2730),
                _node(-25.3480, -49.2720),
                _node(-25.3490, -49.2720),
                _node(-25.3490, -49.2730),
                _node(-25.3480, -49.2730),
            ],
        },
        {"type": "node", "id": 5, "lat": -25.3505, "lon": -49.2755, "tags": {"natural": "tree"}},
        {
            "type": "relation",
            "id": 6,
            "tags": {"leisure": "park"},
            "members": [
                {
                    "type": "way",
                    "role": "outer",
                    "geometry": [
                        _node(-25.3460, -49.2790),
                        _node(-25.3460, -49.2780),
                        _node(-25.3470, -49.2780),
                        _node(-25.3470, -49.2790),
                        _node(-25.3460, -49.2790),
                    ],
                }
            ],
        },
        # Sem tags: deve ser ignorado sem quebrar.
        {"type": "way", "id": 7, "geometry": [_node(-25.35, -49.27), _node(-25.35, -49.27)]},
    ]
}

FIXTURE_BBOX = BBox(-25.3540, -49.2800, -25.3450, -49.2700)


def test_parser_classifica_cada_tipo():
    data = parse_osm(OVERPASS_FIXTURE, FIXTURE_BBOX)
    assert len(data.buildings) == 1
    assert len(data.roads) == 1
    assert len(data.waters) == 1
    assert len(data.forests) == 1
    assert len(data.trees) == 1
    assert len(data.parks) == 1


def test_parser_le_pavimentos_e_calcula_altura():
    data = parse_osm(OVERPASS_FIXTURE, FIXTURE_BBOX)
    building = data.buildings[0]
    assert building.levels == 2
    assert building.height == pytest.approx(2 * 3.1 + 1.0)
    assert building.building_type == "house"


def test_parser_projeta_para_metros():
    data = parse_osm(OVERPASS_FIXTURE, FIXTURE_BBOX)
    # A casa tem ~20 x 22 m; a area precisa estar na ordem de centenas de m2.
    assert 100 < data.buildings[0].footprint.area < 1500


def test_parser_recorta_na_bbox():
    pequeno = BBox(-25.3510, -49.2760, -25.3495, -49.2745)
    data = parse_osm(OVERPASS_FIXTURE, pequeno)
    half_w, half_h = pequeno.width_m / 2, pequeno.height_m / 2
    for road in data.roads:
        minx, miny, maxx, maxy = road.centerline.bounds
        assert minx >= -half_w - 1e-6 and maxx <= half_w + 1e-6
        assert miny >= -half_h - 1e-6 and maxy <= half_h + 1e-6


def test_parser_ignora_elemento_sem_geometria():
    data = parse_osm({"elements": [{"type": "way", "id": 9, "tags": {"building": "yes"}}]}, BBOX)
    assert data.is_empty()


def test_parser_aceita_resposta_vazia():
    assert parse_osm({"elements": []}, BBOX).is_empty()


# -------------------------------------------------------------------- geracao


def _map_data() -> MapData:
    data = MapData(bbox=BBOX)
    for i in range(-2, 3):
        data.roads.append(
            Road(
                osm_id=100 + i,
                kind=FeatureKind.ROAD,
                tags={},
                centerline=LineString([(i * 50, -200), (i * 50, 200)]),
                road_class=RoadClass.RESIDENTIAL,
            )
        )
    for i in range(12):
        data.buildings.append(
            Building(
                osm_id=200 + i,
                kind=FeatureKind.BUILDING,
                tags={},
                footprint=box(i * 20, 0, i * 20 + 14, 18),
                building_type="house" if i % 2 else "apartments",
            )
        )
    data.parks.append(
        Park(osm_id=300, kind=FeatureKind.PARK, tags={"leisure": "park"}, geometry=box(-150, -150, -50, -50))
    )
    data.forests.append(
        Forest(osm_id=301, kind=FeatureKind.FOREST, tags={"landuse": "forest"}, geometry=box(60, -180, 180, -60))
    )
    data.waters.append(Water(osm_id=302, kind=FeatureKind.WATER, tags={}, geometry=box(-200, 60, -120, 160)))
    data.trees.append(Tree(osm_id=303, kind=FeatureKind.TREE, tags={}, position=Point(10, 40)))
    return data


def test_build_scene_produz_geometria():
    scene = build_scene(_map_data(), GenerationContext(BBOX, GenerationSettings(seed=3)))
    assert scene.triangle_count > 1000
    assert len(scene.groups) > 5


def test_mesma_seed_gera_mesma_malha():
    data = _map_data()
    a = build_scene(data, GenerationContext(BBOX, GenerationSettings(seed=42)))
    b = build_scene(data, GenerationContext(BBOX, GenerationSettings(seed=42)))
    for name, group in a.groups.items():
        assert np.allclose(group.vertices, b.groups[name].vertices)


def test_seed_diferente_gera_malha_diferente():
    data = _map_data()
    a = build_scene(data, GenerationContext(BBOX, GenerationSettings(seed=1)))
    b = build_scene(data, GenerationContext(BBOX, GenerationSettings(seed=2)))
    assert a.triangle_count != b.triangle_count or not np.allclose(
        a.bounds(), b.bounds()
    )


@pytest.mark.parametrize("detail", ["low", "medium", "high"])
@pytest.mark.parametrize("style", ["lowpoly", "cartoon", "futurista"])
def test_todas_as_combinacoes_de_estilo_e_detalhe(style, detail):
    settings = GenerationSettings(seed=5, style=style, detail=detail)
    scene = build_scene(_map_data(), GenerationContext(BBOX, settings))
    assert scene.triangle_count > 0


def test_detalhe_baixo_gera_menos_triangulos():
    data = _map_data()
    low = build_scene(data, GenerationContext(BBOX, GenerationSettings(detail="low")))
    high = build_scene(data, GenerationContext(BBOX, GenerationSettings(detail="high")))
    assert low.triangle_count < high.triangle_count


def test_desligar_camadas_reduz_a_cena():
    data = _map_data()
    completo = build_scene(data, GenerationContext(BBOX, GenerationSettings()))
    so_terreno = build_scene(
        data,
        GenerationContext(
            BBOX,
            GenerationSettings(roads=False, buildings=False, vegetation=False, water=False),
        ),
    )
    assert so_terreno.triangle_count < completo.triangle_count
    assert so_terreno.triangle_count > 0


def test_max_trees_limita_vegetacao():
    settings = GenerationSettings(seed=9, max_trees=10, detail="high")
    scene = build_scene(_map_data(), GenerationContext(BBOX, settings))
    assert scene.metadata["generated"]["trees"] <= 10


def test_altura_das_tags_e_respeitada():
    data = MapData(bbox=BBOX)
    data.buildings.append(
        Building(
            osm_id=1,
            kind=FeatureKind.BUILDING,
            tags={},
            footprint=box(0, 0, 20, 20),
            height=30.0,
        )
    )
    settings = GenerationSettings(terrain=False, vegetation=False, roads=False, water=False)
    scene = build_scene(data, GenerationContext(BBOX, settings))
    assert scene.bounds()[1][2] == pytest.approx(30.0, rel=0.25)


def test_height_scale_aumenta_os_predios():
    data = _map_data()
    normal = build_scene(data, GenerationContext(BBOX, GenerationSettings(seed=4)))
    alto = build_scene(
        data, GenerationContext(BBOX, GenerationSettings(seed=4, building_height_scale=3.0))
    )
    assert alto.bounds()[1][2] > normal.bounds()[1][2] * 2


def test_metadata_guarda_parametros_para_regerar():
    settings = GenerationSettings(seed=77, style="cartoon", detail="low")
    scene = build_scene(_map_data(), GenerationContext(BBOX, settings))
    assert scene.metadata["seed"] == 77
    assert scene.metadata["settings"]["style"] == "cartoon"
    assert GenerationSettings.from_dict(scene.metadata["settings"]).seed == 77


# ---------------------------------------------------------------------- ruas


def test_largura_da_via_usa_a_tag_width():
    road = Road(
        osm_id=1,
        kind=FeatureKind.ROAD,
        tags={},
        centerline=LineString([(0, 0), (10, 0)]),
        road_class=RoadClass.RESIDENTIAL,
        width=9.0,
    )
    assert road_half_width(road) == pytest.approx(4.5)


def test_via_maior_e_mais_larga_que_residencial():
    def make(cls):
        return Road(
            osm_id=1,
            kind=FeatureKind.ROAD,
            tags={},
            centerline=LineString([(0, 0), (10, 0)]),
            road_class=cls,
        )

    assert road_half_width(make(RoadClass.MOTORWAY)) > road_half_width(make(RoadClass.RESIDENTIAL))
    assert road_half_width(make(RoadClass.RESIDENTIAL)) > road_half_width(make(RoadClass.FOOTWAY))


# ----------------------------------------------------------------- exportacao


def test_export_glb_reimporta_com_mesma_contagem(tmp_path):
    scene = build_scene(_map_data(), GenerationContext(BBOX, GenerationSettings(seed=11)))
    path = export_scene(scene, tmp_path / "cidade.glb")
    assert path.exists() and path.stat().st_size > 1000

    loaded = trimesh.load(path)
    total = sum(len(g.faces) for g in loaded.geometry.values())
    assert total == scene.triangle_count


def test_export_converte_para_y_up(tmp_path):
    scene = build_scene(_map_data(), GenerationContext(BBOX, GenerationSettings(seed=11)))
    path = export_scene(scene, tmp_path / "cidade.glb")
    loaded = trimesh.load(path)
    altura_zup = scene.bounds()[1][2]
    # No glTF a altura vira o eixo Y.
    assert loaded.bounds[1][1] == pytest.approx(altura_zup, rel=0.02)


def test_export_grava_metadados_ao_lado(tmp_path):
    scene = build_scene(_map_data(), GenerationContext(BBOX, GenerationSettings(seed=11)))
    path = export_scene(scene, tmp_path / "cidade.glb")
    assert path.with_suffix(".glb.json").exists()


def test_export_recusa_formato_desconhecido(tmp_path):
    scene = build_scene(_map_data(), GenerationContext(BBOX, GenerationSettings()))
    with pytest.raises(ValueError, match="formato"):
        export_scene(scene, tmp_path / "cidade.xyz")


# ----------------------------------------------------------------------- cache


def test_cache_guarda_e_recupera(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        payload = {"elements": [{"type": "node", "id": 1}]}
        cache.put_osm("k", "bbox", payload)
        assert cache.get_osm("k") == payload
        assert cache.get_osm("outra") is None


def test_cache_expira_com_ttl_zero(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        cache.put_osm("k", "bbox", {"elements": []})
        assert cache.get_osm("k", ttl=1e-9) is None


def test_projeto_guarda_apenas_parametros(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        settings = GenerationSettings(seed=8, style="cartoon").to_dict()
        cache.save_project("teste", BBOX.key(), 8, "cartoon", settings)
        loaded = cache.load_project("teste")
        assert loaded["seed"] == 8
        assert loaded["settings"]["style"] == "cartoon"
        assert cache.load_project("nao_existe") is None


# -------------------------------------------------------------------- render


def test_render_offscreen_gera_imagem():
    from mapforge.render import Camera, render_scene

    scene = build_scene(_map_data(), GenerationContext(BBOX, GenerationSettings(detail="low")))
    image = render_scene(scene, None, width=160, height=120, supersample=1, camera=Camera())
    assert image.shape == (120, 160, 3)
    # A cena precisa cobrir boa parte do quadro (nao pode sair so o fundo).
    background = image[0, 0]
    assert (np.abs(image.astype(int) - background.astype(int)).sum(axis=2) > 12).mean() > 0.2
