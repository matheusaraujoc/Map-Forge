"""Testes do nucleo: geografia, malha e estilos. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from mapforge.core.geo import BBox, LocalProjection
from mapforge.core.mesh import Material, MeshBuilder, Scene
from mapforge.styles import STYLES, get_style


# ------------------------------------------------------------------- geografia


def test_bbox_from_string():
    bbox = BBox.from_string("-25.44, -49.28, -25.43, -49.27")
    assert bbox.south == pytest.approx(-25.44)
    assert bbox.east == pytest.approx(-49.27)


def test_bbox_rejeita_ordem_invertida():
    with pytest.raises(ValueError):
        BBox(-25.43, -49.28, -25.44, -49.27)


def test_bbox_from_center_tem_lado_esperado():
    bbox = BBox.from_center(-25.43, -49.27, 500.0)
    assert bbox.width_m == pytest.approx(1000.0, rel=0.01)
    assert bbox.height_m == pytest.approx(1000.0, rel=0.01)


def test_projecao_ida_e_volta():
    projection = LocalProjection(-25.43, -49.27)
    lat, lon = projection.unproject(*projection.project(-25.4321, -49.2712))
    assert lat == pytest.approx(-25.4321, abs=1e-9)
    assert lon == pytest.approx(-49.2712, abs=1e-9)


def test_projecao_centro_na_origem():
    projection = LocalProjection(-25.43, -49.27)
    assert projection.project(-25.43, -49.27) == pytest.approx((0.0, 0.0))


def test_project_many_bate_com_project():
    projection = LocalProjection(-20.0, -43.0)
    coords = [(-20.001, -43.001), (-19.999, -42.998)]
    batch = projection.project_many(coords)
    for row, (lat, lon) in zip(batch, coords):
        assert row == pytest.approx(projection.project(lat, lon))


# ------------------------------------------------------------------------ malha


def _normals(vertices, faces):
    tri = vertices[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    return normals[lengths > 0] / lengths[lengths > 0][:, None]


def test_add_flat_gera_normais_para_cima():
    builder = MeshBuilder()
    material = Material("teste", (1.0, 0.0, 0.0))
    builder.add_flat(material, box(0, 0, 10, 10), z=2.0)
    group = builder.build()["teste"]
    verts, faces = group.vertices, group.faces
    assert np.allclose(verts[:, 2], 2.0)
    assert np.all(_normals(verts, faces)[:, 2] > 0.99)


def test_add_flat_flip_inverte_normais():
    builder = MeshBuilder()
    material = Material("teste", (1.0, 0.0, 0.0))
    builder.add_flat(material, box(0, 0, 10, 10), z=0.0, flip=True)
    group = builder.build()["teste"]
    verts, faces = group.vertices, group.faces
    assert np.all(_normals(verts, faces)[:, 2] < -0.99)


def test_paredes_apontam_para_fora():
    builder = MeshBuilder()
    material = Material("parede", (1.0, 1.0, 1.0))
    square = box(-5, -5, 5, 5)
    builder.add_walls(material, square, 0.0, 10.0)
    group = builder.build()["parede"]
    verts, faces = group.vertices, group.faces

    normals = _normals(verts, faces)
    centroids = verts[faces].mean(axis=1)
    outward = centroids.copy()
    outward[:, 2] = 0.0
    lengths = np.linalg.norm(outward, axis=1)
    outward = outward[lengths > 0] / lengths[lengths > 0][:, None]

    # Toda parede deve ter normal horizontal apontando para longe do centro.
    assert np.all(np.abs(normals[:, 2]) < 1e-9)
    assert np.all(np.sum(normals[lengths > 0] * outward, axis=1) > 0.5)


def test_prisma_com_buraco_preserva_area():
    builder = MeshBuilder()
    material = Material("m", (0.5, 0.5, 0.5))
    donut = box(0, 0, 20, 20).difference(box(5, 5, 15, 15))
    builder.add_prism(material, donut, 0.0, 5.0)
    group = builder.build()["m"]
    verts, faces = group.vertices, group.faces
    # Paredes externas (4) + internas (4) = 8 quads = 16 triangulos; mais a tampa.
    assert len(faces) >= 16
    assert verts[:, 2].max() == pytest.approx(5.0)


def test_prisma_nao_gera_nada_com_altura_invertida():
    builder = MeshBuilder()
    builder.add_prism(Material("m", (0, 0, 0)), box(0, 0, 4, 4), 5.0, 2.0)
    assert builder.is_empty()


def test_add_instances_aplica_posicao_escala_rotacao():
    builder = MeshBuilder()
    material = Material("inst", (0.2, 0.7, 0.2))
    proto = (
        np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
        np.array([[0, 1, 2]]),
    )
    transforms = np.array(
        [
            [10.0, 20.0, 1.0, 2.0, 0.0],
            [-5.0, 0.0, 0.0, 1.0, np.pi / 2],
        ]
    )
    builder.add_instances(material, proto, transforms)
    group = builder.build()["inst"]
    verts, faces = group.vertices, group.faces

    assert len(faces) == 2
    assert verts[0] == pytest.approx([10.0, 20.0, 1.0])
    assert verts[1] == pytest.approx([12.0, 20.0, 1.0])  # escala 2, sem rotacao
    assert verts[4] == pytest.approx([-5.0, 1.0, 0.0], abs=1e-9)  # girado 90 graus


def test_grupos_separados_por_material():
    builder = MeshBuilder()
    builder.add_flat(Material("a", (1, 0, 0)), box(0, 0, 1, 1), 0.0)
    builder.add_flat(Material("b", (0, 1, 0)), box(2, 2, 3, 3), 0.0)
    groups = builder.build()
    assert set(groups) == {"a", "b"}


def test_geometria_invalida_e_consertada():
    builder = MeshBuilder()
    # Bowtie: auto-intersecao que precisa passar por buffer(0).
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    builder.add_flat(Material("m", (1, 1, 1)), bowtie, 0.0)
    assert not builder.is_empty()


def test_scene_conta_triangulos_e_limites():
    builder = MeshBuilder()
    builder.add_prism(Material("m", (1, 1, 1)), box(0, 0, 10, 10), 0.0, 7.0)
    scene = Scene(name="t", groups=builder.build())
    assert scene.triangle_count > 0
    bounds = scene.bounds()
    assert bounds[1][2] == pytest.approx(7.0)


# ----------------------------------------------------------------------- estilos


def test_todos_os_estilos_produzem_materiais_validos():
    for name in STYLES:
        palette = get_style(name).palette()
        for material in (palette.ground, palette.asphalt, palette.water, palette.wall(0)):
            r, g, b, a = material.rgba8()
            assert all(0 <= c <= 255 for c in (r, g, b, a))


def test_estilo_desconhecido_lista_opcoes():
    with pytest.raises(KeyError, match="lowpoly"):
        get_style("inexistente")


def test_palette_reutiliza_material_por_nome():
    palette = get_style("lowpoly").palette()
    assert palette.wall(0) is palette.wall(0)
    assert palette.wall(0) is palette.wall(palette.wall_count())  # indice circular

