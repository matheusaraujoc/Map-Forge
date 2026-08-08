"""Testes da selecao por contorno desenhado a mao. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Point, Polygon, box

from mapforge.core.features import (
    Building,
    FeatureKind,
    MapData,
    Park,
    Road,
    RoadClass,
    Tree,
)
from mapforge.core.geo import BBox, LocalProjection
from mapforge.core.mesh import Material, MeshBuilder
from mapforge.data.parser import parse_osm
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.pipeline import polygon_to_local

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)
PROJ = LocalProjection.for_bbox(BBOX)


def _triangulo_local() -> Polygon:
    """Contorno em L, bem diferente de um retangulo."""
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    return Polygon(
        [
            (-half_w, -half_h),
            (half_w, -half_h),
            (half_w, 0),
            (0, 0),
            (0, half_h),
            (-half_w, half_h),
        ]
    )


# --------------------------------------------------------------- conversao


def test_contorno_em_graus_vira_poligono_em_metros():
    lat, lon = BBOX.center
    d = 0.002
    contorno = [(lat - d, lon - d), (lat - d, lon + d), (lat + d, lon)]
    poly = polygon_to_local(contorno, PROJ)
    assert poly is not None
    assert poly.area > 1000  # metros quadrados, nao graus


def test_contorno_com_menos_de_tres_pontos_e_recusado():
    lat, lon = BBOX.center
    assert polygon_to_local([(lat, lon), (lat + 0.001, lon)], PROJ) is None
    assert polygon_to_local([], PROJ) is None
    assert polygon_to_local(None, PROJ) is None


def test_contorno_que_se_cruza_e_consertado():
    lat, lon = BBOX.center
    d = 0.002
    laco = [(lat - d, lon - d), (lat + d, lon + d), (lat - d, lon + d), (lat + d, lon - d)]
    poly = polygon_to_local(laco, PROJ)
    assert poly is not None and poly.is_valid


# ------------------------------------------------------------------ parser


FIXTURE = {
    "elements": [
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "residential"},
            # Atravessa a bbox de lado a lado, entrando e saindo do contorno.
            "geometry": [
                {"lat": -20.3890, "lon": -43.5070},
                {"lat": -20.3820, "lon": -43.5000},
            ],
        },
        {
            "type": "node",
            "id": 2,
            "lat": -20.3820,  # canto que o contorno em L exclui
            "lon": -43.5000,
            "tags": {"natural": "tree"},
        },
        {
            "type": "node",
            "id": 3,
            "lat": -20.3890,  # canto que o contorno mantem
            "lon": -43.5070,
            "tags": {"natural": "tree"},
        },
    ]
}


def test_parser_recorta_pelo_contorno():
    clip = _triangulo_local()
    data = parse_osm(FIXTURE, BBOX, PROJ, clip_area=clip)
    for road in data.roads:
        assert clip.buffer(1e-6).contains(road.centerline)


def test_parser_descarta_o_que_cai_fora_do_contorno():
    clip = _triangulo_local()
    completo = parse_osm(FIXTURE, BBOX, PROJ)
    recortado = parse_osm(FIXTURE, BBOX, PROJ, clip_area=clip)
    assert len(recortado.trees) < len(completo.trees)


def test_sem_contorno_o_recorte_e_a_bbox():
    data = parse_osm(FIXTURE, BBOX, PROJ)
    assert len(data.trees) == 2


# ------------------------------------------------------------------ malha


def test_superficie_e_recortada_pelo_contorno():
    clip = _triangulo_local()
    builder = MeshBuilder(clip=clip)
    # Retangulo maior que o contorno.
    builder.add_flat(Material("m", (1, 1, 1)), box(-500, -500, 500, 500), 0.0)
    verts = builder.build()["m"].vertices
    dentro = shapely.contains_xy(clip.buffer(1e-6), verts[:, 0], verts[:, 1])
    assert dentro.all()


def test_area_da_superficie_recortada_bate_com_o_contorno():
    clip = _triangulo_local()
    builder = MeshBuilder(clip=clip)
    builder.add_flat(Material("m", (1, 1, 1)), box(-500, -500, 500, 500), 0.0)
    g = builder.build()["m"]
    tri = g.vertices[g.faces]
    # Area dos triangulos projetados no plano.
    area = 0.5 * np.abs(
        (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
        - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])
    ).sum()
    assert area == pytest.approx(clip.area, rel=1e-6)


def test_geometria_fora_do_contorno_nao_gera_nada():
    clip = _triangulo_local()
    builder = MeshBuilder(clip=clip)
    # No canto que o L recorta.
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    builder.add_flat(Material("m", (1, 1, 1)), box(half_w * 0.5, half_h * 0.5, half_w, half_h), 0.0)
    assert builder.is_empty()


def test_sem_contorno_nada_e_recortado():
    builder = MeshBuilder(clip=None)
    quadrado = box(0, 0, 100, 100)
    builder.add_flat(Material("m", (1, 1, 1)), quadrado, 0.0)
    g = builder.build()["m"]
    assert g.vertices[:, 0].max() == pytest.approx(100.0)


def test_prisma_tambem_respeita_o_contorno():
    clip = _triangulo_local()
    builder = MeshBuilder(clip=clip)
    builder.add_prism(Material("m", (1, 1, 1)), box(-500, -500, 500, 500), 0.0, 3.0)
    verts = builder.build()["m"].vertices
    assert shapely.contains_xy(clip.buffer(1e-6), verts[:, 0], verts[:, 1]).all()


# ------------------------------------------------------------------- cena


def _map_data() -> MapData:
    data = MapData(bbox=BBOX)
    data.roads.append(
        Road(osm_id=1, kind=FeatureKind.ROAD, tags={},
             centerline=LineString([(-400, -300), (400, 300)]),
             road_class=RoadClass.SECONDARY)
    )
    data.buildings.append(
        Building(osm_id=2, kind=FeatureKind.BUILDING, tags={}, footprint=box(-60, -60, -20, -20))
    )
    data.parks.append(
        Park(osm_id=3, kind=FeatureKind.PARK, tags={"leisure": "park"},
             geometry=box(-300, -300, -100, -100))
    )
    return data


def test_cena_recortada_cabe_no_contorno():
    """A calcada e mais larga que o eixo: sem recorte na malha ela vaza."""
    clip = _triangulo_local()
    settings = GenerationSettings(seed=1, detail="high", vegetation=False)
    ctx = GenerationContext(BBOX, settings, clip=clip)
    scene = build_scene(_map_data(), ctx)

    folga = clip.buffer(0.01)
    for nome, g in scene.groups.items():
        if nome == "bank":  # a saia lateral fica sobre o contorno
            continue
        v = g.vertices
        dentro = shapely.contains_xy(folga, v[:, 0], v[:, 1])
        assert dentro.all(), f"{nome} vazou para fora do contorno"


def test_cena_recortada_tem_menos_geometria_que_a_bbox_inteira():
    settings = GenerationSettings(seed=1, detail="high", vegetation=False)
    inteira = build_scene(_map_data(), GenerationContext(BBOX, settings))
    recortada = build_scene(
        _map_data(), GenerationContext(BBOX, settings, clip=_triangulo_local())
    )
    assert recortada.triangle_count < inteira.triangle_count


def test_terreno_recortado_ganha_saia_no_contorno():
    settings = GenerationSettings(seed=1, detail="medium", vegetation=False, roads=False)
    scene = build_scene(
        _map_data(), GenerationContext(BBOX, settings, clip=_triangulo_local())
    )
    assert "bank" in scene.groups  # parede lateral fechando o volume


def test_contorno_nao_muda_o_resultado_quando_cobre_tudo():
    """Um contorno igual a bbox tem de dar a mesma cena que nao ter contorno."""
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    settings = GenerationSettings(seed=1, detail="medium", vegetation=False)
    sem = build_scene(_map_data(), GenerationContext(BBOX, settings))
    com = build_scene(
        _map_data(),
        GenerationContext(BBOX, settings, clip=box(-half_w, -half_h, half_w, half_h)),
    )
    # A saia lateral e o unico acrescimo esperado.
    assert com.triangle_count >= sem.triangle_count
    assert abs(com.bounds()[1][2] - sem.bounds()[1][2]) < 0.5
