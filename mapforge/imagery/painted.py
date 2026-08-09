"""Textura propria, pintada a partir da foto.

Colar a foto de satelite no terreno nao funciona: a foto tem carro, sombra de
poste, ruido de compressao e a propria rua - que o modelo ja desenha em 3D -, e
o resultado briga com a estilizacao low-poly. Foi por isso que o modo
`--ground-texture` cru ficou de lado.

Pintar retalho chapado por poligono tambem nao funciona, e o defeito e visivel:
cada mancha detectada vira um bloco de cor unica, com degrau duro na divisa, e o
chao fica com cara de mapa de bloco em vez de terreno.

O caminho do meio e este: **classificar cada pixel e repinta-lo com a cor
daquela cobertura naquele lugar**. Mata recebe o verde que a mata tem ali,
gramado o verde do gramado dali, solo exposto o ocre do solo dali - cor medida,
nao cor de tabela. A variacao interna de cada classe e preservada em parte, o
que da relevo e evita a chapa; o que se perde e exatamente o que atrapalhava:
carro, poste, ruido e borda dura.

Nao ha padrao nem paleta fixa - a textura sai diferente em cada regiao porque as
cores vem da regiao. Um cerrado sai ocre; um manguezal sai verde-escuro.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .segment import CLASSES, INDEX, classify

log = logging.getLogger(__name__)

# Quanto da variacao interna da classe sobrevive na repintura. Em 0 a classe
# vira chapa; em 1 volta a ser a foto. O que se quer e o meio: manchado o
# bastante para nao parecer tinta, liso o bastante para parecer estilizado.
DEFAULT_VARIATION = 0.55

# Suavizacao das divisas entre classes, em metros. Sem ela a repintura mantem o
# serrilhado do classificador pixel a pixel.
DEFAULT_SMOOTH_M = 1.6

# Classes que nao devem entrar na textura do chao porque o modelo ja as desenha
# em 3D: elas herdam a cor da vizinhanca em vez de aparecer na foto pintada.
COVERED_BY_MESH = ("telha", "laje", "telhado_escuro", "via", "agua")

# Classes cuja cor pode ser medida com seguranca. Sombra e "desconhecido" nao
# tem cor propria - sao ausencia de informacao.
BORROWS_NEIGHBOUR = ("sombra", "desconhecido")

# Teto do lado da textura.
#
# A textura e uma imagem so esticada sobre a regiao inteira, entao a resolucao
# por metro cai conforme a regiao cresce: 0,35 m/px em 1,4 km, 0,68 em 2,8 km e
# 1,19 em 4,4 km - onde ela empata com a foto e o grao deixa de existir.
#
# Subir o teto para 8192 foi medido e nao compensa: recupera para 0,54 m/px numa
# regiao de 4,4 km, mas o arquivo vai a **112,7 MB** e a geracao a **471 s**. O
# caminho certo nao e guardar mais pixel de foto, e sintetizar o detalhe - e,
# para regiao grande, dividir o terreno em blocos de textura.
TEXTURE_MAX_PX = 4096

# --- detalhe procedural -----------------------------------------------------
#
# O problema do "8 ou 80": ou a superficie e cor chapada, sem vida, ou e a foto
# de satelite, que a 1,2 m/px esta borrada e cheia de ruido de compressao. Falta
# o meio.
#
# A saida e classica em terreno de jogo: **separacao de frequencias**. Uma foto
# de satelite so tem informacao de frequencia *baixa* - a cor macro, onde muda
# de mata para pasto. A frequencia *alta* - o grao da terra, o granulado do
# asfalto, o recorte de copa - ela nao tem, e nunca vai ter, porque o pixel dela
# e maior que o detalhe.
#
# Entao: a cor macro vem do dado (medida, verdadeira), e o detalhe micro vem de
# ruido procedural com a escala e a aspereza certas *para cada cobertura*. O
# resultado tem a leitura do lugar real e a nitidez que a foto nao tem, numa
# resolucao maior que a da foto - porque o que foi acrescentado nao e foto
# inventada, e material.


@dataclass(frozen=True)
class DetailSpec:
    """Como e o grao de uma cobertura, em metros e em contraste."""

    scale_m: float  # tamanho do detalhe dominante
    amplitude: float  # variacao de luminancia, 0-1
    octaves: int = 3
    roughness: float = 0.5  # quanto cada oitava seguinte pesa
    tint: float = 0.0  # quanto o detalhe tambem mexe na cor, nao so no brilho


# Amplitudes calibradas olhando a textura ao lado da foto: o primeiro ajuste
# ficou com o dobro disto e o chao parecia sujo, nao texturado. O grao tem de
# ser percebido como material a media distancia, nao como chuvisco.
DETAIL: dict[str, DetailSpec] = {
    # Copa e o caso mais grosseiro: o "grao" e a propria arvore.
    "mata": DetailSpec(scale_m=6.5, amplitude=0.21, octaves=4, roughness=0.58, tint=0.22),
    # Capim tem grao fino e contraste baixo; o que se ve de longe e a falha.
    "grama": DetailSpec(scale_m=1.8, amplitude=0.10, octaves=3, roughness=0.5),
    # Terra batida: sulco, poca seca, pedra solta.
    "solo": DetailSpec(scale_m=2.6, amplitude=0.15, octaves=4, roughness=0.55, tint=0.12),
    # Asfalto: agregado fino, quase sem contraste, mas nao zero - e o que tira o
    # aspecto de plastico.
    "via": DetailSpec(scale_m=0.6, amplitude=0.06, octaves=2, roughness=0.45),
    # Agua tem a lamina 3D por cima; o fundo so precisa nao ser chapado.
    "agua": DetailSpec(scale_m=9.0, amplitude=0.04, octaves=2, roughness=0.6),
    "sombra": DetailSpec(scale_m=3.2, amplitude=0.07, octaves=2, roughness=0.5),
}

DEFAULT_DETAIL = DetailSpec(scale_m=2.2, amplitude=0.08, octaves=3)

# Resolucao alvo da textura. A foto tem ~1,2 m/px; o detalhe procedural sustenta
# bem mais que isso, e e ai que a nitidez aparece.
DEFAULT_TARGET_MPP = 0.35


@dataclass
class PaintedGround:
    """A textura pintada e o que foi medido para chegar nela."""

    image: object  # PIL.Image
    colors: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    shares: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        partes = [
            f"{chave} {100 * fracao:.0f}%"
            for chave, fracao in sorted(self.shares.items(), key=lambda kv: -kv[1])
            if fracao >= 0.02
        ]
        return ", ".join(partes) or "sem cobertura medida"


def _class_colors(array: np.ndarray, labels: np.ndarray) -> dict[int, np.ndarray]:
    """Cor representativa de cada classe: mediana dos pixels dela na foto.

    Mediana, e nao media, porque uma sombra ou um telhado invadindo a mascara
    puxa a media e nao mexe na mediana.
    """
    cores: dict[int, np.ndarray] = {}
    for indice in np.unique(labels):
        pixels = array[labels == indice]
        if len(pixels) < 12:
            continue
        if len(pixels) > 40_000:  # a mediana ja e estavel muito antes disso
            pixels = pixels[:: len(pixels) // 40_000 + 1]
        cores[int(indice)] = np.median(pixels, axis=0)
    return cores


def _fill_from_neighbours(
    labels: np.ndarray, alvo: np.ndarray, meters_per_pixel: float
) -> np.ndarray:
    """Substitui os rotulos de `alvo` pelo rotulo valido mais proximo.

    E o que resolve sombra, telhado e rua na textura do chao: em vez de pintar
    uma mancha escura ou um asfalto que o modelo ja desenha em 3D, o pixel herda
    a cobertura vizinha - o chao continua embaixo, so estava escondido.
    """
    from scipy import ndimage

    if not alvo.any():
        return labels
    if alvo.all():
        return labels

    # `distance_transform_edt` com `return_indices` devolve, para cada pixel, as
    # coordenadas do pixel valido mais proximo. Uma chamada resolve tudo.
    _, (linhas, colunas) = ndimage.distance_transform_edt(
        alvo, return_indices=True, sampling=(meters_per_pixel, meters_per_pixel)
    )
    saida = labels.copy()
    saida[alvo] = labels[linhas[alvo], colunas[alvo]]
    return saida


def _value_noise(
    shape: tuple[int, int], cell_px: float, rng: np.random.Generator
) -> np.ndarray:
    """Ruido de valor: grade grosseira sorteada, interpolada suavemente.

    Nao e Perlin - e mais barato e, para grao de material visto de cima, da no
    mesmo. O que importa e ter uma banda de frequencia controlada.
    """
    from scipy import ndimage

    altura, largura = shape
    cell_px = max(cell_px, 1.2)
    baixa = (
        max(int(round(altura / cell_px)) + 2, 2),
        max(int(round(largura / cell_px)) + 2, 2),
    )
    grade = rng.random(baixa)
    # `order=3` (spline cubica) evita o losango que a interpolacao bilinear
    # deixa e que o olho identifica na hora como ruido de computador.
    ampliada = ndimage.zoom(
        grade, (altura / baixa[0], largura / baixa[1]), order=3, mode="reflect"
    )
    return _fit(ampliada, shape)


def _fit(array: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Corta ou repete para bater exatamente com a forma pedida."""
    altura, largura = shape
    if array.shape[0] < altura or array.shape[1] < largura:
        repeticoes = (
            int(np.ceil(altura / array.shape[0])),
            int(np.ceil(largura / array.shape[1])),
        )
        array = np.tile(array, repeticoes)
    return array[:altura, :largura]


