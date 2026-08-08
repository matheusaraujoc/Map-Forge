"""Classificacao da imagem de satelite em classes de cobertura.

Existe para tornar a percepcao *observavel*. Ate aqui o detector de edificios
era uma caixa preta: entrava foto, saia poligono, e quando errava nao dava para
saber se o erro estava na cor, na forma ou no filtro. Este modulo desenha o que
o sistema enxerga, classe por classe, com uma paleta fixa e uma legenda.

A classificacao trabalha em HSV, nao em RGB cru. O tom (matiz) separa telha de
vegetacao de forma estavel mesmo com a iluminacao mudando; a saturacao e o
valor separam telha de solo exposto, que e o caso dificil no Brasil, onde a
terra e vermelha e ocupa a mesma familia de matiz da ceramica.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverClass:
    """Uma classe de cobertura do solo, com a cor que a representa no desenho."""

    key: str
    label: str
    color: tuple[int, int, int]


# A paleta e de leitura, nao de estilo: cores chapadas e bem separadas entre si.
CLASSES: tuple[CoverClass, ...] = (
    CoverClass("desconhecido", "Nao classificado", (245, 245, 245)),
    CoverClass("telha", "Telha ceramica", (214, 96, 56)),
    CoverClass("laje", "Laje / fibrocimento claro", (238, 232, 216)),
    CoverClass("telhado_escuro", "Telhado escuro / metalico", (96, 100, 110)),
    CoverClass("mata", "Vegetacao densa", (34, 102, 48)),
    CoverClass("grama", "Vegetacao rasteira", (124, 176, 84)),
    CoverClass("solo", "Solo exposto", (176, 132, 96)),
    CoverClass("agua", "Agua", (54, 116, 168)),
    CoverClass("sombra", "Sombra", (58, 58, 68)),
    CoverClass("via", "Via (do mapa vetorial)", (86, 88, 96)),
)

INDEX = {c.key: i for i, c in enumerate(CLASSES)}
PALETTE = np.array([c.color for c in CLASSES], dtype=np.uint8)


def rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RGB em 0..1 -> matiz (graus), saturacao e valor, tudo vetorizado."""
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    high = np.maximum(np.maximum(r, g), b)
    low = np.minimum(np.minimum(r, g), b)
    delta = high - low

    hue = np.zeros_like(high)
    seguro = delta > 1e-9
    # Cada ramo cobre o setor em que um canal e o maximo.
    idx = seguro & (high == r)
    hue[idx] = ((g[idx] - b[idx]) / delta[idx]) % 6
    idx = seguro & (high == g)
    hue[idx] = (b[idx] - r[idx]) / delta[idx] + 2
    idx = seguro & (high == b)
    hue[idx] = (r[idx] - g[idx]) / delta[idx] + 4
    hue *= 60.0

    saturation = np.zeros_like(high)
    saturation[high > 1e-9] = delta[high > 1e-9] / high[high > 1e-9]
    return hue, saturation, high


def local_variation(gray: np.ndarray, radius_px: int) -> np.ndarray:
    """Desvio padrao local: telhado e liso, solo e mato sao texturados.

    E o discriminador que falta ao tom puro. Telha e terra vermelha ocupam a
    mesma faixa de matiz; o que as separa e o telhado ser uma superficie
    fabricada, homogenea, com borda reta.
    """
    from scipy import ndimage

    size = max(2 * radius_px + 1, 3)
    media = ndimage.uniform_filter(gray, size=size)
    media_quadrado = ndimage.uniform_filter(gray * gray, size=size)
    return np.sqrt(np.maximum(media_quadrado - media * media, 0.0))


