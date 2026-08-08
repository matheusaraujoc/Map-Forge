"""Testes de suavizacao de eixos, classificacao viaria e escavacao da agua."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString, box

from mapforge.core.features import (
    FeatureKind,
    MapData,
    River,
    Road,
    RoadClass,
    Water,
)
from mapforge.core.geo import BBox
from mapforge.core.mesh import Material, MeshBuilder
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation.curves import (
    chaikin,
    offset_polyline,
    round_corners,
    smooth_river,
    smooth_road,
)
from mapforge.generation.roads import road_role, road_surface_key

BBOX = BBox(-25.4400, -49.2800, -25.4300, -49.2700)


# ------------------------------------------------------------------- curvas


def test_chaikin_preserva_as_pontas():
    coords = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])
    out = chaikin(coords, iterations=3)
    assert out[0] == pytest.approx(coords[0])
    assert out[-1] == pytest.approx(coords[-1])
    assert len(out) > len(coords)


def test_chaikin_nao_ondula_linha_reta():
    coords = np.array([[0.0, 0.0], [5.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
    out = chaikin(coords, iterations=3)
    assert np.allclose(out[:, 1], 0.0)


def test_round_corners_corta_o_canto():
    # Canto de 90 graus: o vertice exato tem de sumir.
    coords = np.array([[0.0, 0.0], [20.0, 0.0], [20.0, 20.0]])
    out = round_corners(coords, radius=5.0, segments=3)
    assert not np.any(np.all(np.isclose(out, [20.0, 0.0]), axis=1))
    assert len(out) > 3


def test_round_corners_ignora_canto_quase_reto():
    coords = np.array([[0.0, 0.0], [10.0, 0.05], [20.0, 0.0]])
    out = round_corners(coords, radius=5.0, segments=3)
    assert len(out) == 3


def test_round_corners_limita_o_raio_ao_segmento():
    # Segmentos de 2 m nao podem receber um raio de 10 m.
    coords = np.array([[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]])
    out = round_corners(coords, radius=10.0, segments=3)
    assert np.all(np.abs(out) <= 2.001)


def test_offset_polyline_mantem_a_distancia():
    coords = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
    shifted = offset_polyline(coords, 3.0)
    assert np.allclose(np.abs(shifted[:, 1]), 3.0)
    assert len(shifted) == len(coords)


def test_offset_polyline_troca_de_lado_com_o_sinal():
    coords = np.array([[0.0, 0.0], [10.0, 0.0]])
    a = offset_polyline(coords, 2.0)
    b = offset_polyline(coords, -2.0)
    assert np.sign(a[0, 1]) == -np.sign(b[0, 1])


def test_smooth_road_desligado_no_detalhe_baixo():
    line = LineString([(0, 0), (20, 0), (20, 20)])
    assert smooth_road(line, 4.0, "low") is line


def test_smooth_road_adiciona_vertices_no_detalhe_alto():
    line = LineString([(0, 0), (40, 0), (40, 40)])
    out = smooth_road(line, 4.0, "high")
    assert len(out.coords) > len(line.coords)


def test_smooth_river_encurta_o_percurso():
    # Ziguezague: suavizar tem de reduzir o comprimento total.
    zig = LineString([(0, 0), (10, 8), (20, 0), (30, 8), (40, 0)])
    assert smooth_river(zig, "high").length < zig.length


def test_suavizacao_nao_move_as_extremidades():
    line = LineString([(0, 0), (15, 6), (30, 0)])
    for smoothed in (smooth_road(line, 3.0, "high"), smooth_river(line, "high")):
        assert smoothed.coords[0] == pytest.approx(line.coords[0])
        assert smoothed.coords[-1] == pytest.approx(line.coords[-1])


# --------------------------------------------------------- classificacao viaria


def test_calcada_mapeada_vira_camada_de_calcada():
    road = Road(osm_id=1, kind=FeatureKind.ROAD, tags={"footway": "sidewalk"},
                centerline=LineString([(0, 0), (30, 0)]), road_class=RoadClass.FOOTWAY)
    assert road_role(road) == "sidewalk"


def test_faixa_de_pedestre_e_identificada():
    road = Road(osm_id=1, kind=FeatureKind.ROAD, tags={"footway": "crossing"},
                centerline=LineString([(0, 0), (8, 0)]), road_class=RoadClass.FOOTWAY)
    assert road_role(road) == "crossing"


def test_trilha_comum_continua_sendo_superficie():
    road = Road(osm_id=1, kind=FeatureKind.ROAD, tags={},
                centerline=LineString([(0, 0), (30, 0)]), road_class=RoadClass.FOOTWAY)
    assert road_role(road) == "surface"


def test_rua_nao_pedonal_nunca_vira_calcada():
    road = Road(osm_id=1, kind=FeatureKind.ROAD, tags={"footway": "sidewalk"},
                centerline=LineString([(0, 0), (30, 0)]), road_class=RoadClass.RESIDENTIAL)
    assert road_role(road) == "surface"


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("sett", "cobble"),
        ("cobblestone", "cobble"),
        ("paving_stones", "cobble"),
        ("gravel", "dirt"),
        ("ground", "dirt"),
        ("asphalt", "road"),
        ("", "road"),
    ],
)
def test_tag_surface_escolhe_o_material(surface, expected):
    road = Road(osm_id=1, kind=FeatureKind.ROAD, tags={"surface": surface} if surface else {},
                centerline=LineString([(0, 0), (30, 0)]), road_class=RoadClass.RESIDENTIAL)
    assert road_surface_key(road) == expected


# ------------------------------------------------------------------- agua


def _water_map() -> MapData:
    data = MapData(bbox=BBOX)
    data.waters.append(
        Water(osm_id=1, kind=FeatureKind.WATER, tags={}, geometry=box(-120, -60, 120, 60))
    )
    data.rivers.append(
        River(osm_id=2, kind=FeatureKind.RIVER, tags={"waterway": "river"},
              centerline=LineString([(-300, 0), (-120, 20)]), width=16.0)
    )
    return data


def test_canal_e_escavado_com_talude():
    settings = GenerationSettings(terrain=True, roads=False, buildings=False, vegetation=False)
    scene = build_scene(_water_map(), GenerationContext(BBOX, settings))

    assert "water" in scene.groups
    assert "water_bed" in scene.groups
    assert "bank" in scene.groups

    bed_z = scene.groups["water_bed"].vertices[:, 2]
    surface_z = scene.groups["water"].vertices[:, 2]
    bank_z = scene.groups["bank"].vertices[:, 2]

    assert bed_z.max() < surface_z.min()  # leito abaixo da lamina
    assert bank_z.min() == pytest.approx(bed_z.min(), abs=0.01)
    assert bank_z.max() == pytest.approx(0.0, abs=0.01)  # encosta no solo
    assert bank_z.min() < bank_z.max()  # e de fato inclinado


def test_agua_desligada_nao_gera_canal():
    settings = GenerationSettings(water=False, roads=False, buildings=False, vegetation=False)
    scene = build_scene(_water_map(), GenerationContext(BBOX, settings))
    assert "water" not in scene.groups


def test_ponte_aparece_onde_a_via_cruza_a_agua():
    data = _water_map()
    data.roads.append(
        Road(osm_id=3, kind=FeatureKind.ROAD, tags={},
             centerline=LineString([(0, -200), (0, 200)]), road_class=RoadClass.SECONDARY)
    )
    settings = GenerationSettings(buildings=False, vegetation=False)
    scene = build_scene(data, GenerationContext(BBOX, settings))

    # A laje fica abaixo do nivel da pista, fechando o vao sobre o rio.
    curb_z = scene.groups["curb"].vertices[:, 2]
    assert curb_z.min() < -0.3


def test_talude_nao_quebra_em_corrego_estreito():
    data = MapData(bbox=BBOX)
    data.rivers.append(
        River(osm_id=1, kind=FeatureKind.RIVER, tags={"waterway": "ditch"},
              centerline=LineString([(-200, 0), (200, 0)]), width=1.8)
    )
    settings = GenerationSettings(roads=False, buildings=False, vegetation=False)
    scene = build_scene(data, GenerationContext(BBOX, settings))
    assert "water" in scene.groups


# ------------------------------------------------------------------ texturas


def test_add_flat_gera_uv_normalizado():
    builder = MeshBuilder()
    builder.add_flat(
        Material("chao", (1, 1, 1)), box(-50, -50, 50, 50), 0.0, uv_bounds=(-50, -50, 50, 50)
    )
    group = builder.build()["chao"]
    assert group.uv is not None
    assert group.uv.min() >= -1e-9
    assert group.uv.max() <= 1.0 + 1e-9


def test_uv_tem_o_norte_no_topo():
    builder = MeshBuilder()
    builder.add_flat(
        Material("chao", (1, 1, 1)), box(-50, -50, 50, 50), 0.0, uv_bounds=(-50, -50, 50, 50)
    )
    group = builder.build()["chao"]
    norte = group.vertices[:, 1].argmax()
    sul = group.vertices[:, 1].argmin()
    assert group.uv[norte, 1] < group.uv[sul, 1]


def test_sem_uv_bounds_o_grupo_fica_sem_uv():
    builder = MeshBuilder()
    builder.add_flat(Material("chao", (1, 1, 1)), box(0, 0, 10, 10), 0.0)
    assert builder.build()["chao"].uv is None


def test_textura_sobrevive_ao_glb(tmp_path):
    """A imagem tem de chegar no arquivo, senao o terreno exporta cinza."""
    import trimesh
    from PIL import Image

    from mapforge.core.mesh import Scene
    from mapforge.export import export_scene

    texture = Image.new("RGB", (32, 32), (200, 40, 40))
    builder = MeshBuilder()
    builder.add_flat(
        Material("chao", (1.0, 1.0, 1.0), texture=texture),
        box(-50, -50, 50, 50),
        0.0,
        uv_bounds=(-50, -50, 50, 50),
    )
    path = export_scene(Scene(name="t", groups=builder.build()), tmp_path / "t.glb")

    loaded = trimesh.load(path)
    mesh = next(iter(loaded.geometry.values()))
    assert mesh.visual.uv is not None
    assert mesh.visual.material.baseColorTexture is not None


def test_material_com_textura_continua_hashavel():
    """A imagem fica fora de __eq__/__hash__; sem isso o cache de materiais quebra."""
    from PIL import Image

    material = Material("chao", (1.0, 1.0, 1.0), texture=Image.new("RGB", (4, 4)))
    assert hash(material) == hash(Material("chao", (1.0, 1.0, 1.0)))
