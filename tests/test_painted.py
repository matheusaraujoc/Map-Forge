"""Textura pintada: repintura por classe com cores medidas na propria foto."""

import numpy as np

from mapforge.imagery.painted import (
    COVERED_BY_MESH,
    _class_colors,
    _fill_from_neighbours,
    paint_ground,
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

        return Image.fromarray(self.array, mode="RGB")


def _cena(largura=120, altura=120):
    """Mata escura em cima, gramado claro embaixo, com ruido em cada faixa."""
    rng = np.random.default_rng(11)
    img = np.zeros((altura, largura, 3), dtype=np.uint8)
    meio = altura // 2

    ruido = rng.integers(-30, 30, size=(meio, largura))
    img[:meio, :, 0] = np.clip(30 + ruido, 0, 255)
    img[:meio, :, 1] = np.clip(76 + ruido, 0, 255)
    img[:meio, :, 2] = np.clip(36 + ruido, 0, 255)

    ruido = rng.integers(-12, 12, size=(altura - meio, largura))
    img[meio:, :, 0] = np.clip(126 + ruido, 0, 255)
    img[meio:, :, 1] = np.clip(178 + ruido, 0, 255)
    img[meio:, :, 2] = np.clip(92 + ruido, 0, 255)
    return GeoFalso(img)


def test_repinta_cada_classe_com_a_cor_medida_nela():
    geo = _cena()
    pintado = paint_ground(geo, map_data=None, smooth_m=0.0, detail=False)

    saida = np.asarray(pintado.image).astype(float)
    entrada = geo.array.astype(float)
    meio = entrada.shape[0] // 2

    # A cor media de cada faixa tem de continuar sendo a da foto.
    for fatia in (slice(0, meio - 6), slice(meio + 6, None)):
        assert np.allclose(saida[fatia].mean(axis=(0, 1)), entrada[fatia].mean(axis=(0, 1)), atol=14)


def _ruido_espacial(imagem: np.ndarray, fatia) -> float:
    """Desvio dentro de cada canal, sem misturar a diferenca entre canais."""
    return float(np.mean([imagem[fatia, :, c].std() for c in range(3)]))


def test_a_repintura_alisa_o_ruido_da_foto():
    geo = _cena()
    pintado = paint_ground(geo, map_data=None, smooth_m=0.0, variation=0.0)

    saida = np.asarray(pintado.image).astype(float)
    meio = saida.shape[0] // 2
    fatia = slice(0, meio - 6)
    # Sem variacao, cada classe vira chapa - e a foto tinha desvio bem maior.
    assert _ruido_espacial(saida, fatia) < _ruido_espacial(geo.array.astype(float), fatia) / 3


def test_variacao_devolve_relevo_sem_devolver_o_ruido_todo():
    geo = _cena()
    chapado = np.asarray(paint_ground(geo, smooth_m=0.0, variation=0.0).image).astype(float)
    manchado = np.asarray(paint_ground(geo, smooth_m=0.0, variation=0.55).image).astype(float)
    original = geo.array.astype(float)

    fatia = slice(0, original.shape[0] // 2 - 6)
    assert (
        _ruido_espacial(chapado, fatia)
        < _ruido_espacial(manchado, fatia)
        < _ruido_espacial(original, fatia)
    )


def test_cores_e_fracoes_sao_reportadas():
    pintado = paint_ground(_cena(), smooth_m=0.0, detail=False)

    assert pintado.colors  # ao menos uma classe medida
    assert abs(sum(pintado.shares.values()) - 1.0) < 1e-6
    assert pintado.summary()


def test_classes_desenhadas_em_3d_herdam_a_vizinhanca():
    """Telhado, via e agua nao entram na textura do chao: o mesh ja os desenha."""
    labels = np.full((20, 20), INDEX["grama"], dtype=np.int64)
    labels[8:12, 8:12] = INDEX["via"]

    alvo = labels == INDEX["via"]
    preenchido = _fill_from_neighbours(labels, alvo, 1.0)

    assert not (preenchido == INDEX["via"]).any()
    assert (preenchido == INDEX["grama"]).all()


def test_cor_da_classe_e_mediana_e_nao_media():
    """Uma sombra invadindo a mascara nao pode puxar a cor da classe."""
    pixels = np.full((100, 1, 3), 120.0)
    pixels[:5] = 0.0  # cinco pixels pretos de sombra
    labels = np.zeros((100, 1), dtype=np.int64)

    cores = _class_colors(pixels, labels)
    assert np.allclose(cores[0], 120.0)


def test_a_lista_de_classes_cobertas_pelo_mesh_e_coerente():
    for chave in COVERED_BY_MESH:
        assert chave in INDEX


def test_imagem_sem_classe_reconhecivel_nao_quebra():
    ruido = np.random.default_rng(3).integers(0, 255, size=(40, 40, 3), dtype=np.uint8)
    pintado = paint_ground(GeoFalso(ruido), smooth_m=0.0, detail=False)

    assert pintado.image.size == (40, 40)


# --- separacao de frequencias -----------------------------------------------


def test_o_grao_amplia_a_textura_alem_da_resolucao_da_foto():
    """A banda alta e procedural, entao a textura pode passar da foto."""
    geo = _cena(120, 120)  # 1 m/px
    pintado = paint_ground(geo, smooth_m=0.0, target_mpp=0.25)

    # 1 m/px -> 0,25 m/px = 4x em cada eixo.
    assert pintado.image.size[0] >= 400


def test_o_grao_acrescenta_frequencia_alta_sem_mexer_na_cor_macro():
    geo = _cena(120, 120)
    macro = np.asarray(paint_ground(geo, smooth_m=0.0, detail=False).image).astype(float)
    grao = np.asarray(paint_ground(geo, smooth_m=0.0, target_mpp=0.5).image).astype(float)

    # A cor media de cada faixa nao pode andar: o grao modula, nao repinta.
    meio_macro, meio_grao = macro.shape[0] // 2, grao.shape[0] // 2
    assert np.allclose(
        macro[: meio_macro - 6].mean(axis=(0, 1)),
        grao[: meio_grao - 12].mean(axis=(0, 1)),
        atol=10,
    )
    # E o desvio local tem de subir em relacao a mesma textura sem grao - a
    # comparacao tem de ser na mesma resolucao, senao mede a ampliacao e nao o
    # grao. (Contra a foto crua nao serve: nesta cena sintetica a "foto" e ruido
    # puro, com desvio maior que qualquer material real.)
    # Com `variation=0` dos dois lados, a unica diferenca e o grao.
    sem = np.asarray(
        paint_ground(geo, smooth_m=0.0, variation=0.0, target_mpp=0.5, grain=0.0).image
    ).astype(float)
    com = np.asarray(
        paint_ground(geo, smooth_m=0.0, variation=0.0, target_mpp=0.5, grain=1.0).image
    ).astype(float)
    faixa = slice(0, meio_grao - 12)
    assert _ruido_espacial(com, faixa) > _ruido_espacial(sem, faixa) * 3.0


def test_grain_zero_devolve_a_cor_chapada():
    geo = _cena(120, 120)
    liso = np.asarray(
        paint_ground(geo, smooth_m=0.0, variation=0.0, target_mpp=0.5, grain=0.0).image
    ).astype(float)

    meio = liso.shape[0] // 2
    assert _ruido_espacial(liso, slice(0, meio - 12)) < 1.0


def test_o_mesmo_seed_da_o_mesmo_grao():
    geo = _cena(80, 80)
    a = np.asarray(paint_ground(geo, smooth_m=0.0, target_mpp=0.5, seed=7).image)
    b = np.asarray(paint_ground(geo, smooth_m=0.0, target_mpp=0.5, seed=7).image)
    c = np.asarray(paint_ground(geo, smooth_m=0.0, target_mpp=0.5, seed=8).image)

    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_cada_cobertura_tem_grao_proprio():
    """Mata tem grao grosso; capim tem grao fino. Se fossem iguais, nao serviria."""
    from mapforge.imagery.painted import DETAIL

    assert DETAIL["mata"].scale_m > DETAIL["grama"].scale_m * 2
    assert DETAIL["mata"].amplitude > DETAIL["via"].amplitude * 2
