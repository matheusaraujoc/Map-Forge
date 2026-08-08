"""Amostragem de cores a partir da imagem de satelite.

A imagem entra georreferenciada pela bbox; as coordenadas locais em metros viram
pixels por mapeamento linear. Dentro de uma cidade o desvio entre o Web Mercator
da imagem e a projecao equirretangular local fica abaixo de um pixel, entao nao
compensa reprojetar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..core.geo import BBox

log = logging.getLogger(__name__)

Color = tuple[float, float, float]

# Luminancia aceitavel para uma cor amostrada: fora disso a cidade fica ou
# lavada ou preta, porque telhado sob sombra e telhado sob sol direto chegam
# com varias paradas de diferenca.
MIN_LUMA = 0.18
MAX_LUMA = 0.82


@dataclass
class GeoImage:
    """Imagem raster georreferenciada por uma bbox."""

    image: object  # PIL.Image
    bbox: BBox
    zoom: int = 0
    provider: str = ""
    attribution: str = ""

    def __post_init__(self) -> None:
        self._array: Optional[np.ndarray] = None

    @property
    def array(self) -> np.ndarray:
        """(altura, largura, 3) em uint8. Linha 0 = norte."""
        if self._array is None:
            self._array = np.asarray(self.image.convert("RGB"), dtype=np.uint8)
        return self._array

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size  # (largura, altura)

    @property
    def meters_per_pixel(self) -> float:
        return self.bbox.width_m / max(self.image.size[0], 1)

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """Metros locais (X=leste, Y=norte) -> (coluna, linha)."""
        width_px, height_px = self.image.size
        half_w, half_h = self.bbox.width_m / 2.0, self.bbox.height_m / 2.0
        col = (x + half_w) / (2 * half_w) * (width_px - 1)
        row = (half_h - y) / (2 * half_h) * (height_px - 1)
        return col, row

    def window(self, minx: float, miny: float, maxx: float, maxy: float):
        """Recorte da imagem correspondente a um retangulo em metros locais."""
        col0, row1 = self.to_pixel(minx, miny)  # miny e o sul -> linha maior
        col1, row0 = self.to_pixel(maxx, maxy)
        height_px, width_px = self.array.shape[:2]

        c0 = max(int(np.floor(min(col0, col1))), 0)
        c1 = min(int(np.ceil(max(col0, col1))) + 1, width_px)
        r0 = max(int(np.floor(min(row0, row1))), 0)
        r1 = min(int(np.ceil(max(row0, row1))) + 1, height_px)
        if c1 <= c0 or r1 <= r0:
            return None
        return self.array[r0:r1, c0:c1]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.image.save(path)
        return path


# ------------------------------------------------------------------ utilidades


def _median_color(window: np.ndarray, max_samples: int = 4096) -> Optional[Color]:
    """Mediana das cores do recorte. Mediana e nao media: resiste a sombra,
    caixa d'agua e antena, que sao minoria de pixels mas puxam a media."""
    if window is None or window.size == 0:
        return None
    flat = window.reshape(-1, 3)
    if len(flat) > max_samples:
        step = len(flat) // max_samples + 1
        flat = flat[::step]
    median = np.median(flat, axis=0) / 255.0
    return (float(median[0]), float(median[1]), float(median[2]))


def _luma(color: Color) -> float:
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def harmonize(color: Color, saturation: float = 1.25) -> Color:
    """Traz a cor amostrada para uma faixa utilizavel e reforca o croma.

    Sem isso o resultado fica cinza-lama: a imagem de satelite e naturalmente
    dessaturada e cheia de sombra.
    """
    mean = sum(color) / 3.0
    boosted = [mean + (c - mean) * saturation for c in color]

    luma = _luma(tuple(boosted))  # type: ignore[arg-type]
    if luma < 1e-4:
        return (MIN_LUMA, MIN_LUMA, MIN_LUMA)
    if luma < MIN_LUMA:
        boosted = [c * (MIN_LUMA / luma) for c in boosted]
    elif luma > MAX_LUMA:
        boosted = [c * (MAX_LUMA / luma) for c in boosted]

    return tuple(float(min(1.0, max(0.0, c))) for c in boosted)  # type: ignore[return-value]


def blend(style_color: Color, sampled: Color, amount: float) -> Color:
    """`amount` = 0 usa so o estilo, 1 usa so o satelite."""
    amount = min(1.0, max(0.0, amount))
    return tuple(  # type: ignore[return-value]
        style_color[i] + (sampled[i] - style_color[i]) * amount for i in range(3)
    )


# -------------------------------------------------------------- amostradores


def sample_area_color(geo: GeoImage, geometry, inset: float = 0.0) -> Optional[Color]:
    """Cor representativa de um poligono (usa a janela do seu retangulo)."""
    if geometry is None or geometry.is_empty:
        return None
    shape = geometry
    if inset > 0:
        shrunk = shape.buffer(-inset)
        if not shrunk.is_empty:
            shape = shrunk
    minx, miny, maxx, maxy = shape.bounds
    return _median_color(geo.window(minx, miny, maxx, maxy))


def sample_roof_colors(
    geo: GeoImage,
    buildings,
    inset: float = 1.2,
    saturation: float = 1.2,
) -> dict[int, Color]:
    """Cor de telhado por edificio, direto da imagem.

    Recua o contorno antes de amostrar para nao pegar calcada e sombra da
    fachada junto - a sombra e o que mais suja o resultado.
    """
    colors: dict[int, Color] = {}
    for building in buildings:
        footprint = building.footprint
        if footprint is None or footprint.is_empty:
            continue
        shape = footprint
        if inset > 0 and footprint.area > 40:
            shrunk = footprint.buffer(-inset)
            if not shrunk.is_empty:
                shape = shrunk
        minx, miny, maxx, maxy = shape.bounds
        sampled = _median_color(geo.window(minx, miny, maxx, maxy))
        if sampled is not None:
            colors[building.osm_id] = harmonize(sampled, saturation)
    return colors


def build_ground_texture(
    geo: GeoImage,
    brightness: float = 1.0,
    saturation: float = 1.0,
    max_size: int = 4096,
):
    """Prepara a imagem para virar textura do terreno.

    Ajustes leves de brilho e croma sao o "complemento artificial": mantem a
    leitura do lugar real sem que a foto crua brigue com a paleta do estilo.
    """
    from PIL import Image, ImageEnhance

    image = geo.image.convert("RGB")
    if max(image.size) > max_size:
        scale = max_size / max(image.size)
        image = image.resize(
            (max(int(image.width * scale), 1), max(int(image.height * scale), 1)),
            Image.LANCZOS,
        )
    if abs(saturation - 1.0) > 1e-3:
        image = ImageEnhance.Color(image).enhance(saturation)
    if abs(brightness - 1.0) > 1e-3:
        image = ImageEnhance.Brightness(image).enhance(brightness)
    return image
