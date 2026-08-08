"""Deteccao de edificios na imagem de satelite, guiada pelo mapa vetorial.

Existe porque as fontes abertas de contorno tem buracos: o conjunto da Microsoft
nao cobre o quadkey de Araioses-MA, por exemplo, embora cubra Sao Luis e
Teresina. Onde nao ha contorno pronto, esta e a ultima linha.

Sozinha, classificacao de pixel por cor confunde telhado com solo exposto. O que
torna o resultado utilizavel e usar o mapa 2D do OSM como filtro:

* onde ha rua, agua ou mata, nao ha telhado;
* casa fica perto de rua - candidato longe de qualquer via e descartado;
* o que ja existe como edificio (OSM ou Microsoft) e removido antes.

O contorno devolvido e um retangulo orientado ajustado por PCA sobre os pixels
da mancha. Nao e o contorno exato do telhado, mas para uma cena low-poly o
retangulo orientado e melhor do que um blob irregular ruidoso - e e o que mais
se parece com uma casa vista de cima.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from shapely.geometry import Polygon

log = logging.getLogger(__name__)

# Faixa plausivel de area de um telhado isolado, em metros quadrados.
MIN_AREA_M2 = 22.0
MAX_AREA_M2 = 4000.0

# Distancia maxima ate uma via para o candidato ser aceito.
MAX_ROAD_DISTANCE_M = 70.0

# Quanto do retangulo orientado precisa estar preenchido pela mancha.
MIN_RECT_FILL = 0.55

# Acima desta area a mancha provavelmente e um quarteirao de casas geminadas.
SPLIT_AREA_M2 = 300.0
TYPICAL_HOUSE_M = 10.5
# Preenchimento do retangulo orientado acima do qual a mancha e aceita como um
# unico predio grande (galpao, escola, igreja) em vez de quarteirao fundido.
SOLID_FILL = 0.82

MAX_METERS_PER_PIXEL = 1.2


@dataclass
class DetectionResult:
    polygons: list[Polygon] = field(default_factory=list)
    candidates: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    note: str = ""

    def summary(self) -> dict:
        return {
            "detected": len(self.polygons),
            "candidates": self.candidates,
            "rejected": dict(self.rejected),
        }


# ------------------------------------------------------------------- mascaras


def _rasterize(geo, geometries, width_m: float = 0.0) -> np.ndarray:
    """Desenha geometrias (em metros locais) numa mascara do tamanho da imagem."""
    from PIL import Image, ImageDraw

    width_px, height_px = geo.size
    canvas = Image.new("L", (width_px, height_px), 0)
    draw = ImageDraw.Draw(canvas)

    for geom in geometries:
        if geom is None or geom.is_empty:
            continue
        shape = geom.buffer(width_m) if width_m > 0 else geom
        polys = [shape] if shape.geom_type == "Polygon" else list(getattr(shape, "geoms", []))
        for poly in polys:
            if poly.geom_type != "Polygon" or poly.is_empty:
                continue
            pixels = [geo.to_pixel(x, y) for x, y in poly.exterior.coords]
            if len(pixels) >= 3:
                draw.polygon(pixels, fill=255)
    return np.asarray(canvas, dtype=np.uint8) > 0


def _roof_likelihood(rgb: np.ndarray) -> np.ndarray:
    """Pixels que parecem telhado: ceramica avermelhada ou laje clara.

    Duas familias porque no Brasil o telhado ou e ceramica (R > G > B, bem
    saturado) ou e laje / fibrocimento / metal (claro e dessaturado). Vegetacao
    e recortada pelo excesso de verde, que e o discriminador mais confiavel em
    banda visivel.
    """
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    high = np.maximum(np.maximum(r, g), b)
    low = np.minimum(np.minimum(r, g), b)
    saturation = high - low

    # Excesso de verde: positivo em vegetacao.
    excess_green = 2.0 * g - r - b
    vegetation = excess_green > 0.045

    ceramic = (r > g + 0.035) & (g >= b) & (saturation > 0.09) & (luma > 0.16) & (luma < 0.82)
    slab = (luma > 0.52) & (saturation < 0.16) & (excess_green < 0.02)

    return (ceramic | slab) & ~vegetation


def _morphology(mask: np.ndarray, meters_per_pixel: float) -> np.ndarray:
    """Tira sujeira e fecha buracos, com raio proporcional a escala."""
    from scipy import ndimage

    radius = max(int(round(0.9 / max(meters_per_pixel, 0.05))), 1)
    structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    cleaned = ndimage.binary_opening(mask, structure=structure)
    return ndimage.binary_closing(cleaned, structure=structure)


# ------------------------------------------------------------------ geometria


def _oriented_rectangle(pixels: np.ndarray) -> tuple[np.ndarray, float]:
    """Retangulo orientado por PCA sobre os pixels da mancha.

    Devolve os 4 cantos (em pixels) e a fracao da area do retangulo que a mancha
    de fato ocupa - um blob em L preenche pouco e por isso e rejeitado.
    """
    center = pixels.mean(axis=0)
    centered = pixels - center
    # Autovetores da covariancia: o maior alinha com o lado longo da casa.
    cov = np.cov(centered, rowvar=False)
    if not np.all(np.isfinite(cov)):
        return np.empty((0, 2)), 0.0
    values, vectors = np.linalg.eigh(cov)
    axis_major = vectors[:, int(np.argmax(values))]
    axis_minor = np.array([-axis_major[1], axis_major[0]])

    proj_major = centered @ axis_major
    proj_minor = centered @ axis_minor
    lo_a, hi_a = proj_major.min(), proj_major.max()
    lo_b, hi_b = proj_minor.min(), proj_minor.max()

    rect_area = (hi_a - lo_a) * (hi_b - lo_b)
    fill = len(pixels) / rect_area if rect_area > 1e-6 else 0.0

    corners = np.array(
        [
            center + axis_major * lo_a + axis_minor * lo_b,
            center + axis_major * hi_a + axis_minor * lo_b,
            center + axis_major * hi_a + axis_minor * hi_b,
            center + axis_major * lo_a + axis_minor * hi_b,
        ]
    )
    return corners, float(fill)


def _pixels_to_world(geo, corners: np.ndarray) -> np.ndarray:
    """Cantos em pixel -> metros locais (o mapeamento e linear e separavel)."""
    width_px, height_px = geo.size
    half_w = geo.bbox.width_m / 2.0
    half_h = geo.bbox.height_m / 2.0
    x = corners[:, 0] / max(width_px - 1, 1) * (2 * half_w) - half_w
    y = half_h - corners[:, 1] / max(height_px - 1, 1) * (2 * half_h)
    return np.column_stack([x, y])


def _split_block(polygon: Polygon, fill: float) -> list[Polygon]:
    """Fatia uma mancha grande em unidades do tamanho de uma casa.

    Numa cidade terrea as casas se tocam e viram uma mancha so. Deixar assim
    produz um "predio" de 1000 m2 que a classificacao por area transforma em
    torre de dez andares - foi exatamente o que apareceu em Araioses.

    O que distingue um quarteirao fundido de um galpao de verdade e a
    regularidade: telhado unico e limpo preenche quase todo o retangulo
    orientado, enquanto um aglomerado de casas fica irregular. Por isso a
    divisao so acontece abaixo de um limiar de preenchimento.
    """
    coords = np.asarray(polygon.exterior.coords, dtype=np.float64)[:-1]
    if len(coords) != 4 or polygon.area <= SPLIT_AREA_M2 or fill >= SOLID_FILL:
        return [polygon]

    edge_a = coords[1] - coords[0]
    edge_b = coords[3] - coords[0]
    len_a = float(np.linalg.norm(edge_a))
    len_b = float(np.linalg.norm(edge_b))
    if len_a < 1e-6 or len_b < 1e-6:
        return [polygon]

    cols = max(int(round(len_a / TYPICAL_HOUSE_M)), 1)
    rows = max(int(round(len_b / TYPICAL_HOUSE_M)), 1)
    if cols * rows <= 1:
        return [polygon]

    step_a = edge_a / cols
    step_b = edge_b / rows
    gap_a = step_a * 0.07  # fresta entre as casas, para a silhueta nao virar bloco
    gap_b = step_b * 0.07
    origin = coords[0]

    out: list[Polygon] = []
    for i in range(cols):
        for j in range(rows):
            a = origin + step_a * i + step_b * j + (gap_a + gap_b) / 2
            piece = Polygon(
                [
                    a,
                    a + step_a - gap_a,
                    a + step_a - gap_a + step_b - gap_b,
                    a + step_b - gap_b,
                ]
            )
            if piece.is_valid and not piece.is_empty and piece.area > MIN_AREA_M2:
                out.append(piece)
    return out or [polygon]


# ------------------------------------------------------------------- deteccao


def detect_buildings(
    geo,
    map_data,
    existing: Optional[list[Polygon]] = None,
    max_road_distance: float = MAX_ROAD_DISTANCE_M,
) -> DetectionResult:
    """Encontra telhados na imagem que ainda nao existem como edificio."""
    result = DetectionResult()

    if geo is None:
        result.note = "sem imagem de satelite"
        return result
    if geo.meters_per_pixel > MAX_METERS_PER_PIXEL:
        result.note = f"resolucao insuficiente ({geo.meters_per_pixel:.2f} m/px)"
        return result

    try:
        from scipy import ndimage
    except ImportError:  # pragma: no cover
        result.note = "scipy nao instalado"
        return result

    mpp = geo.meters_per_pixel
    pixel_area = mpp * mpp
    rgb = geo.array.astype(np.float64) / 255.0

    mask = _roof_likelihood(rgb)

    # --- o mapa 2D recorta o que nao pode ser telhado ---
    from ..generation.roads import road_half_width

    road_geoms = [
        r.centerline.buffer(road_half_width(r) + 2.0)
        for r in map_data.roads
        if r.centerline is not None
    ]
    roads_mask = _rasterize(geo, road_geoms) if road_geoms else np.zeros_like(mask)

    blocked = roads_mask.copy()
    for features in (map_data.waters, map_data.forests):
        geoms = [f.geometry for f in features if f.geometry is not None]
        if geoms:
            blocked |= _rasterize(geo, geoms)
    river_geoms = [
        r.centerline.buffer(max(r.width, 3.0) / 2 + 2.0)
        for r in map_data.rivers
        if r.centerline is not None
    ]
    if river_geoms:
        blocked |= _rasterize(geo, river_geoms)

    mask &= ~blocked
    mask = _morphology(mask, mpp)

    # --- casa fica perto de rua ---
    if roads_mask.any():
        road_distance = ndimage.distance_transform_edt(~roads_mask) * mpp
    else:
        road_distance = np.zeros_like(mask, dtype=np.float64)

    # --- o que ja existe como edificio sai da disputa ---
    known = list(existing or [])
    known += [b.footprint for b in map_data.buildings if b.footprint is not None]
    known_mask = _rasterize(geo, known, width_m=1.0) if known else None

    labels, count = ndimage.label(mask)
    result.candidates = int(count)
    rejected = {"area": 0, "forma": 0, "longe_da_via": 0, "ja_existe": 0}

    slices = ndimage.find_objects(labels)
    for index, window in enumerate(slices, start=1):
        if window is None:
            continue
        sub = labels[window] == index
        pixel_count = int(sub.sum())
        area = pixel_count * pixel_area
        if area < MIN_AREA_M2 or area > MAX_AREA_M2:
            rejected["area"] += 1
            continue

        rows, cols = np.nonzero(sub)
        rows = rows + window[0].start
        cols = cols + window[1].start

        if known_mask is not None and known_mask[rows, cols].mean() > 0.25:
            rejected["ja_existe"] += 1
            continue

        if road_distance[rows, cols].min() > max_road_distance:
            rejected["longe_da_via"] += 1
            continue

        corners, fill = _oriented_rectangle(np.column_stack([cols, rows]).astype(np.float64))
        if len(corners) != 4 or fill < MIN_RECT_FILL:
            rejected["forma"] += 1
            continue

        world = _pixels_to_world(geo, corners)
        polygon = Polygon(world)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty or polygon.geom_type != "Polygon":
            rejected["forma"] += 1
            continue
        if polygon.area < MIN_AREA_M2:
            rejected["area"] += 1
            continue

        result.polygons.extend(_split_block(polygon, fill))

    result.rejected = rejected
    log.info(
        "Deteccao: %d manchas -> %d edificios (rejeitados: %s)",
        result.candidates,
        len(result.polygons),
        rejected,
    )
    return result
