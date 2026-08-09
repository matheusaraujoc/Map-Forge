"""Redesenho do chao: desenho chapado com as cores medidas na foto."""

import numpy as np

from mapforge.imagery.redraw import (
    PATCH_M,
    TONE_RANGE,
    TONES,
    _patch_field,
    _smooth_partition,
    redraw_ground,
)
from mapforge.imagery.segment import INDEX


class GeoFalso:
    def __init__(self, array, meters_per_pixel=1.0):
        self.array = array
        self.meters_per_pixel = meters_per_pixel
        self.size = (array.shape[1], array.shape[0])

    @property
    def image(self):
        from PIL import Image

        return Image.fromarray(self.array)


def _cena(largura=120, altura=120):
    """Mata escura em cima, gramado claro embaixo."""
    rng = np.random.default_rng(11)
    img = np.zeros((altura, largura, 3), dtype=np.uint8)
    meio = altura // 2
    ruido = rng.integers(-25, 25, size=(meio, largura))
    img[:meio, :, 0] = np.clip(30 + ruido, 0, 255)
    img[:meio, :, 1] = np.clip(76 + ruido, 0, 255)
    img[:meio, :, 2] = np.clip(36 + ruido, 0, 255)
    ruido = rng.integers(-10, 10, size=(altura - meio, largura))
    img[meio:, :, 0] = np.clip(126 + ruido, 0, 255)
    img[meio:, :, 1] = np.clip(178 + ruido, 0, 255)
    img[meio:, :, 2] = np.clip(92 + ruido, 0, 255)
    return GeoFalso(img)


def test_desenha_em_resolucao_maior_que_a_foto():
    geo = _cena()
    r = redraw_ground(geo, target_mpp=0.25)
    assert r.image.size[0] >= 400  # 1 m/px -> 0,25 m/px


def test_a_maior_parte_da_area_e_chapada():
    """E desenho com transicao: o miolo chapado, a beira interpolando.

    Nao da para contar cores unicas - a antisserrilha e a mistura na fronteira
    criam intermediarios de proposito. O que define "chapado" e a *area*: a
    maioria dos pixels tem de estar em cima de um dos tons medidos.
    """
    r = redraw_ground(_cena(), target_mpp=0.5)
    pixels = np.asarray(r.image).reshape(-1, 3).astype(float)

    tons = np.array([t for lista in r.tones.values() for t in lista], dtype=float)
    assert len(tons)
    distancia = np.abs(pixels[:, None, :] - tons[None, :, :]).sum(axis=2).min(axis=1)
    assert (distancia < 18).mean() > 0.7


def test_o_desenho_usa_mais_de_uma_cobertura():
    """A cena tem mata e gramado; o desenho tem de mostrar as duas."""
    r = redraw_ground(_cena(), target_mpp=0.5)
    pixels = np.asarray(r.image).reshape(-1, 3).astype(float)

    tons = np.array([t for lista in r.tones.values() for t in lista], dtype=float)
    mais_perto = np.abs(pixels[:, None, :] - tons[None, :, :]).sum(axis=2).argmin(axis=1)
    # Pelo menos dois tons ocupam area significativa.
    ocupacao = np.bincount(mais_perto, minlength=len(tons)) / len(pixels)
    assert (ocupacao > 0.05).sum() >= 2


def test_as_cores_saem_da_foto_e_nao_de_tabela():
    geo = _cena()
    r = redraw_ground(geo, target_mpp=0.5)

    entrada = geo.array.astype(float)
    meio = entrada.shape[0] // 2
    verde_mata = entrada[: meio - 6].mean(axis=(0, 1))

    # Algum tom de mata tem de estar perto do verde medido.
    tons = np.array(r.tones.get("mata") or r.tones.get("grama") or [[0, 0, 0]], dtype=float)
    assert np.abs(tons - verde_mata).sum(axis=1).min() < 90


def test_os_tons_de_uma_cobertura_sao_vizinhos():
    """Num desenho, os tons do mesmo material nao sao opostos."""
    r = redraw_ground(_cena(), target_mpp=0.5)
    for chave, tons in r.tones.items():
        if len(tons) < 2:
            continue
        arr = np.array(tons, dtype=float)
        assert np.abs(arr.max(axis=0) - arr.min(axis=0)).max() < 130, chave


def test_a_faixa_de_tons_evita_os_extremos():
    assert 0.2 < TONE_RANGE[0] < TONE_RANGE[1] < 0.8


def test_a_mancha_e_grande_o_bastante_para_nao_virar_camuflagem():
    """26 m produziu camuflagem; a mancha tem de ser bem maior que isso."""
    assert PATCH_M >= 50.0


def test_a_particao_suavizada_nao_deixa_vao_nem_sobreposicao():
    labels = np.zeros((40, 40), dtype=np.int64)
    labels[:20] = INDEX["mata"]
    labels[20:] = INDEX["grama"]
    presentes = [INDEX["mata"], INDEX["grama"]]

    saida = _smooth_partition(labels, presentes, 2.0, 2.0, (80, 80))

    assert saida.shape == (80, 80)
    assert set(np.unique(saida)) <= set(presentes)  # so classes validas
    assert (saida == INDEX["mata"]).any() and (saida == INDEX["grama"]).any()


def test_a_fronteira_suavizada_e_mais_curta_que_a_serrilhada():
    """Contorno liso tem menos troca de classe ao longo da borda."""
    rng = np.random.default_rng(3)
    labels = np.where(rng.random((60, 60)) < 0.5, INDEX["mata"], INDEX["grama"])
    presentes = [INDEX["mata"], INDEX["grama"]]

    suave = _smooth_partition(labels, presentes, 3.0, 1.0, (60, 60))

    trocas_antes = int((labels[:, 1:] != labels[:, :-1]).sum())
    trocas_depois = int((suave[:, 1:] != suave[:, :-1]).sum())
    assert trocas_depois < trocas_antes / 4


def test_o_campo_de_manchas_cobre_a_faixa_de_tons():
    """O campo e continuo; quem chapa e `_blend_tones`."""
    campo = _patch_field((200, 200), 60.0, 3, np.random.default_rng(1))

    assert campo.min() >= 0.0
    assert campo.max() <= 2.0 + 1e-5
    # Tem de variar de verdade, senao o desenho sai de uma cor so.
    assert campo.max() - campo.min() > 1.0


def test_blend_chapa_o_miolo_e_suaviza_so_a_beira():
    from mapforge.imagery.redraw import _blend_tones

    tons = [(40, 80, 40), (120, 160, 90)]
    nivel = np.linspace(0.0, 1.0, 400).reshape(1, 400).astype(np.float32)
    cor = _blend_tones(nivel, tons, 0.34)[0]

    perto_do_primeiro = np.abs(cor - np.array(tons[0])).sum(axis=1) < 8
    perto_do_segundo = np.abs(cor - np.array(tons[1])).sum(axis=1) < 8
    chapado = (perto_do_primeiro | perto_do_segundo).mean()

    assert chapado > 0.55  # a maior parte da faixa esta em cima de um tom
    assert chapado < 0.98  # mas existe passagem


def test_o_mesmo_seed_da_o_mesmo_desenho():
    geo = _cena(80, 80)
    a = np.asarray(redraw_ground(geo, target_mpp=0.5, seed=5).image)
    b = np.asarray(redraw_ground(geo, target_mpp=0.5, seed=5).image)
    c = np.asarray(redraw_ground(geo, target_mpp=0.5, seed=6).image)

    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