@dataclass
class Segmentation:
    """Resultado da classificacao."""

    labels: np.ndarray  # (h, w) com indices em CLASSES
    meters_per_pixel: float

    def counts(self) -> dict[str, int]:
        total = self.labels.size
        saida = {}
        for i, cover in enumerate(CLASSES):
            n = int((self.labels == i).sum())
            if n:
                saida[cover.key] = n
        return saida

    def fractions(self) -> dict[str, float]:
        total = max(self.labels.size, 1)
        return {k: v / total for k, v in self.counts().items()}

    def mask(self, *keys: str) -> np.ndarray:
        alvo = [INDEX[k] for k in keys if k in INDEX]
        if not alvo:
            return np.zeros(self.labels.shape, dtype=bool)
        return np.isin(self.labels, alvo)

    def to_rgb(self) -> np.ndarray:
        return PALETTE[self.labels]


def classify(geo, map_data=None, road_mask: Optional[np.ndarray] = None) -> Segmentation:
    """Classifica cada pixel da imagem numa classe de cobertura.

    Quando `map_data` e informado, as vias entram como classe propria vinda do
    mapa vetorial - elas ja sao confiaveis e nao precisam ser adivinhadas.
    """
    rgb = geo.array.astype(np.float64) / 255.0
    hue, sat, val = rgb_to_hsv(rgb)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    gray = 0.2126 * r + 0.7152 * g + 0.0722 * b

    mpp = geo.meters_per_pixel
    textura = local_variation(gray, radius_px=max(int(round(1.2 / max(mpp, 0.05))), 1))

    labels = np.zeros(gray.shape, dtype=np.uint8)  # 0 = desconhecido

    # --- agua: escura, azulada, lisa ---
    agua = (b > r + 0.02) & (sat > 0.10) & (val < 0.55) & (textura < 0.05)
    agua |= (hue > 170) & (hue < 260) & (sat > 0.18) & (val < 0.6)

    # --- vegetacao pelo excesso de verde, que resiste a variacao de luz ---
    excesso = 2.0 * g - r - b
    vegetacao = excesso > 0.03
    mata = vegetacao & (val < 0.42)
    grama = vegetacao & ~mata

    # --- sombra: escura e dessaturada, sem ser agua ---
    sombra = (val < 0.20) & ~agua & ~vegetacao

    # --- familia avermelhada: telha ceramica e solo exposto disputam aqui ---
    avermelhado = ((hue < 45) | (hue > 340)) & (r > b + 0.03)
    # Telha e mais saturada, mais clara e MUITO mais lisa que terra batida.
    telha = avermelhado & (sat > 0.22) & (val > 0.18) & (val < 0.88) & (textura < 0.085)
    solo = avermelhado & ~telha

    # --- superficies claras e dessaturadas: laje, fibrocimento, areia ---
    claro = (val > 0.50) & (sat < 0.20) & ~vegetacao
    # Areia tem textura; laje nao.
    laje = claro & (textura < 0.075)
    solo |= claro & ~laje

    # --- telhado escuro / metalico: cinza medio, liso, sem ser sombra ---
    escuro = (val >= 0.20) & (val < 0.48) & (sat < 0.22) & ~vegetacao & ~agua
    telhado_escuro = escuro & (textura < 0.06)

    # A ordem embute a prioridade: o que e mais especifico sobrescreve.
    labels[solo] = INDEX["solo"]
    labels[grama] = INDEX["grama"]
    labels[mata] = INDEX["mata"]
    labels[telhado_escuro] = INDEX["telhado_escuro"]
    labels[laje] = INDEX["laje"]
    labels[telha] = INDEX["telha"]
    labels[agua] = INDEX["agua"]
    labels[sombra] = INDEX["sombra"]

    # --- vias e agua: vem do mapa, nao da cor ---
    #
    # Rio de planicie carrega sedimento e aparece marrom ou esverdeado, nao
    # azul: a regra de cor o classificava como vegetacao densa. Onde o mapa
    # vetorial sabe, ele manda.
    if map_data is not None:
        if road_mask is None:
            road_mask = _road_mask(geo, map_data)
        agua_mapa = _water_mask(geo, map_data)
        if agua_mapa is not None and agua_mapa.any():
            labels[agua_mapa] = INDEX["agua"]
    if road_mask is not None:
        labels[road_mask] = INDEX["via"]

    return Segmentation(labels=labels, meters_per_pixel=mpp)