def _fbm(
    shape: tuple[int, int], cell_px: float, spec: DetailSpec, rng: np.random.Generator
) -> np.ndarray:
    """Soma de oitavas, centrada em zero e normalizada para +-1."""
    total = np.zeros(shape)
    peso_total = 0.0
    peso = 1.0
    escala = cell_px
    for _ in range(max(spec.octaves, 1)):
        total += _value_noise(shape, escala, rng) * peso
        peso_total += peso
        peso *= spec.roughness
        escala *= 0.5
        if escala < 1.2:
            break
    total /= max(peso_total, 1e-6)
    total -= total.mean()
    pico = np.abs(total).max()
    return total / pico if pico > 1e-9 else total


def _apply_detail(
    pintado: np.ndarray,
    labels: np.ndarray,
    target_mpp: float,
    seed: int,
    grain: float = 1.0,
) -> np.ndarray:
    """Acrescenta o grao de cada cobertura sobre a cor macro.

    Uma passada de ruido por classe presente, aplicada so onde aquela classe
    esta. Classes cobertas pela malha 3D nao entram - o telhado tem material
    proprio e a rua e desenhada em 3D.
    """
    shape = labels.shape
    saida = pintado
    for chave, spec in list(DETAIL.items()) + [("_resto", DEFAULT_DETAIL)]:
        if chave == "_resto":
            mascara = np.ones(shape, dtype=bool)
            for outra in DETAIL:
                mascara &= labels != INDEX[outra]
            for outra in COVERED_BY_MESH:
                mascara &= labels != INDEX[outra]
        else:
            mascara = labels == INDEX[chave]
        if not mascara.any():
            continue

        rng = np.random.default_rng(seed + INDEX.get(chave, 99) * 7919)
        ruido = _fbm(shape, spec.scale_m / max(target_mpp, 0.05), spec, rng)

        ganho = 1.0 + spec.amplitude * grain * ruido
        saida = np.where(mascara[:, :, None], saida * ganho[:, :, None], saida)
        if spec.tint > 1e-3:
            # Detalhe que tambem desloca a cor: mata nao varia so em brilho,
            # varia entre verde-oliva e verde-escuro de folha nova.
            desvio = np.stack([-ruido, ruido * 0.4, -ruido * 0.6], axis=-1)
            saida = np.where(
                mascara[:, :, None], saida + desvio * spec.tint * grain * 26.0, saida
            )
    return saida


