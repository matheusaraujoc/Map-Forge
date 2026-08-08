"""Modelo digital de elevacao a partir de tiles Terrarium.

Usa o mesmo transporte de tiles da imagem de satelite; o que muda e a leitura
dos pixels. No esquema Terrarium a altura vem codificada no RGB:

    altura_m = R * 256 + G + B / 256 - 32768

O conjunto (AWS Open Data, derivado de SRTM/NED e outras fontes publicas) chega
a z15, o que da algo em torno de 4 a 5 metros por pixel em latitudes medias -
resolucao de encosta e vale, nao de calcada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from ..core.geo import BBox
from .tiles import TileError, TileProvider, download_mosaic

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]

TERRARIUM = TileProvider(
    name="terrarium",
    label="AWS Terrain Tiles (Terrarium)",
    url="https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
    max_zoom=15,
    attribution="AWS Terrain Tiles - SRTM, NED e outras fontes publicas",
    extension="png",
)

ELEVATION_PROVIDERS: dict[str, TileProvider] = {"terrarium": TERRARIUM}


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """RGB (h, w, 3) uint8 -> altura em metros (h, w) float."""
    rgb = np.asarray(rgb, dtype=np.float64)
    return rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0


@dataclass
class ElevationGrid:
    """Malha de alturas georreferenciada pela bbox. Linha 0 = norte."""

    heights: np.ndarray  # (linhas, colunas) em metros
    bbox: BBox
    zoom: int = 0
    provider: str = "terrarium"
    attribution: str = ""

    @property
    def shape(self) -> tuple[int, int]:
        return self.heights.shape

    @property
    def min(self) -> float:
        return float(np.nanmin(self.heights))

    @property
    def max(self) -> float:
        return float(np.nanmax(self.heights))

    @property
    def meters_per_pixel(self) -> float:
        return self.bbox.width_m / max(self.heights.shape[1], 1)

    def sample(self, x, y) -> np.ndarray:
        """Altura bilinear em coordenadas locais (metros, X=leste, Y=norte)."""
        x = np.atleast_1d(np.asarray(x, dtype=np.float64))
        y = np.atleast_1d(np.asarray(y, dtype=np.float64))
        rows, cols = self.heights.shape
        half_w, half_h = self.bbox.width_m / 2.0, self.bbox.height_m / 2.0

        # Fora da borda, a amostra gruda no pixel de borda em vez de extrapolar.
        fx = np.clip((x + half_w) / (2 * half_w) * (cols - 1), 0, cols - 1)
        fy = np.clip((half_h - y) / (2 * half_h) * (rows - 1), 0, rows - 1)

        x0 = np.floor(fx).astype(np.int64)
        y0 = np.floor(fy).astype(np.int64)
        x1 = np.minimum(x0 + 1, cols - 1)
        y1 = np.minimum(y0 + 1, rows - 1)
        tx = fx - x0
        ty = fy - y0

        top = self.heights[y0, x0] * (1 - tx) + self.heights[y0, x1] * tx
        bottom = self.heights[y1, x0] * (1 - tx) + self.heights[y1, x1] * tx
        return top * (1 - ty) + bottom * ty

    def smoothed(self, passes: int = 1) -> "ElevationGrid":
        """Media 3x3 repetida: tira o degrau de quantizacao de 1/256 m."""
        heights = self.heights
        for _ in range(max(0, passes)):
            padded = np.pad(heights, 1, mode="edge")
            heights = (
                padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:]
                + padded[1:-1, :-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:]
                + padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:]
            ) / 9.0
        return ElevationGrid(
            heights=heights,
            bbox=self.bbox,
            zoom=self.zoom,
            provider=self.provider,
            attribution=self.attribution,
        )


def fetch_elevation(
    bbox: BBox,
    cache=None,
    zoom: Optional[int] = None,
    provider_name: str = "terrarium",
    progress: Optional[ProgressFn] = None,
    force: bool = False,
    smooth: int = 1,
) -> ElevationGrid:
    """Baixa o mosaico de elevacao da regiao e decodifica para metros."""
    provider = ELEVATION_PROVIDERS.get(provider_name)
    if provider is None:
        raise TileError(
            f"provedor de elevacao desconhecido: {provider_name!r}. "
            f"Disponiveis: {', '.join(ELEVATION_PROVIDERS)}"
        )

    image, actual_zoom = download_mosaic(
        bbox,
        provider,
        zoom=zoom,
        cache=cache,
        max_pixels=2048,
        progress=progress,
        force=force,
        probe=False,  # tile de elevacao nao tem placeholder cinza
    )

    heights = decode_terrarium(np.asarray(image.convert("RGB"), dtype=np.uint8))
    # O oceano vem como -32768 exato em alguns tiles; trata como nivel do mar.
    heights[heights < -400.0] = 0.0

    grid = ElevationGrid(
        heights=heights,
        bbox=bbox,
        zoom=actual_zoom,
        provider=provider.name,
        attribution=provider.attribution,
    )
    if smooth:
        grid = grid.smoothed(smooth)

    if progress:
        progress(f"relevo {grid.min:.0f}-{grid.max:.0f} m ({grid.shape[1]}x{grid.shape[0]})", 0.9)
    log.info(
        "Elevacao z%d: %dx%d, %.0f a %.0f m",
        grid.zoom, grid.shape[1], grid.shape[0], grid.min, grid.max,
    )
    return grid
