"""Procedencia da geometria.

Este modulo nasceu de uma investigacao que travou: apareciam chapas claras no
mapa e, olhando so o material, nao dava para saber quem as tinha criado - `curb`
e meio-fio, embasamento, laje de ponte e pilar de viaduto ao mesmo tempo. Foram
tres hipoteses plausiveis e tres medicoes ate a pergunta certa aparecer: *qual
funcao produziu aquele triangulo?*
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from mapforge.core.geo import BBox
from mapforge.core.mesh import Material, MeshBuilder
from mapforge.diagnostics import (
    GeometryLog,
    manchas,
    o_que_ha_em,
    relatorio,
    top_down,
    visivel_por_origem,
)

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)
CLARO = Material("claro", (0.85, 0.83, 0.78))
ESCURO = Material("escuro", (0.20, 0.20, 0.22))


# ------------------------------------------------------------------ procedencia


def _telhado(builder, geom, z):
    """Funcao com nome proprio: e ela que o log tem de apontar."""
    builder.add_flat(CLARO, geom, z)


def _calcada(builder, geom, z):
    builder.add_flat(CLARO, geom, z)


def test_o_log_aponta_a_funcao_que_criou_a_geometria():
    log = GeometryLog()
    builder = MeshBuilder(log=log)
    _telhado(builder, box(0, 0, 10, 10), 6.0)

    assert len(log.lotes) == 1
    assert log.lotes[0].origem.endswith("._telhado")
    assert log.lotes[0].material == "claro"


def test_o_mesmo_material_separa_origens_diferentes():
    """O ponto do modulo: material nao identifica quem construiu."""
    log = GeometryLog()
    builder = MeshBuilder(log=log)
    _telhado(builder, box(0, 0, 10, 10), 6.0)
    _calcada(builder, box(20, 0, 30, 4), 0.16)

    origens = {lote.origem.split(".")[-1] for lote in log.lotes}
    assert origens == {"_telhado", "_calcada"}


def test_sem_log_o_builder_nao_muda_de_comportamento():
    com, sem = MeshBuilder(log=GeometryLog()), MeshBuilder()
    for b in (com, sem):
        b.add_flat(CLARO, box(0, 0, 10, 10), 1.0)
    assert len(com.build()["claro"].faces) == len(sem.build()["claro"].faces)


def test_o_log_conta_faces_horizontais():
    log = GeometryLog()
    builder = MeshBuilder(log=log)
    builder.add_flat(CLARO, box(0, 0, 10, 10), 3.0)  # horizontal
    builder.add_walls(ESCURO, box(0, 0, 10, 10), 0.0, 3.0)  # vertical

    horizontais = {l.material: l.horizontais for l in log.lotes}
    assert horizontais["claro"] > 0
    assert horizontais["escuro"] == 0
    assert log.lotes[0].area_h == pytest.approx(100.0, rel=1e-6)


def test_a_face_sabe_dizer_de_quem_e():
    log = GeometryLog()
    builder = MeshBuilder(log=log)
    _telhado(builder, box(0, 0, 10, 10), 6.0)
    _calcada(builder, box(20, 0, 30, 4), 0.16)

    primeira = log.dono("claro", 0)
    ultima = log.dono("claro", log.lotes[-1].primeira_face)
    assert primeira.origem.endswith("._telhado")
    assert ultima.origem.endswith("._calcada")
    assert log.dono("claro", 10_000) is None


def test_lote_vazio_e_ignorado():
    log = GeometryLog()
    log.record("x", np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    assert log.lotes == []


# ------------------------------------------------------------------ visibilidade


def _cena_de_teste():
    """Chapa alta cobrindo uma faixa baixa: so a de cima deve ser vista."""
    log = GeometryLog()
    builder = MeshBuilder(log=log)
    _calcada(builder, box(-30, -30, 30, 30), 0.16)
    _telhado(builder, box(-10, -10, 10, 10), 6.0)

    from mapforge.core.mesh import Scene

    return Scene(name="t", groups=builder.build()), log


def test_o_que_esta_por_baixo_nao_conta():
    """Area no arquivo nao e area vista - foi o que enganou a primeira analise."""
    scene, log = _cena_de_teste()
    visivel, _ = visivel_por_origem(scene, log, passo=0.5)

    do_telhado = sum(v for (origem, _), v in visivel.items() if origem.endswith("._telhado"))
    da_calcada = sum(v for (origem, _), v in visivel.items() if origem.endswith("._calcada"))

    assert do_telhado == pytest.approx(400.0, rel=0.05)  # 20 x 20
    assert da_calcada == pytest.approx(3600.0 - 400.0, rel=0.05)  # 60 x 60 menos o vao


def test_a_pilha_lista_de_cima_para_baixo():
    scene, log = _cena_de_teste()
    texto = o_que_ha_em(scene, log, 0.0, 0.0, raio=6.0)
    linhas = [l for l in texto.splitlines() if "z=" in l]
    assert linhas
    assert "_telhado" in linhas[0]  # o de cima primeiro


def test_o_relatorio_nomeia_origem_e_material():
    scene, log = _cena_de_teste()
    texto = relatorio(scene, log, passo=0.5)
    assert "_telhado" in texto and "_calcada" in texto and "claro" in texto


# ---------------------------------------------------------------- faixa x chapa


def test_faixa_e_chapa_tem_a_mesma_area_e_espessuras_diferentes():
    """A medida que resolveu a investigacao.

    Uma calcada de 2 m de largura por 300 m e uma chapa de 25 x 25 m tem a mesma
    ordem de area. O que as separa e o maior circulo que cabe dentro.
    """
    log = GeometryLog()
    builder = MeshBuilder(log=log)
    _calcada(builder, box(-150, -1, 150, 1), 0.16)  # 600 m2, 2 m de largura
    _telhado(builder, box(300, -12, 325, 13), 6.0)  # 625 m2, 25 m de lado

    from mapforge.core.mesh import Scene

    scene = Scene(name="t", groups=builder.build())
    achadas = manchas(scene, log, passo=0.5, area_min=100.0)

    assert len(achadas) == 2
    chapa = [m for m in achadas if m.e_chapa]
    faixa = [m for m in achadas if not m.e_chapa]
    assert len(chapa) == 1 and len(faixa) == 1
    assert chapa[0].origem.endswith("._telhado")
    assert faixa[0].origem.endswith("._calcada")
    assert faixa[0].espessura < 2.0
    assert chapa[0].espessura > 10.0


def test_o_limiar_de_chapa_acompanha_a_resolucao():
    """Numa regiao grande o passo afrouxa; o limiar tem de afrouxar junto.

    Com passo de 2,1 m, uma espessura medida de 4,2 m vale duas celulas - nao da
    para chamar isso de chapa com seguranca.
    """
    from mapforge.diagnostics import Mancha

    fina = Mancha("m", "o", area=500, espessura=4.2, x=0, y=0, passo=0.5)
    mesma_grossa = Mancha("m", "o", area=500, espessura=4.2, x=0, y=0, passo=2.1)
    assert fina.e_chapa
    assert not mesma_grossa.e_chapa  # a mesma medida, sem resolucao para afirmar


def test_material_texturado_nao_entra_como_claro():
    """A cor base de um material texturado e branca e nao significa nada."""
    from PIL import Image

    from mapforge.core.mesh import Scene

    textura = Material("chao", (1.0, 1.0, 1.0), texture=Image.new("RGB", (4, 4)))
    builder = MeshBuilder()
    builder.add_flat(textura, box(-50, -50, 50, 50), 0.0)
    scene = Scene(name="t", groups=builder.build())

    assert manchas(scene, None, passo=1.0, area_min=100.0) == []


def test_top_down_devolve_a_face_e_nao_so_o_material():
    """E o que permite perguntar ao log quem construiu aquele pixel."""
    scene, _ = _cena_de_teste()
    visao = top_down(scene, passo=1.0)
    assert set(visao.nomes) == {"claro"}
    assert (visao.face >= 0).any()
    assert np.isfinite(visao.topo[visao.material >= 0]).all()


def test_a_rasterizacao_e_reaproveitada_em_vez_de_refeita():
    """Rasterizar e a parte cara: relatorio e manchas partilham uma visao so."""
    scene, log = _cena_de_teste()
    visao = top_down(scene, passo=0.5)

    chamadas = {"n": 0}
    import mapforge.diagnostics as d

    original = d.top_down

    def contando(*a, **k):
        chamadas["n"] += 1
        return original(*a, **k)

    d.top_down = contando
    try:
        d.relatorio(scene, log, visao=visao)
        d.manchas(scene, log, visao=visao)
    finally:
        d.top_down = original
    assert chamadas["n"] == 0


def test_regiao_grande_afrouxa_o_passo_em_vez_de_travar():
    """O defeito que travou a interface: grade fina demais numa regiao grande.

    Sem teto, 11 km2 a 0,6 m dao 8,9 milhoes de celulas e o retrato passa de
    minutos - dentro da thread de geracao, o que aparece como "carregando" sem
    fim.
    """
    from mapforge.core.mesh import Scene

    builder = MeshBuilder()
    builder.add_flat(ESCURO, box(-2000, -2000, 2000, 2000), 0.0)  # 16 km2
    scene = Scene(name="grande", groups=builder.build())

    visao = top_down(scene, passo=0.2)  # pediria 400 milhoes de celulas
    assert visao.passo > 0.2
    assert visao.material.size <= d_max_celulas() * 1.05


def d_max_celulas() -> int:
    from mapforge.diagnostics import MAX_CELULAS

    return MAX_CELULAS