def paint_ground(
    geo,
    map_data=None,
    variation: float = DEFAULT_VARIATION,
    smooth_m: float = DEFAULT_SMOOTH_M,
    max_size: int = TEXTURE_MAX_PX,
    target_mpp: float = DEFAULT_TARGET_MPP,
    detail: bool = True,
    seed: int = 1,
    grain: float = 1.0,
) -> PaintedGround:
    """Repinta a foto classe por classe, com as cores medidas na propria foto."""
    from PIL import Image
    from scipy import ndimage

    array = geo.array.astype(np.float64)
    mpp = geo.meters_per_pixel

    seg = classify(geo, map_data)
    labels = seg.labels.astype(np.int64)

    # O que o modelo ja desenha em 3D, e o que nao tem cor propria, herda a
    # cobertura vizinha em vez de entrar na textura.
    ocultar = np.zeros(labels.shape, dtype=bool)
    for chave in COVERED_BY_MESH + BORROWS_NEIGHBOUR:
        ocultar |= labels == INDEX[chave]
    labels_chao = _fill_from_neighbours(labels, ocultar, mpp)

    # A cor de cada classe sai da foto onde a classe foi *observada*, nao onde
    # ela foi herdada - senao a mediana da mata se contamina com telhado.
    cores = _class_colors(array, np.where(ocultar, -1, labels))
    if not cores:
        log.warning("Nenhuma classe com pixels suficientes; textura vira a foto crua")
        return PaintedGround(image=geo.image.convert("RGB"))

    padrao = np.median(np.stack(list(cores.values())), axis=0)
    tabela = np.tile(padrao, (len(CLASSES), 1))
    for indice, cor in cores.items():
        if 0 <= indice < len(CLASSES):
            tabela[indice] = cor

    pintado = tabela[labels_chao]

    # Devolve parte da variacao interna: o desvio de luminancia de cada pixel em
    # relacao a mediana da sua classe. E o que impede a textura de virar tinta
    # chapada, sem trazer de volta o ruido de cor da foto.
    if variation > 1e-3:
        lum = array @ np.array([0.2126, 0.7152, 0.0722])
        lum_classe = (tabela @ np.array([0.2126, 0.7152, 0.0722]))[labels_chao]
        ganho = 1.0 + variation * np.clip(
            (lum - lum_classe) / np.maximum(lum_classe, 12.0), -0.8, 0.8
        )
        pintado *= ganho[:, :, None]

    if smooth_m > 1e-3:
        sigma = max(smooth_m / max(mpp, 0.05), 0.5)
        for canal in range(3):
            pintado[:, :, canal] = ndimage.gaussian_filter(pintado[:, :, canal], sigma)

    # --- separacao de frequencias ---
    #
    # Ate aqui so ha frequencia baixa, porque so isso a foto tinha. A ampliacao
    # abaixo nao inventa foto: ela leva a cor macro para uma resolucao maior e
    # preenche a banda alta com o grao do material. Os rotulos sobem por vizinho
    # mais proximo, para a divisa entre coberturas continuar nitida - ampliar os
    # rotulos por interpolacao produziria uma faixa de cobertura inexistente na
    # borda de cada mancha.
    escala = 1.0
    if detail and target_mpp > 0.01 and mpp / target_mpp > 1.2:
        escala = min(
            mpp / target_mpp,
            max_size / max(pintado.shape[0], pintado.shape[1]),
        )

    if escala > 1.2:
        alvo_mpp = mpp / escala
        pintado = np.stack(
            [ndimage.zoom(pintado[:, :, c], escala, order=1) for c in range(3)], axis=-1
        )
        labels_alvo = ndimage.zoom(labels_chao, escala, order=0)
        labels_alvo = labels_alvo[: pintado.shape[0], : pintado.shape[1]]
        pintado = _apply_detail(pintado, labels_alvo, alvo_mpp, seed, grain)
    elif detail:
        pintado = _apply_detail(pintado, labels_chao, mpp, seed, grain)

    imagem = Image.fromarray(np.clip(pintado, 0, 255).astype(np.uint8))
    if max(imagem.size) > max_size:
        escala = max_size / max(imagem.size)
        imagem = imagem.resize(
            (max(int(imagem.width * escala), 1), max(int(imagem.height * escala), 1)),
            Image.LANCZOS,
        )

    total = labels.size
    resultado = PaintedGround(
        image=imagem,
        colors={
            CLASSES[i].key: tuple(float(c) / 255.0 for c in cor)
            for i, cor in cores.items()
            if 0 <= i < len(CLASSES)
        },
        shares={
            CLASSES[i].key: float((labels == i).sum()) / total
            for i in range(len(CLASSES))
        },
    )
    log.info("Textura pintada: %s", resultado.summary())
    return resultado
