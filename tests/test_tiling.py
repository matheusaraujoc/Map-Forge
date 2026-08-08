"""Testes da geracao em blocos para regioes grandes. Sem rede."""

from __future__ import annotations

import json

import pytest

from mapforge import config
from mapforge.core.geo import BBox, LocalProjection
from mapforge.generation import GenerationSettings
from mapforge.tiling import (
    DEFAULT_TILE_KM,
    TiledResult,
    _group_tiles,
    _write_manifest,
    plan_tiles,
)

# ~6 x 6 km, o tamanho de Parnaiba: nao cabe numa cena so.
GRANDE = BBox(-2.9418, -41.7932, -2.8877, -41.7392)


# ------------------------------------------------------------------ plano


def test_regiao_grande_vira_varios_blocos():
    plano, cols, rows = plan_tiles(GRANDE, tile_km=1.5)
    assert cols >= 4 and rows >= 4
    assert len(plano) == cols * rows


def test_bloco_menor_gera_mais_blocos():
    _, c1, r1 = plan_tiles(GRANDE, tile_km=3.0)
    _, c2, r2 = plan_tiles(GRANDE, tile_km=1.0)
    assert c2 * r2 > c1 * r1


def test_blocos_cobrem_a_regiao_inteira():
    plano, _, _ = plan_tiles(GRANDE, tile_km=1.5)
    assert min(t.bbox[0] for t in plano) == pytest.approx(GRANDE.south)
    assert min(t.bbox[1] for t in plano) == pytest.approx(GRANDE.west)
    assert max(t.bbox[2] for t in plano) == pytest.approx(GRANDE.north)
    assert max(t.bbox[3] for t in plano) == pytest.approx(GRANDE.east)


def test_blocos_nao_se_sobrepoem():
    plano, cols, _ = plan_tiles(GRANDE, tile_km=1.5)
    por_pos = {(t.row, t.col): t for t in plano}
    for (row, col), tile in por_pos.items():
        direita = por_pos.get((row, col + 1))
        if direita:
            # Leste de um e exatamente o oeste do proximo.
            assert tile.bbox[3] == pytest.approx(direita.bbox[1])
        acima = por_pos.get((row + 1, col))
        if acima:
            assert tile.bbox[2] == pytest.approx(acima.bbox[0])


def test_deslocamento_bate_com_a_posicao_do_bloco():
    """O manifesto so serve se o deslocamento posicionar o bloco certo."""
    plano, cols, rows = plan_tiles(GRANDE, tile_km=1.5)
    projection = LocalProjection.for_bbox(GRANDE)
    for tile in plano:
        bbox = BBox(*tile.bbox)
        esperado = projection.project(*bbox.center)
        assert tile.offset[0] == pytest.approx(esperado[0], abs=0.01)
        assert tile.offset[1] == pytest.approx(esperado[1], abs=0.01)


def test_deslocamento_cresce_para_leste_e_para_o_norte():
    plano, cols, _ = plan_tiles(GRANDE, tile_km=1.5)
    por_pos = {(t.row, t.col): t for t in plano}
    a, b = por_pos[(0, 0)], por_pos[(0, 1)]
    assert b.offset[0] > a.offset[0]  # coluna seguinte esta a leste
    assert por_pos[(1, 0)].offset[1] > a.offset[1]  # linha seguinte esta ao norte


def test_regiao_pequena_vira_um_bloco_so():
    pequena = BBox(-2.900, -41.760, -2.895, -41.755)
    plano, cols, rows = plan_tiles(pequena, tile_km=DEFAULT_TILE_KM)
    assert (cols, rows) == (1, 1)
    assert len(plano) == 1


def test_area_do_bloco_fica_abaixo_do_limite_de_uma_consulta():
    plano, cols, rows = plan_tiles(GRANDE, tile_km=DEFAULT_TILE_KM)
    for tile in plano:
        assert BBox(*tile.bbox).area_km2 < config.MAX_AREA_KM2


# ------------------------------------------------------------ agrupamento


def test_blocos_vizinhos_caem_no_mesmo_download():
    """Um download por super-bloco: era o que dominava o relogio."""
    plano, cols, rows = plan_tiles(GRANDE, tile_km=1.5)
    grupos = _group_tiles(plano, GRANDE, cols, rows)
    assert len(grupos) < len(plano)


def test_todo_bloco_pertence_a_exatamente_um_grupo():
    plano, cols, rows = plan_tiles(GRANDE, tile_km=1.5)
    grupos = _group_tiles(plano, GRANDE, cols, rows)
    vistos = [t for tiles in grupos.values() for t in tiles]
    assert len(vistos) == len(plano)
    assert len({(t.row, t.col) for t in vistos}) == len(plano)


def test_envolvente_do_grupo_respeita_o_limite_de_consulta():
    plano, cols, rows = plan_tiles(GRANDE, tile_km=1.5)
    for tiles in _group_tiles(plano, GRANDE, cols, rows).values():
        envolvente = BBox(
            min(t.bbox[0] for t in tiles), min(t.bbox[1] for t in tiles),
            max(t.bbox[2] for t in tiles), max(t.bbox[3] for t in tiles),
        )
        assert envolvente.area_km2 <= config.MAX_AREA_KM2


# -------------------------------------------------------------- manifesto


def test_manifesto_guarda_parametros_e_montagem(tmp_path):
    plano, cols, rows = plan_tiles(GRANDE, tile_km=1.5)
    for tile in plano:
        tile.file = f"x_r{tile.row}c{tile.col}.glb"
        tile.triangles = 100
        tile.buildings = 5

    resultado = TiledResult(
        name="x",
        region=[GRANDE.south, GRANDE.west, GRANDE.north, GRANDE.east],
        tile_km=1.5,
        grid=[cols, rows],
        tiles=plano,
    )
    settings = GenerationSettings(seed=77, style="cartoon")
    caminho = _write_manifest(resultado, settings, tmp_path, "x")

    dados = json.loads(caminho.read_text(encoding="utf-8"))
    # Reconstruivel a partir de parametros: seed e configuracoes vao junto.
    assert dados["settings"]["seed"] == 77
    assert dados["settings"]["style"] == "cartoon"
    assert dados["grid"] == {"cols": cols, "rows": rows}
    assert len(dados["tiles"]) == len(plano)
    assert dados["totals"]["triangles"] == 100 * len(plano)
    # A convencao de eixo precisa estar escrita: sem ela a montagem sai torta.
    assert "Y-up" in dados["axis"]


def test_manifesto_registra_bloco_que_falhou(tmp_path):
    plano, cols, rows = plan_tiles(GRANDE, tile_km=3.0)
    plano[0].error = "nenhum dado encontrado"
    resultado = TiledResult(
        name="x", region=[0, 0, 1, 1], tile_km=3.0, grid=[cols, rows], tiles=plano
    )
    caminho = _write_manifest(resultado, GenerationSettings(), tmp_path, "x")
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    assert dados["tiles"][0]["error"]
    assert dados["totals"]["generated"] == 0


def test_resultado_conta_apenas_blocos_gerados():
    plano, cols, rows = plan_tiles(GRANDE, tile_km=3.0)
    plano[0].file = "a.glb"
    plano[0].triangles = 10
    resultado = TiledResult(
        name="x", region=[0, 0, 1, 1], tile_km=3.0, grid=[cols, rows], tiles=plano
    )
    assert len(resultado.ok) == 1
    assert resultado.triangles == 10
