"""Grade do mundo, aneis de detalhe e o orcamento de memoria do streaming."""

import math

import pytest

from mapforge.streaming import (
    BASE_TILE_M,
    RINGS,
    Tile,
    TileKey,
    WorldStreamer,
    _distancia_m,
    estimate_world,
    tile_at,
    tile_bbox,
    tiles_for_view,
)

ARAIOSES = (-2.8909, -41.9050)


def test_o_bloco_que_contem_o_ponto_realmente_o_contem():
    for nivel in range(3):
        key = tile_at(*ARAIOSES, nivel)
        caixa = tile_bbox(key)
        assert caixa.south <= ARAIOSES[0] <= caixa.north
        assert caixa.west <= ARAIOSES[1] <= caixa.east


def test_o_lado_do_bloco_bate_com_o_nivel():
    for nivel in range(4):
        caixa = tile_bbox(tile_at(*ARAIOSES, nivel))
        esperado = BASE_TILE_M * (2**nivel)
        assert abs(caixa.height_m - esperado) < esperado * 0.02
        assert abs(caixa.width_m - esperado) < esperado * 0.05


def test_blocos_vizinhos_nao_se_sobrepoem_nem_deixam_vao():
    a = tile_at(*ARAIOSES, 0)
    direita = tile_bbox(TileKey(a.level, a.col + 1, a.row))
    aqui = tile_bbox(a)
    assert abs(aqui.east - direita.west) < 1e-9

    acima = tile_bbox(TileKey(a.level, a.col, a.row + 1))
    assert abs(aqui.north - acima.south) < 1e-9


def test_a_vista_pede_detalhe_alto_perto_e_barato_longe():
    vista = tiles_for_view(*ARAIOSES, radius_m=RINGS[-1][0])
    assert vista

    lat, lon = ARAIOSES
    for key, detalhe in vista:
        distancia = _distancia_m(lat, lon, *tile_bbox(key).center)
        if distancia < RINGS[0][0] / 2:
            assert detalhe == RINGS[0][1]
        if distancia > RINGS[1][0] * 1.5:
            assert detalhe in (RINGS[1][1], RINGS[2][1])


def test_a_lista_vem_do_mais_perto_para_o_mais_longe():
    """O que esta debaixo do nariz tem de ser gerado primeiro."""
    lat, lon = ARAIOSES
    vista = tiles_for_view(lat, lon, radius_m=6_000)
    distancias = [_distancia_m(lat, lon, *tile_bbox(k).center) for k, _ in vista]
    assert distancias == sorted(distancias)


def test_a_vista_nao_cresce_com_o_tamanho_do_mundo():
    """E o ponto do streaming: ver o Brasil custa o mesmo que ver Araioses."""
    perto = len(tiles_for_view(*ARAIOSES, radius_m=3_000))
    longe = len(tiles_for_view(*ARAIOSES, radius_m=200_000))
    # O raio pedido e limitado pelo ultimo anel; 200 km nao pode explodir.
    assert longe == len(tiles_for_view(*ARAIOSES, radius_m=RINGS[-1][0]))
    assert longe < perto * 12


def test_raio_maior_nunca_pede_menos_blocos():
    anterior = 0
    for raio in (500, 1_500, 4_000, 12_000):
        atual = len(tiles_for_view(*ARAIOSES, radius_m=raio))
        assert atual >= anterior
        anterior = atual


def test_mover_pouco_reaproveita_quase_tudo():
    a = {k for k, _ in tiles_for_view(*ARAIOSES, radius_m=3_000)}
    # 200 m ao norte.
    b = {k for k, _ in tiles_for_view(ARAIOSES[0] + 0.0018, ARAIOSES[1], radius_m=3_000)}
    assert len(a & b) > 0.6 * len(a)


def test_o_orcamento_descarta_o_mais_distante_primeiro():
    streamer = WorldStreamer(budget_triangles=1_000)
    streamer._center = ARAIOSES

    perto = tile_at(*ARAIOSES, 0)
    longe = TileKey(perto.level, perto.col + 8, perto.row + 8)
    for key in (perto, longe):
        streamer._tiles[key] = Tile(
            key=key, bbox=tile_bbox(key), detail="low", scene=object(), triangles=800
        )

    streamer._evict_locked()

    assert perto in streamer._tiles
    assert longe not in streamer._tiles


def test_o_orcamento_nao_mexe_no_que_ja_cabe():
    streamer = WorldStreamer(budget_triangles=10_000)
    streamer._center = ARAIOSES
    key = tile_at(*ARAIOSES, 0)
    streamer._tiles[key] = Tile(
        key=key, bbox=tile_bbox(key), detail="low", scene=object(), triangles=800
    )

    streamer._evict_locked()
    assert key in streamer._tiles


def test_estimate_world_diz_que_o_brasil_nao_cabe():
    brasil = estimate_world(8_510_000, detail="distante")
    # Dezenas de TB e centenas de bilhoes de triangulos, no nivel mais barato.
    assert brasil["terabytes"] > 10
    assert brasil["triangulos"] > 1e11

    araioses = estimate_world(2.0, detail="medium")
    assert araioses["megabytes"] < 20  # um bloco cabe de sobra


@pytest.mark.parametrize("nivel", [0, 1, 2])
def test_a_grade_e_estavel_para_o_mesmo_ponto(nivel):
    """Dois pontos dentro do mesmo bloco tem de dar a mesma chave."""
    lat, lon = ARAIOSES
    passo = BASE_TILE_M * (2**nivel) / 111_320.0 / 4.0
    assert tile_at(lat, lon, nivel) == tile_at(lat + passo * 0.1, lon, nivel)
