"""Redesenho do chao: um desenho, nao uma textura.

O resto da cena e low-poly de face chapada. Um chao que tenta imitar textura
real briga com isso - e foi o que as tentativas anteriores fizeram, cada uma do
seu jeito:

- modular a foto com ruido: continua sendo a foto borrada, so que com granulado;
- pincelar copa, seixo e capim: ganha estrutura, mas fica agitado e realista
  demais para um mapa desenhado;
- acrescentar manchado fino e marcas esparsas para "tirar o aspecto de
  plastico": deixou o chao sujo e, junto com a antisserrilha analitica que veio
  no mesmo pacote, piorou o conjunto. Foi revertido.

O que este modulo faz e outra coisa: **repinta o chao como um desenho**. Poucas
cores chapadas por cobertura, manchas grandes, contorno curvo e limpo. Sem grao,
sem ruido de alta frequencia, sem imitacao de material. A alta resolucao serve
para a *borda* ficar lisa, nao para caber detalhe - o mesmo motivo pelo qual um
desenho vetorial e nitido em qualquer tamanho.

O que continua vindo do dado e o que importa: **onde** esta cada cobertura, e
**qual e a cor dela naquele lugar**. Areia rosada sai rosada, mata de varzea sai
escura, capim de fim de estacao sai ocre. Coerente com a realidade, sem tentar
ser fotografia.

Tres decisoes fazem o desenho parecer desenho:

1. **A fronteira e suavizada antes de virar pixel.** Cada cobertura vira um
   campo de pertinencia borrado, e o pixel recebe a classe de maior pertinencia.
   Isso troca o serrilhado do classificador por curva organica, e ainda garante
   que o resultado continue sendo uma particao - sem vao nem sobreposicao.
2. **O tom varia em manchas grandes, nao por pixel.** Um campo de ruido de
   frequencia baixa escolhe qual dos tons medidos cada regiao recebe. Da a
   variacao que um desenho a mao teria, sem virar chuvisco.
3. **O desenho e feito no dobro da resolucao e reduzido no fim.** A fronteira
   nasce de um `argmax`, que e decisao binaria por pixel; reduzir com
   reamostragem e o que faz a media dos dois lados na borda.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .segment import CLASSES, INDEX, classify

log = logging.getLogger(__name__)

DEFAULT_TARGET_MPP = 0.22
MAX_PX = 4096

# O desenho e feito no dobro da resolucao final e reduzido no fim: e a unica
# antisserrilha que existe aqui, porque `argmax` nao tem meio-termo.
SUPERSAMPLE = 2

# Raio de suavizacao da fronteira entre coberturas, em metros. Abaixo disso o
# contorno guarda o degrau do classificador; muito acima, uma mata estreita
# desaparece dentro do vizinho.
BOUNDARY_SMOOTH_M = 6.0

# Tamanho tipico de uma mancha de tom, em metros.
#
# Calibrado olhando o resultado: com 26 m e duas oitavas o chao virou
# **camuflagem** - manchas pequenas demais, em quantidade demais. Mancha grande
# le como pincelada; mancha pequena le como padrao militar. Uma oitava so, e
# larga.
PATCH_M = 75.0

# Largura da transicao entre dois tons vizinhos, como fracao do degrau.
#
# 0 devolve o chapado puro, que ficou duro demais; 1 vira degrade e perde o
# desenho. O que se quer e a maior parte da area chapada com uma faixa curta de
# passagem - como aquarela que encosta.
TONE_BLEND = 0.34

# Quantos tons cada cobertura usa. Poucos de proposito - e desenho chapado.
TONES = {
    "mata": 3,
    "grama": 2,
    "solo": 2,
    "via": 2,
    "agua": 2,
    "sombra": 2,
}
DEFAULT_TONES = 2

# Faixa de percentil de onde os tons saem. Estreita: num desenho os tons de um
# mesmo material sao vizinhos, nao opostos. Com 0,15-0,85 o contraste dentro da
# mata competia com o contraste entre mata e solo, e a leitura se perdia.
TONE_RANGE = (0.32, 0.68)

# Coberturas que a malha 3D ja desenha: herdam a vizinhanca.
COVERED_BY_MESH = ("telha", "laje", "telhado_escuro", "via", "agua")
BORROWS_NEIGHBOUR = ("sombra", "desconhecido")


@dataclass
class RedrawnGround:
    image: object  # PIL.Image
    tones: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)

    def summary(self) -> str:
        partes = [f"{k} {len(v)}" for k, v in sorted(self.tones.items())]
        return "tons por cobertura: " + ", ".join(partes)


def _class_tones(array: np.ndarray, labels: np.ndarray, indice: int, n: int):
    """Os `n` tons de uma cobertura, tirados dos percentis dela na foto.

    Percentis, e nao media nem k-means: o que interessa e a *faixa* real daquela
    cobertura naquele lugar - do verde mais claro ao mais escuro que a mata tem
    ali - e percentil de luminancia entrega isso ordenado, sem iteracao.
    """
    pixels = array[labels == indice]
    if len(pixels) < 40:
        return []
    if len(pixels) > 60_000:
        pixels = pixels[:: len(pixels) // 60_000 + 1]

    lum = pixels @ np.array([0.2126, 0.7152, 0.0722])
    pixels = pixels[np.argsort(lum)]
    # Evita os extremos: o percentil 0 e sombra dura, o 100 e reflexo estourado.
    cortes = np.linspace(*TONE_RANGE, max(n, 1))
    return [pixels[int(f * (len(pixels) - 1))] for f in cortes]


def _smooth_partition(labels: np.ndarray, presentes, sigma_px: float, escala: float, forma):
    """Fronteiras curvas em vez de serrilhado, ja na resolucao de trabalho.

    O truque e nao suavizar a imagem de rotulos (que nao tem ordem: a media
    entre 'agua' e 'telha' nao significa nada), e sim um campo de pertinencia
    por cobertura. O pixel fica com a cobertura de maior pertinencia - o que
    garante particao, sem vao nem sobreposicao.

    O borramento e feito na resolucao da foto, que e barato, e a ampliacao para
    a resolucao de trabalho e bilinear: e ai que a curva nasce lisa.
    """
    from scipy import ndimage

    melhor = None
    # int16 e nao int64: na resolucao de trabalho de uma regiao media isso e a
    # diferenca entre 134 MB e 536 MB, e os indices de cobertura sao uma duzia.
    vencedor = np.zeros(forma, dtype=np.int16)
    for indice in presentes:
        campo = ndimage.gaussian_filter((labels == indice).astype(np.float32), sigma_px)
        # `order=3` (spline cubica) e nao bilinear.
        #
        # A ampliacao bilinear e linear por trecho: entre quatro amostras ela
        # desenha um losango, e o `argmax` logo depois transforma esses losangos
        # em **degraus diagonais** na fronteira - o serrilhado que aparece na
        # borda de cada mancha. A cubica passa uma curva pelas amostras e a
        # fronteira sai lisa, com a mesma memoria.
        grande = ndimage.zoom(campo, escala, order=3)
        if grande.shape != forma:  # borda de arredondamento
            recorte = np.zeros(forma, dtype=np.float32)
            alt = min(forma[0], grande.shape[0])
            larg = min(forma[1], grande.shape[1])
            recorte[:alt, :larg] = grande[:alt, :larg]
            grande = recorte
        if melhor is None:
            melhor = grande
            vencedor[:] = indice
        else:
            troca = grande > melhor
            melhor = np.where(troca, grande, melhor)
            vencedor[troca] = indice
    return vencedor


def _patch_field(forma, escala_px: float, degraus: int, rng) -> np.ndarray:
    """Campo de manchas grandes, em nivel continuo.

    Quem quantiza e quem pinta: devolver o nivel continuo deixa uma faixa de
    passagem entre os tons em vez de um degrau seco.
    """
    from scipy import ndimage

    baixa = (
        max(int(round(forma[0] / max(escala_px, 2))) + 2, 2),
        max(int(round(forma[1] / max(escala_px, 2))) + 2, 2),
    )
    grade = rng.random(baixa).astype(np.float32)
    ampliada = ndimage.zoom(
        grade, (forma[0] / baixa[0], forma[1] / baixa[1]), order=3, mode="reflect"
    )
    campo = np.zeros(forma, dtype=np.float32)
    alt = min(forma[0], ampliada.shape[0])
    larg = min(forma[1], ampliada.shape[1])
    campo[:alt, :larg] = ampliada[:alt, :larg]

    baixo, alto = campo.min(), campo.max()
    if alto - baixo < 1e-6:
        return np.zeros(forma, dtype=np.float32)
    return ((campo - baixo) / (alto - baixo) * (degraus - 1)).astype(np.float32)


def _coverage(particao: np.ndarray, indice: int, fator: int, forma_final) -> np.ndarray:
    """Fracao de cada pixel final ocupada por uma cobertura.

    E a antisserrilha: no miolo da mancha a fracao e 1, fora e 0, e na borda vale
    quantos dos `fator x fator` sub-pixels caem dentro. Somar as coberturas
    ponderadas da exatamente o que a reducao da imagem no dobro daria.
    """
    alt, larg = forma_final
    recorte = particao[: alt * fator, : larg * fator] == indice
    if fator == 1:
        return recorte.astype(np.float32)
    return recorte.reshape(alt, fator, larg, fator).mean(axis=(1, 3), dtype=np.float32)


def _blend_tones(nivel: np.ndarray, tons, suavidade: float) -> np.ndarray:
    """Converte o nivel continuo em cor, chapando o meio e suavizando a beira.

    A fracao entre dois tons passa por um degrau suave estreito: fica 0 na maior
    parte, 1 na maior parte, e varia so perto da troca. E o que da "transicao"
    sem virar degrade.
    """
    tabela = np.array(tons, dtype=np.float32)
    n = len(tons)
    if n == 1:
        return np.broadcast_to(tabela[0], (*nivel.shape, 3)).copy()

    baixo = np.clip(np.floor(nivel), 0, n - 1).astype(np.int64)
    alto = np.clip(baixo + 1, 0, n - 1)
    fracao = np.clip(nivel - baixo, 0.0, 1.0).astype(np.float32)

    if suavidade <= 1e-3:
        fracao = (fracao > 0.5).astype(np.float32)
    else:
        # Estica a fracao para fora da faixa de transicao e recorta: o miolo
        # satura em 0 ou 1 (chapado) e so a beira interpola.
        fracao = np.clip((fracao - 0.5) / suavidade + 0.5, 0.0, 1.0)
        fracao = fracao * fracao * (3.0 - 2.0 * fracao)  # smoothstep

    return tabela[baixo] * (1.0 - fracao)[..., None] + tabela[alto] * fracao[..., None]


def redraw_ground(
    geo,
    map_data=None,
    target_mpp: float = DEFAULT_TARGET_MPP,
    seed: int = 1,
    max_size: int = MAX_PX,
) -> RedrawnGround:
    """Repinta o chao como desenho chapado, com as cores medidas na foto."""
    from PIL import Image

    from .painted import _fill_from_neighbours

    array = geo.array.astype(np.float64)
    mpp = geo.meters_per_pixel

    seg = classify(geo, map_data)
    labels = seg.labels.astype(np.int64)

    ocultar = np.zeros(labels.shape, dtype=bool)
    for chave in COVERED_BY_MESH + BORROWS_NEIGHBOUR:
        ocultar |= labels == INDEX[chave]
    labels_chao = _fill_from_neighbours(labels, ocultar, mpp)

    # A resolucao final e limitada pelo teto; o desenho e feito no dobro dela.
    final_px = min(
        int(round(max(labels.shape) * mpp / max(target_mpp, 0.05))), max_size
    )
    # Sem `max(final_px, foto)`.
    #
    # Essa linha existia para a textura nunca ficar abaixo da resolucao da foto,
    # e o efeito colateral era anular o teto: numa regiao de 6 km a foto tem
    # 5.042 px, o dobro disso e 10.084, e o desenho passava a mexer com **101
    # milhoes de pixels** - foi o que levou uma geracao a 715 s. O desenho e de
    # formas chapadas; reduzir a classificacao custa pouco, e o teto tem de
    # valer.
    trabalho_px = final_px * SUPERSAMPLE

    escala = trabalho_px / max(labels.shape)
    alvo_mpp = mpp / (final_px / max(labels.shape))
    final_alt = max(int(round(labels.shape[0] * final_px / max(labels.shape))), 1)
    # A forma de trabalho e um multiplo exato da final, senao a media de 2x2 nao
    # casa com os pixels que ela deveria representar.
    forma = (final_alt * SUPERSAMPLE, final_px * SUPERSAMPLE)

    presentes = [int(i) for i in np.unique(labels_chao)]
    grandes = _smooth_partition(
        labels_chao, presentes, BOUNDARY_SMOOTH_M / max(mpp, 0.05), escala, forma
    )

    # A cor sai na resolucao FINAL; do dobro so vem a cobertura.
    #
    # Pintar tudo no dobro e reduzir no fim e o caminho obvio, e era o que estava
    # aqui - mas custa quatro vezes tudo: a mancha, a mistura de tons e a imagem
    # inteira nasciam com 67 milhoes de pixels para virarem 17. O supersampling
    # so e necessario para a **fronteira**; a cor dentro de cada cobertura varia
    # devagar e nao ganha nada com isso.
    #
    # Entao a particao continua no dobro, mas dela se extrai apenas a *fracao de
    # cobertura* de cada classe em cada pixel final (media de 2x2). O resultado e
    # identico ao supersampling - a media dos quatro sub-pixels e exatamente o
    # que a reducao faria - com um quarto do trabalho.
    fator = max(int(round(forma[0] / final_alt)), 1)
    forma_final = (final_alt, final_px)

    rng = np.random.default_rng(seed)
    saida = np.zeros((*forma_final, 3), dtype=np.float32)
    usados: dict[str, list] = {}
    final_mpp = mpp / (final_px / max(labels.shape))

    # O campo de manchas e o mesmo para todas as coberturas - so o numero de
    # degraus muda. Gerar um por cobertura custava um quarto do tempo do desenho.
    manchas = _patch_field(forma_final, PATCH_M / final_mpp, 2, rng)
    pico = float(manchas.max())
    if pico > 1e-6:
        manchas /= pico  # normaliza para 0..1

    for indice in presentes:
        if not (0 <= indice < len(CLASSES)):
            continue
        chave = CLASSES[indice].key
        n = TONES.get(chave, DEFAULT_TONES)
        # A cor vem de onde a cobertura foi *observada*, nao de onde foi herdada.
        tons = _class_tones(array, np.where(ocultar, -1, labels), indice, n)
        if not tons:
            continue

        cobertura = _coverage(grandes, indice, fator, forma_final)
        if not cobertura.any():
            continue
        usados[chave] = [tuple(int(c) for c in t) for t in tons]

        # Pinta so onde a cobertura aparece, inclusive parcialmente na borda.
        onde = np.nonzero(cobertura > 0.0)
        nivel = manchas[onde] * (len(tons) - 1)
        cor = _blend_tones(nivel, tons, TONE_BLEND)
        saida[onde] += cor * cobertura[onde][:, None]

    imagem = Image.fromarray(np.clip(saida, 0, 255).astype(np.uint8))

    resultado = RedrawnGround(image=imagem, tones=usados)
    log.info(
        "Chao desenhado: %dx%d px (%.2f m/px) | %s",
        imagem.width, imagem.height, alvo_mpp, resultado.summary(),
    )
    return resultado
