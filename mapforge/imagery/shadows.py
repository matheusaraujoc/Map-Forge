"""Estimativa de altura de edificios pela sombra na imagem de satelite.

A ideia e antiga em sensoriamento remoto e nao precisa de IA: a sombra projetada
tem comprimento proporcional a altura, `h = L * tan(elevacao_solar)`.

O problema pratico e que nao sabemos a data nem a hora da captura do tile, entao
nao da para calcular a elevacao solar. A saida e *auto-calibrar*: a direcao da
sombra sai da propria imagem (a direcao em que a vizinhanca dos predios escurece)
e a escala sai dos predios que ja tem altura no OSM. Onde nao houver nenhum
predio com altura conhecida, a estimativa continua relativa e e marcada como de
baixa confianca.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Resolucao minima util: acima disso a sombra de um sobrado tem poucos pixels.
MAX_METERS_PER_PIXEL = 1.2

# Quanto a sombra e mais escura que o entorno iluminado.
SHADOW_RATIO = 0.66

MAX_SHADOW_M = 90.0  # ate onde procurar (predio de ~60 andares ao entardecer)
MIN_HEIGHT_M = 2.5
MAX_HEIGHT_M = 250.0


@dataclass
class ShadowResult:
    """Alturas estimadas e o que foi usado para chegar nelas."""

    heights: dict[int, float] = field(default_factory=dict)
    shadow_azimuth: float = 0.0  # graus, direcao para onde a sombra aponta
    scale: float = 0.0  # h = scale * comprimento_da_sombra
    calibration: int = 0  # quantos predios com altura conhecida calibraram
    measured: int = 0  # quantos tiveram sombra mensuravel
    confidence: str = "baixa"
    note: str = ""

    def summary(self) -> dict:
        return {
            "shadow_azimuth": round(self.shadow_azimuth, 1),
            "scale": round(self.scale, 3),
            "sun_elevation": round(float(np.degrees(np.arctan(self.scale))), 1)
            if self.scale > 0
            else None,
            "calibration": self.calibration,
            "measured": self.measured,
            "estimated": len(self.heights),
            "confidence": self.confidence,
        }


# --------------------------------------------------------------------- apoio


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return (
        0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    ) / 255.0


def _footprint_mask(geo, buildings) -> np.ndarray:
    """Mascara booleana dos telhados, para descartar sombra que cai em outro predio."""
    from PIL import Image, ImageDraw

    width, height = geo.size
    canvas = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(canvas)
    for building in buildings:
        poly = building.footprint
        if poly is None or poly.is_empty:
            continue
        pixels = [geo.to_pixel(x, y) for x, y in poly.exterior.coords]
        if len(pixels) >= 3:
            draw.polygon(pixels, fill=255)
    return np.asarray(canvas, dtype=np.uint8) > 0


def _boundary_pixels(geo, poly, spacing_m: float) -> np.ndarray:
    """Pontos ao longo do contorno, em pixels, espacados ~spacing_m metros."""
    perimeter = poly.exterior.length
    count = int(max(perimeter / max(spacing_m, 0.5), 4))
    count = min(count, 160)
    points = [poly.exterior.interpolate(i / count, normalized=True) for i in range(count)]
    return np.array([geo.to_pixel(p.x, p.y) for p in points], dtype=np.float64)


def _sample(image: np.ndarray, cols: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """Amostra por vizinho mais proximo, com clamp na borda."""
    h, w = image.shape[:2]
    c = np.clip(cols.astype(np.int64), 0, w - 1)
    r = np.clip(rows.astype(np.int64), 0, h - 1)
    return image[r, c]


def estimate_shadow_azimuth(
    luma: np.ndarray,
    geo,
    buildings,
    inside: np.ndarray,
    samples: int = 220,
) -> float:
    """Direcao em que a vizinhanca dos predios escurece = direcao da sombra.

    Varre 36 direcoes, mede a luminancia media num anel a alguns metros de cada
    contorno e fica com a direcao mais escura. Depois refina em passo de 2 graus.
    """
    rng = np.random.default_rng(0)
    chosen = list(buildings)
    if len(chosen) > samples:
        idx = rng.choice(len(chosen), samples, replace=False)
        chosen = [chosen[i] for i in idx]

    centers = []
    radii = []
    for building in chosen:
        poly = building.footprint
        if poly is None or poly.is_empty or poly.area < 25:
            continue
        point = poly.representative_point()
        centers.append(geo.to_pixel(point.x, point.y))
        # Raio em pixels para sair do telhado com folga.
        radii.append(np.sqrt(poly.area) / geo.meters_per_pixel * 0.75)
    if not centers:
        return 0.0

    centers = np.array(centers)
    radii = np.array(radii)

    def darkness(angle_deg: float) -> float:
        theta = np.radians(angle_deg)
        # Azimute geografico: 0 = norte, cresce para leste. Em pixels, o norte
        # e -linha e o leste e +coluna.
        dx, dy = np.sin(theta), -np.cos(theta)
        total = []
        for step in (1.2, 1.8, 2.6):
            cols = centers[:, 0] + dx * radii * step
            rows = centers[:, 1] + dy * radii * step
            valid = ~_sample(inside, cols, rows)  # ignora o que caiu noutro telhado
            values = _sample(luma, cols, rows)
            if valid.any():
                total.append(float(values[valid].mean()))
        return float(np.mean(total)) if total else 1.0

    coarse = [(darkness(a), a) for a in range(0, 360, 10)]
    best = min(coarse)[1]
    fine = [(darkness(a), a) for a in np.arange(best - 10, best + 10.1, 2.0)]
    return float(min(fine)[1] % 360.0)


def _measure_lengths(
    luma: np.ndarray,
    inside: np.ndarray,
    geo,
    buildings,
    azimuth_deg: float,
) -> dict[int, float]:
    """Comprimento da sombra de cada predio, em metros."""
    mpp = geo.meters_per_pixel
    theta = np.radians(azimuth_deg)
    dx, dy = np.sin(theta), -np.cos(theta)

    max_steps = int(MAX_SHADOW_M / mpp)
    steps = np.arange(1, max_steps + 1, dtype=np.float64)
    height_px, width_px = luma.shape

    lengths: dict[int, float] = {}
    for building in buildings:
        poly = building.footprint
        if poly is None or poly.is_empty or poly.area < 30:
            continue

        boundary = _boundary_pixels(geo, poly, spacing_m=max(mpp * 3, 2.0))
        if len(boundary) < 4:
            continue

        # So interessa o lado voltado para a sombra: um passo a frente ja sai
        # do telhado.
        probe_c = boundary[:, 0] + dx * 2.0
        probe_r = boundary[:, 1] + dy * 2.0
        facing = ~_sample(inside, probe_c, probe_r)
        if facing.sum() < 3:
            continue
        origins = boundary[facing]

        # Referencia de claridade: anel bem afastado, do lado oposto a sombra.
        ref_c = boundary[:, 0] - dx * 6.0
        ref_r = boundary[:, 1] - dy * 6.0
        reference = _sample(luma, ref_c, ref_r)
        reference = reference[~_sample(inside, ref_c, ref_r)]
        if len(reference) < 3:
            continue
        threshold = float(np.median(reference)) * SHADOW_RATIO

        cols = origins[:, 0][:, None] + dx * steps[None, :]
        rows = origins[:, 1][:, None] + dy * steps[None, :]

        dentro_da_imagem = (
            (cols >= 0) & (cols < width_px) & (rows >= 0) & (rows < height_px)
        )
        valores = _sample(luma, cols, rows)
        em_telhado = _sample(inside, cols, rows)

        # A sombra termina no primeiro ponto claro; o raio e descartado se
        # esbarrar noutro predio antes de terminar.
        escuro = (valores < threshold) & dentro_da_imagem
        primeiro_claro = np.argmax(~escuro, axis=1)
        sem_fim = escuro.all(axis=1)
        primeiro_claro = np.where(sem_fim, escuro.shape[1], primeiro_claro)

        idx = np.arange(escuro.shape[1])[None, :]
        bateu_em_predio = (em_telhado & (idx < primeiro_claro[:, None])).any(axis=1)

        bons = (~bateu_em_predio) & (~sem_fim) & (primeiro_claro > 0)
        if bons.sum() < 3:
            continue

        # Percentil alto: cantos do predio dao sombra mais curta que a face.
        comprimento_px = float(np.percentile(primeiro_claro[bons], 65))
        comprimento_m = comprimento_px * mpp
        if comprimento_m < mpp * 1.5:
            continue
        lengths[building.osm_id] = comprimento_m

    return lengths


def estimate_heights(
    geo,
    buildings,
    known_heights: Optional[dict[int, float]] = None,
    default_sun_elevation: float = 52.0,
) -> ShadowResult:
    """Estima a altura dos edificios a partir das sombras da imagem."""
    result = ShadowResult()

    if geo is None or not buildings:
        result.note = "sem imagem ou sem edificios"
        return result
    if geo.meters_per_pixel > MAX_METERS_PER_PIXEL:
        result.note = (
            f"resolucao insuficiente ({geo.meters_per_pixel:.2f} m/px; "
            f"o limite util e {MAX_METERS_PER_PIXEL})"
        )
        return result

    luma = _luminance(geo.array.astype(np.float64))
    inside = _footprint_mask(geo, buildings)

    result.shadow_azimuth = estimate_shadow_azimuth(luma, geo, buildings, inside)
    lengths = _measure_lengths(luma, inside, geo, buildings, result.shadow_azimuth)
    result.measured = len(lengths)
    if not lengths:
        result.note = "nenhuma sombra mensuravel"
        return result

    # --- calibracao ---
    known_heights = known_heights or {}
    razoes = [
        known_heights[osm_id] / length
        for osm_id, length in lengths.items()
        if osm_id in known_heights and length > 1.0 and known_heights[osm_id] > 2.0
    ]
    if len(razoes) >= 5:
        result.scale = float(np.median(razoes))
        result.calibration = len(razoes)
        result.confidence = "alta" if len(razoes) >= 25 else "media"
    else:
        result.scale = float(np.tan(np.radians(default_sun_elevation)))
        result.calibration = len(razoes)
        result.confidence = "baixa"
        result.note = (
            f"sem calibracao suficiente ({len(razoes)} predios com altura conhecida); "
            f"assumindo sol a {default_sun_elevation:.0f} graus"
        )

    result.heights = {
        osm_id: float(np.clip(length * result.scale, MIN_HEIGHT_M, MAX_HEIGHT_M))
        for osm_id, length in lengths.items()
    }

    log.info(
        "Sombras: azimute %.0f graus, escala %.2f (sol ~%.0f graus), %d medidos, %d calibraram",
        result.shadow_azimuth,
        result.scale,
        np.degrees(np.arctan(result.scale)),
        result.measured,
        result.calibration,
    )
    return result