def _water_mask(geo, map_data) -> Optional[np.ndarray]:
    """Corpos d'agua do OSM: lagos, reservatorios e o leito dos rios."""
    from .detect import _rasterize

    geoms = [w.geometry for w in map_data.waters if w.geometry is not None]
    geoms += [
        river.centerline.buffer(max(river.width, 3.0) / 2.0)
        for river in map_data.rivers
        if river.centerline is not None
    ]
    if not geoms:
        return None
    return _rasterize(geo, geoms)


def _road_mask(geo, map_data) -> np.ndarray:
    from ..generation.roads import road_half_width
    from .detect import _rasterize

    geoms = [
        road.centerline.buffer(road_half_width(road))
        for road in map_data.roads
        if road.centerline is not None
    ]
    if not geoms:
        return np.zeros(geo.array.shape[:2], dtype=bool)
    return _rasterize(geo, geoms)


# ------------------------------------------------------------------ desenho


def render(
    segmentation: Segmentation,
    path: Optional[str | Path] = None,
    geo=None,
    side_by_side: bool = True,
    polygons=None,
):
    """Grava o desenho classificado, opcionalmente ao lado da foto original.

    `polygons` (em metros locais) sao desenhados por cima em contorno, para
    comparar o que o detector extraiu com o que a classificacao enxergou.
    """
    from PIL import Image, ImageDraw

    classificado = Image.fromarray(segmentation.to_rgb())

    if polygons and geo is not None:
        desenho = ImageDraw.Draw(classificado)
        for poly in polygons:
            anel = getattr(poly, "exterior", None)
            if anel is None:
                continue
            pixels = [geo.to_pixel(x, y) for x, y in anel.coords]
            if len(pixels) >= 3:
                desenho.line(pixels + [pixels[0]], fill=(20, 20, 20), width=2)

    legenda = _legend(classificado.width, segmentation)

    if side_by_side and geo is not None:
        original = geo.image.convert("RGB")
        largura = original.width + classificado.width + 12
        altura = max(original.height, classificado.height) + legenda.height
        folha = Image.new("RGB", (largura, altura), (255, 255, 255))
        folha.paste(original, (0, 0))
        folha.paste(classificado, (original.width + 12, 0))
        folha.paste(legenda, (0, max(original.height, classificado.height)))
    else:
        # A folha acompanha a legenda quando a imagem e estreita, senao a
        # legenda sai cortada justamente nos casos pequenos.
        largura = max(classificado.width, legenda.width)
        folha = Image.new(
            "RGB", (largura, classificado.height + legenda.height), (255, 255, 255)
        )
        folha.paste(classificado, (0, 0))
        folha.paste(legenda, (0, classificado.height))

    if path is None:
        return folha
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    folha.save(path)
    return path


def _legend(width: int, segmentation: Segmentation):
    """Faixa com as classes presentes e quanto cada uma ocupa."""
    from PIL import Image, ImageDraw

    fracoes = segmentation.fractions()
    presentes = [c for c in CLASSES if c.key in fracoes and fracoes[c.key] > 0.0005]
    linhas = max((len(presentes) + 2) // 3, 1)
    altura = 12 + linhas * 20

    faixa = Image.new("RGB", (max(width, 420), altura), (255, 255, 255))
    desenho = ImageDraw.Draw(faixa)
    coluna_larg = faixa.width // 3

    for i, cover in enumerate(presentes):
        col, lin = i % 3, i // 3
        x = 8 + col * coluna_larg
        y = 8 + lin * 20
        desenho.rectangle([x, y, x + 14, y + 14], fill=cover.color, outline=(120, 120, 120))
        desenho.text(
            (x + 20, y + 2),
            f"{cover.label}  {fracoes[cover.key] * 100:.1f}%",
            fill=(30, 30, 30),
        )
    return faixa
