"""Malha de fisica: formas simples que a engine entende.

A malha bonita nao serve como colisor - um predio tem centenas de triangulos
para responder "bati na casa?", que uma caixa responde sozinha.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import box

from mapforge.core.geo import BBox
from mapforge.core.mesh import MeshBuilder
from mapforge.generation.colliders import (
    NAMING,
    BoxShape,
    ColliderSet,
    CylinderShape,
    HeightField,
    _box_mesh,
    _cylinder_mesh,
    _oriented_box,
    add_collider_meshes,
    build_colliders,
)

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


class CtxFalso:
    def __init__(self, draped=False, terrain=None):
        self.half_size = (400.0, 300.0)
        self.draped = draped
        self.terrain = terrain
        self.collider_boxes = []
        self.collider_cylinders = []


# ------------------------------------------------------------------- caixas


def test_a_caixa_acompanha_a_orientacao_do_predio():
    """Casa raramente e paralela ao norte; caixa alinhada ao eixo sobraria."""
    girado = affinity.rotate(box(0, 0, 20, 10), 30, origin="centroid")

    caixa = _oriented_box(girado, base_z=0.0, top_z=6.0)

    assert caixa is not None
    # Os lados voltam a ser 20 x 10, nao a caixa envolvente alinhada ao eixo.
    assert sorted(caixa.size[:2]) == pytest.approx([10.0, 20.0], abs=0.01)
    # E o giro bate com os 30 graus, a menos de meia volta (o lado longo pode
    # ser medido nos dois sentidos).
    assert math.degrees(caixa.yaw) % 180 == pytest.approx(30.0, abs=0.5)


def test_a_altura_da_caixa_vem_do_predio():
    caixa = _oriented_box(box(0, 0, 10, 10), base_z=2.0, top_z=11.0)

    assert caixa.size[2] == pytest.approx(9.0)
    assert caixa.center[2] == pytest.approx(6.5)  # meio da altura


def test_contorno_degenerado_nao_vira_caixa():
    assert _oriented_box(box(0, 0, 0, 0), 0.0, 3.0) is None


def test_predio_pequeno_demais_nao_ganha_colisor():
    ctx = CtxFalso()
    ctx.collider_boxes = [(box(0, 0, 2, 2), 0.0, 3.0)]  # 4 m2

    assert build_colliders(ctx, None).boxes == []


# ---------------------------------------------------------------- cilindros


def test_arvore_vira_cilindro_no_tronco():
    ctx = CtxFalso()
    ctx.collider_cylinders = [(10.0, 20.0, 1.0, 4.5, 0.3)]

    conjunto = build_colliders(ctx, None)

    assert len(conjunto.cylinders) == 1
    cilindro = conjunto.cylinders[0]
    assert cilindro.center == (10.0, 20.0, 1.0)
    assert cilindro.height == pytest.approx(4.5)
    assert cilindro.radius == pytest.approx(0.3)


def test_cilindro_degenerado_e_descartado():
    ctx = CtxFalso()
    ctx.collider_cylinders = [(0.0, 0.0, 0.0, 0.05, 0.3), (0.0, 0.0, 0.0, 3.0, 0.001)]

    assert build_colliders(ctx, None).cylinders == []


# --------------------------------------------------------------------- chao


def test_o_chao_cobre_a_regiao_inteira():
    conjunto = build_colliders(CtxFalso(), None)
    chao = conjunto.ground

    assert chao is not None
    assert chao.origin == (-400.0, -300.0)
    largura = (chao.columns - 1) * chao.step
    assert largura == pytest.approx(800.0, abs=chao.step)
    assert chao.heights.shape == (chao.rows, chao.columns)


def test_o_chao_segue_o_relevo():
    class TerrenoFalso:
        def height(self, xs, ys):
            return np.asarray(xs) * 0.1  # rampa de 10%

    conjunto = build_colliders(CtxFalso(draped=True, terrain=TerrenoFalso()), None)
    alturas = conjunto.ground.heights

    assert alturas.min() == pytest.approx(-40.0, abs=1.0)
    assert alturas.max() == pytest.approx(40.0, abs=1.0)


def test_sem_relevo_o_chao_e_plano():
    assert np.allclose(build_colliders(CtxFalso(), None).ground.heights, 0.0)


# ------------------------------------------------------------------- malhas


def test_a_caixa_vira_doze_triangulos():
    verts, faces = _box_mesh(BoxShape(center=(0, 0, 0), size=(2, 4, 6), yaw=0.0))

    assert len(verts) == 8
    assert len(faces) == 12
    assert verts[:, 0].max() == pytest.approx(1.0)
    assert verts[:, 2].max() == pytest.approx(3.0)


def test_o_cilindro_e_um_prisma_barato():
    verts, faces = _cylinder_mesh(CylinderShape(center=(0, 0, 0), radius=1.0, height=5.0))

    assert len(verts) == 12  # seis lados, dois aneis
    assert len(faces) <= 16
    assert verts[:, 2].max() == pytest.approx(5.0)


def test_o_nome_do_no_segue_a_convencao_da_engine():
    conjunto = ColliderSet(boxes=[BoxShape((0, 0, 0), (4, 4, 4), 0.0)])

    for engine, marca in (("godot", "-colonly"), ("unreal", "UCX_")):
        builder = MeshBuilder()
        add_collider_meshes(builder, conjunto, naming=engine)
        nomes = list(builder.build())
        assert any(marca in n for n in nomes), (engine, nomes)


def test_convencao_desconhecida_cai_no_padrao():
    assert NAMING["godot"] == "-colonly"
    builder = MeshBuilder()
    add_collider_meshes(
        builder, ColliderSet(boxes=[BoxShape((0, 0, 0), (4, 4, 4), 0.0)]), naming="xpto"
    )
    assert any("-colonly" in n for n in builder.build())


# ------------------------------------------------------------------ sidecar


def test_o_json_descreve_as_formas_analiticamente():
    """E o que permite colisor primitivo na engine, em vez de trimesh."""
    conjunto = ColliderSet(
        boxes=[BoxShape((1, 2, 3), (4, 5, 6), 0.5)],
        cylinders=[CylinderShape((7, 8, 9), 0.4, 5.0)],
        ground=HeightField((0, 0), 10.0, 2, 2, np.zeros((2, 2))),
    )

    dados = conjunto.to_dict()

    assert dados["formato"].startswith("mapforge-colisores/")
    assert dados["edificios"][0]["tipo"] == "caixa"
    assert dados["edificios"][0]["centro"] == [1.0, 2.0, 3.0]
    assert dados["vegetacao"][0]["tipo"] == "cilindro"
    assert dados["vegetacao"][0]["raio"] == pytest.approx(0.4)
    assert dados["chao"]["tipo"] == "campo_de_altura"
    assert len(dados["chao"]["alturas"]) == 2


def test_conjunto_vazio_nao_quebra_o_resumo():
    assert "0 caixas" in ColliderSet().summary()


# ------------------------------------------------------- invisivel na tela


def test_todo_no_de_colisao_e_reconhecivel_pelo_nome():
    """O viewport e o render usam isto para nao desenhar a fisica."""
    from mapforge.generation.colliders import is_collider

    conjunto = ColliderSet(
        boxes=[BoxShape((0, 0, 0), (4, 4, 4), 0.0)],
        cylinders=[CylinderShape((0, 0, 0), 0.3, 4.0)],
        ground=HeightField((0, 0), 10.0, 3, 3, np.zeros((3, 3))),
    )
    for engine in ("godot", "unreal", "plain"):
        builder = MeshBuilder()
        add_collider_meshes(builder, conjunto, naming=engine)
        nomes = list(builder.build())
        assert nomes
        assert all(is_collider(n) for n in nomes), (engine, nomes)


def test_o_material_de_colisao_e_invisivel():
    """Vermelho semitransparente cobria o mapa inteiro - foi o defeito."""
    builder = MeshBuilder()
    add_collider_meshes(builder, ColliderSet(boxes=[BoxShape((0, 0, 0), (4, 4, 4), 0.0)]))

    for grupo in builder.build().values():
        assert grupo.material.opacity == 0.0


def test_geometria_normal_nao_e_confundida_com_colisor():
    from mapforge.generation.colliders import is_collider

    for nome in ("wall_01", "roof_sat_03", "ground_redesenhado", "sidewalk", "canopy_00"):
        assert not is_collider(nome)


# ------------------------------------------------------ ligacao com a cena
#
# Os testes acima exercitam o modulo isolado. Estes cobrem o que faltava quando
# ele existia no disco sem ninguem chamar: a fisica so vale se sair da mesma
# geracao que o visual.


def _cena_pequena():
    from shapely.geometry import LineString, Point

    from mapforge.core.features import (
        Building,
        FeatureKind,
        Forest,
        MapData,
        Road,
        RoadClass,
        Tree,
    )

    data = MapData(bbox=BBOX)
    data.buildings.append(
        Building(osm_id=1, kind=FeatureKind.BUILDING, tags={},
                 footprint=box(-30, -20, 10, 20), building_type="house")
    )
    data.buildings.append(
        Building(osm_id=2, kind=FeatureKind.BUILDING, tags={},
                 footprint=box(40, 40, 70, 75), building_type="house")
    )
    data.roads.append(
        Road(osm_id=3, kind=FeatureKind.ROAD, tags={},
             centerline=LineString([(-200, 60), (200, 60)]), road_class=RoadClass.RESIDENTIAL)
    )
    data.trees.append(Tree(osm_id=4, kind=FeatureKind.TREE, tags={}, position=Point(120, -40)))
    data.forests.append(
        Forest(osm_id=5, kind=FeatureKind.FOREST, tags={"natural": "wood"},
               geometry=box(-200, -200, -100, -100))
    )
    return data


def _cena_com_fisica(**kwargs):
    from mapforge.generation import GenerationContext, GenerationSettings, build_scene

    base = dict(seed=1, colliders=True)
    base.update(kwargs)
    ctx = GenerationContext(BBOX, GenerationSettings(**base))
    return build_scene(_cena_pequena(), ctx), ctx


def test_desligada_por_padrao():
    """O mapa continua sendo so o visual ate alguem pedir a fisica."""
    from mapforge.generation import GenerationSettings
    from mapforge.generation.colliders import is_collider

    assert GenerationSettings().colliders is False
    scene, _ = _cena_com_fisica(colliders=False)
    assert not any(is_collider(n) for n in scene.groups)


def test_a_fisica_entra_na_cena_quando_pedida():
    from mapforge.generation.colliders import is_collider

    scene, _ = _cena_com_fisica()
    assert any(is_collider(n) for n in scene.groups)


def test_ha_uma_caixa_por_edificio_gerado():
    """O colisor sai do mesmo laco que desenha o predio, nao de uma releitura."""
    scene, ctx = _cena_com_fisica()
    assert len(ctx.collider_boxes) == 2
    assert len(scene.metadata["colliders"]["edificios"]) == 2


def test_a_caixa_usa_a_altura_que_o_gerador_escolheu():
    scene, ctx = _cena_com_fisica()

    paredes = np.concatenate(
        [g.vertices[:, 2] for n, g in scene.groups.items() if n.startswith("wall_")]
    )
    caixas = scene.metadata["colliders"]["edificios"]
    topo_caixa = max(c["centro"][2] + c["tamanho"][2] / 2.0 for c in caixas)
    # O topo da caixa e o topo da parede - o telhado fica de fora de proposito.
    assert topo_caixa == pytest.approx(paredes.max(), abs=0.01)


def test_a_arvore_vira_cilindro_no_tronco_da_cena():
    scene, ctx = _cena_com_fisica()
    assert len(ctx.collider_cylinders) > 0

    cilindros = scene.metadata["colliders"]["vegetacao"]
    assert cilindros
    # Tronco, nao copa: raio pequeno em relacao a altura.
    for c in cilindros[:20]:
        assert c["raio"] < c["altura"]


def test_a_fisica_nao_e_desenhada_no_render():
    """Foi o defeito do vermelho cobrindo o mapa."""
    from mapforge.generation.colliders import is_collider
    from mapforge.render.software import _gather

    scene, _ = _cena_com_fisica()
    com = _gather(scene)

    fisica = sum(len(g.faces) for n, g in scene.groups.items() if is_collider(n))
    assert fisica > 0
    assert len(com.faces) == scene.triangle_count - fisica


def test_o_sidecar_de_fisica_sai_em_arquivo_proprio(tmp_path):
    """O campo de altura e uma matriz: no sidecar de metadados ele domina tudo."""
    import json

    from mapforge.export import export_scene

    scene, _ = _cena_com_fisica()
    destino = tmp_path / "mapa.glb"
    export_scene(scene, destino)

    lado = tmp_path / "mapa.colisores.json"
    assert lado.exists()
    formas = json.loads(lado.read_text(encoding="utf-8"))
    assert formas["chao"]["tipo"] == "campo_de_altura"

    meta = json.loads((tmp_path / "mapa.glb.json").read_text(encoding="utf-8"))
    assert meta["colliders"]["arquivo"] == "mapa.colisores.json"
    assert "chao" not in meta["colliders"]  # ficou so o ponteiro e o resumo
