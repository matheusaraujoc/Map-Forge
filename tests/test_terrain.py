"""Testes do modelo de elevacao, do campo de altura e do assentamento no relevo."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from mapforge.core.features import (
    Building,
    FeatureKind,
    MapData,
    River,
    Road,
    RoadClass,
    Tree,
)
from mapforge.core.geo import BBox
from mapforge.core.mesh import Material, MeshBuilder
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation.terrain import TerrainField
from mapforge.imagery.elevation import ElevationGrid, decode_terrarium

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


# ---------------------------------------------------------- decodificacao DEM


def test_decode_terrarium_nivel_do_mar():
    # R=128, G=0, B=0 -> 128*256 - 32768 = 0 m
    rgb = np.array([[[128, 0, 0]]], dtype=np.uint8)
    assert decode_terrarium(rgb)[0, 0] == pytest.approx(0.0)


def test_decode_terrarium_altitude_conhecida():
    # 1000 m -> 33768 = 131*256 + 232
    rgb = np.array([[[131, 232, 0]]], dtype=np.uint8)
    assert decode_terrarium(rgb)[0, 0] == pytest.approx(1000.0)


def test_decode_terrarium_usa_o_canal_azul_como_fracao():
    base = decode_terrarium(np.array([[[131, 232, 0]]], dtype=np.uint8))[0, 0]
    fine = decode_terrarium(np.array([[[131, 232, 128]]], dtype=np.uint8))[0, 0]
    assert fine - base == pytest.approx(0.5)


def test_decode_terrarium_altitude_negativa():
    rgb = np.array([[[127, 156, 0]]], dtype=np.uint8)  # abaixo do nivel do mar
    assert decode_terrarium(rgb)[0, 0] < 0


# ------------------------------------------------------------- ElevationGrid


def _grid(heights: np.ndarray) -> ElevationGrid:
    return ElevationGrid(heights=heights, bbox=BBOX, zoom=15)


def test_grid_amostra_os_cantos():
    heights = np.array([[10.0, 20.0], [30.0, 40.0]])  # linha 0 = norte
    grid = _grid(heights)
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    assert grid.sample(-half_w, half_h)[0] == pytest.approx(10.0)  # noroeste
    assert grid.sample(half_w, half_h)[0] == pytest.approx(20.0)  # nordeste
    assert grid.sample(-half_w, -half_h)[0] == pytest.approx(30.0)  # sudoeste
    assert grid.sample(half_w, -half_h)[0] == pytest.approx(40.0)  # sudeste


def test_grid_interpola_no_centro():
    grid = _grid(np.array([[0.0, 0.0], [100.0, 100.0]]))
    assert grid.sample(0.0, 0.0)[0] == pytest.approx(50.0)


def test_grid_fora_da_borda_gruda_no_limite():
    grid = _grid(np.array([[10.0, 20.0], [30.0, 40.0]]))
    longe = grid.sample(BBOX.width_m * 5, BBOX.height_m * 5)[0]
    assert longe == pytest.approx(20.0)  # canto nordeste, sem extrapolar


def test_suavizacao_reduz_a_variacao():
    rng = np.random.default_rng(0)
    ruido = rng.normal(100.0, 5.0, size=(40, 40))
    grid = _grid(ruido)
    assert grid.smoothed(2).heights.std() < grid.heights.std()


# --------------------------------------------------------------- TerrainField


def _linear_field(a: float, b: float, step: float = 25.0) -> TerrainField:
    """Campo cuja altura verdadeira e a*x + b*y (representavel exatamente)."""
    rows, cols = 40, 40
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    xs = np.linspace(-half_w, half_w, cols)
    ys = np.linspace(half_h, -half_h, rows)  # linha 0 = norte
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    grid = _grid(a * mesh_x + b * mesh_y)
    return TerrainField.from_grid(BBOX, grid, step=step)


def test_campo_reproduz_plano_inclinado_exatamente():
    """Uma funcao linear e representavel na malha: a consulta tem de ser exata.

    E o invariante que garante que rua e calcada nao afundem na encosta.
    """
    a, b = 0.05, -0.03
    field = _linear_field(a, b)
    rng = np.random.default_rng(1)
    x = rng.uniform(-BBOX.width_m / 2, BBOX.width_m / 2, 200)
    y = rng.uniform(-BBOX.height_m / 2, BBOX.height_m / 2, 200)

    esperado = a * x + b * y - field.base
    assert np.allclose(field.height(x, y), esperado, atol=1e-6)


def test_consulta_bate_com_a_malha_desenhada():
    """A altura consultada tem de cair exatamente sobre o triangulo desenhado."""
    field = _linear_field(0.08, 0.02, step=40.0)
    vertices, faces = field.mesh()

    rng = np.random.default_rng(7)
    for _ in range(60):
        face = faces[rng.integers(len(faces))]
        tri = vertices[face]
        # Ponto aleatorio dentro do triangulo, por coordenadas baricentricas.
        w = rng.dirichlet([1.0, 1.0, 1.0])
        point = (tri * w[:, None]).sum(axis=0)
        assert field.height(point[0], point[1])[0] == pytest.approx(point[2], abs=1e-6)


def test_vertices_da_malha_batem_com_a_grade():
    field = _linear_field(0.04, 0.01)
    vertices, _ = field.mesh()
    amostra = field.height(vertices[:, 0], vertices[:, 1])
    assert np.allclose(amostra, vertices[:, 2], atol=1e-6)


def test_base_desloca_o_terreno_para_zero():
    field = _linear_field(0.05, 0.05)
    assert field.z.min() == pytest.approx(0.0, abs=1e-6)
    assert field.base != 0.0


def test_exagero_multiplica_o_desnivel():
    rows, cols = 20, 20
    grid = _grid(np.tile(np.linspace(0.0, 100.0, cols), (rows, 1)))
    normal = TerrainField.from_grid(BBOX, grid, step=30.0, exaggeration=1.0)
    dobro = TerrainField.from_grid(BBOX, grid, step=30.0, exaggeration=2.0)
    assert dobro.relief == pytest.approx(normal.relief * 2.0, rel=1e-6)


def test_carve_rebaixa_apenas_dentro_do_poligono():
    field = _linear_field(0.0, 0.0)  # plano em z=0
    antes = field.z.copy()
    field.carve(box(-100, -100, 100, 100), depth=5.0, level=0.0)
    assert field.z.min() == pytest.approx(-5.0)
    # Fora da area escavada nada mudou.
    assert field.z.max() == pytest.approx(antes.max())


def test_carve_nunca_levanta_o_terreno():
    field = _linear_field(0.0, 0.0)
    field.carve(box(-100, -100, 100, 100), depth=-50.0, level=0.0)
    assert field.z.max() <= 1e-9


def test_min_over_pega_o_ponto_mais_baixo():
    field = _linear_field(0.1, 0.0)  # sobe para leste
    poligono = box(-200, -50, 200, 50)
    assert field.min_over(poligono) == pytest.approx(field.at(-200, -50), abs=0.5)


# ------------------------------------------------------------- drapejamento


def test_add_flat_com_drape_acompanha_o_relevo():
    field = _linear_field(0.1, 0.0)
    builder = MeshBuilder(terrain=field)
    builder.add_flat(Material("m", (1, 1, 1)), box(-100, -100, 100, 100), 0.5, drape=True)
    group = builder.build()["m"]
    # A superficie inclinada nao pode ser toda na mesma altura.
    assert group.vertices[:, 2].max() - group.vertices[:, 2].min() > 10.0


def test_add_flat_sem_drape_fica_plano():
    field = _linear_field(0.1, 0.0)
    builder = MeshBuilder(terrain=field)
    builder.add_flat(Material("m", (1, 1, 1)), box(-100, -100, 100, 100), 0.5, drape=False)
    assert np.allclose(builder.build()["m"].vertices[:, 2], 0.5)


def test_prisma_drapeado_mantem_a_espessura():
    field = _linear_field(0.1, 0.0)
    builder = MeshBuilder(terrain=field)
    builder.add_prism(Material("m", (1, 1, 1)), box(-100, -100, 100, 100), 0.0, 0.16, drape=True)
    verts = builder.build()["m"].vertices

    # Em cada coluna (x, y) a diferenca topo-base tem de continuar 0.16.
    # O recorte pela grade do terreno repete vertices nas bordas das celulas,
    # entao a comparacao e sobre alturas distintas, nao sobre a contagem bruta.
    chaves: dict[tuple, set] = {}
    for x, y, z in verts:
        chaves.setdefault((round(x, 3), round(y, 3)), set()).add(round(float(z), 6))
    espessuras = [max(v) - min(v) for v in chaves.values() if len(v) > 1]
    assert espessuras
    assert all(e == pytest.approx(0.16, abs=1e-5) for e in espessuras)


def _wavy_field(step: float = 8.0) -> TerrainField:
    """Terreno ondulado: e onde o assentamento errado aparece."""
    rows = cols = 120
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    xs = np.linspace(-half_w, half_w, cols)
    ys = np.linspace(half_h, -half_h, rows)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    heights = 12.0 * np.sin(mesh_x / 120.0 * 2 * np.pi) + 6.0 * np.cos(mesh_y / 90.0 * 2 * np.pi)
    return TerrainField.from_grid(BBOX, _grid(heights), step=step)


def test_rua_longa_nao_atravessa_o_morro():
    """Regressao: um corredor com poucos vertices afundava 6,5 m no relevo.

    A causa era o earcut ligar vertices distantes do poligono, produzindo
    triangulos com arestas de centenas de metros que cortam reto por cima do
    terreno. O recorte pelos triangulos do terreno elimina isso.
    """
    field = _wavy_field()
    faixa = LineString([(-400, 0), (400, 30)]).buffer(4.0, quad_segs=2)
    assert len(faixa.exterior.coords) < 20  # poucos vertices, como vem do OSM

    builder = MeshBuilder(terrain=field)
    builder.add_flat(Material("via", (0.4, 0.4, 0.4)), faixa, 0.05, drape=True)
    group = builder.build()["via"]

    centros = group.vertices[group.faces].mean(axis=1)
    esperado = field.height(centros[:, 0], centros[:, 1]) + 0.05 + builder.drape_bias
    desvio = centros[:, 2] - esperado
    assert np.abs(desvio).max() < 0.02, f"a rua desviou {np.abs(desvio).max():.2f} m do terreno"


def test_superficie_assentada_fica_sempre_acima_do_terreno():
    field = _wavy_field()
    faixa = LineString([(-300, -100), (300, 120)]).buffer(6.0, quad_segs=2)

    builder = MeshBuilder(terrain=field)
    builder.add_flat(Material("via", (0.4, 0.4, 0.4)), faixa, 0.05, drape=True)
    group = builder.build()["via"]

    altura_terreno = field.height(group.vertices[:, 0], group.vertices[:, 1])
    assert (group.vertices[:, 2] > altura_terreno).all()


def test_recorte_pela_grade_cobre_a_mesma_area():
    field = _wavy_field()
    builder = MeshBuilder(terrain=field)
    original = LineString([(-200, 0), (200, 50)]).buffer(5.0, quad_segs=2)

    pieces = builder.grid_split(original)
    assert len(pieces) > 20
    assert sum(p.area for p in pieces) == pytest.approx(original.area, rel=1e-6)


def test_recorte_nao_acontece_sem_relevo():
    builder = MeshBuilder(terrain=None)
    geom = box(0, 0, 100, 100)
    assert builder.grid_split(geom) == [geom]


def test_sem_terreno_o_drape_nao_faz_nada():
    builder = MeshBuilder(terrain=None)
    builder.add_flat(Material("m", (1, 1, 1)), box(0, 0, 10, 10), 2.0, drape=True)
    assert np.allclose(builder.build()["m"].vertices[:, 2], 2.0)


# --------------------------------------------------------- cena com relevo


def _map_data() -> MapData:
    data = MapData(bbox=BBOX)
    data.roads.append(
        Road(osm_id=1, kind=FeatureKind.ROAD, tags={},
             centerline=LineString([(-300, 0), (300, 0)]), road_class=RoadClass.RESIDENTIAL)
    )
    data.buildings.append(
        Building(osm_id=2, kind=FeatureKind.BUILDING, tags={},
                 footprint=box(-40, 40, 0, 80), building_type="house")
    )
    data.trees.append(Tree(osm_id=3, kind=FeatureKind.TREE, tags={}, position=Point(120, 30)))
    return data


def _context(field: TerrainField, **kwargs) -> GenerationContext:
    settings = GenerationSettings(seed=1, **kwargs)
    return GenerationContext(BBOX, settings, terrain=field)


def test_cena_com_relevo_gera_terreno_inclinado():
    field = _linear_field(0.15, 0.0)
    scene = build_scene(_map_data(), _context(field, vegetation=False))
    ground = scene.groups["ground"].vertices[:, 2]
    assert ground.max() - ground.min() > 50.0


def test_arvore_planta_na_altura_do_terreno():
    field = _linear_field(0.15, 0.0)
    scene = build_scene(_map_data(), _context(field, buildings=False, roads=False))
    tronco = scene.groups["trunk"].vertices
    esperado = field.at(120, 30)
    assert tronco[:, 2].min() == pytest.approx(esperado, abs=0.5)


def test_predio_assenta_no_terreno():
    field = _linear_field(0.15, 0.0)
    data = _map_data()
    scene = build_scene(data, _context(field, vegetation=False, roads=False))

    footprint = data.buildings[0].footprint
    chao = field.min_over(footprint)
    paredes = np.concatenate(
        [g.vertices[:, 2] for name, g in scene.groups.items() if name.startswith("wall_")]
    )
    # A base fica junto ao ponto mais baixo do contorno, nao em z=0.
    assert paredes.min() == pytest.approx(chao - 0.6, abs=0.05)
    assert chao > 5.0  # confirma que o terreno ali nao e o nivel zero


def test_rua_acompanha_a_encosta():
    field = _linear_field(0.15, 0.0)
    scene = build_scene(_map_data(), _context(field, vegetation=False, buildings=False))
    asfalto = scene.groups["road"].vertices
    # A pista sobe junto com o terreno em vez de ficar numa altura so.
    assert asfalto[:, 2].max() - asfalto[:, 2].min() > 50.0
    esperado = field.height(asfalto[:, 0], asfalto[:, 1])
    assert np.allclose(asfalto[:, 2] - esperado, asfalto[0, 2] - esperado[0], atol=1e-6)


def test_agua_escava_o_terreno():
    field = _linear_field(0.0, 0.0)
    data = MapData(bbox=BBOX)
    data.rivers.append(
        River(osm_id=1, kind=FeatureKind.RIVER, tags={"waterway": "river"},
              centerline=LineString([(-300, 0), (300, 0)]), width=40.0)
    )
    scene = build_scene(data, _context(field, buildings=False, roads=False, vegetation=False))

    assert field.z.min() < -0.5  # a grade foi rebaixada
    agua = scene.groups["water"].vertices[:, 2]
    assert agua.min() > field.z.min()  # a lamina fica acima do leito
