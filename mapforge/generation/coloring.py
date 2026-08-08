"""Cores vindas da imagem de satelite.

Cada edificio tem a sua cor real, mas um material por edificio inviabilizaria o
arquivo: no glTF cada material vira uma primitiva separada. Entao as cores
amostradas sao quantizadas numa paleta pequena por k-means, e cada cor da paleta
e misturada com a do estilo. O resultado guarda a leitura do lugar real sem
abandonar a estilizacao - e a "mescla" entre foto e desenho.
"""

from __future__ import annotations

import logging

import numpy as np

from ..core.mesh import Material
from ..imagery.sampling import blend

log = logging.getLogger(__name__)


def quantize(colors: np.ndarray, k: int, iterations: int = 12, seed: int = 0):
    """k-means simples em RGB. Devolve (rotulos, centros)."""
    colors = np.asarray(colors, dtype=np.float64).reshape(-1, 3)
    k = max(1, min(k, len(colors)))

    rng = np.random.default_rng(seed)
    # k-means++ enxuto: primeiro centro aleatorio, demais longe dos escolhidos.
    centers = [colors[rng.integers(len(colors))]]
    for _ in range(k - 1):
        distance = np.min(
            [np.sum((colors - c) ** 2, axis=1) for c in centers], axis=0
        )
        total = distance.sum()
        if total <= 1e-12:
            centers.append(colors[rng.integers(len(colors))])
            continue
        centers.append(colors[rng.choice(len(colors), p=distance / total)])
    centers = np.array(centers)

    labels = np.zeros(len(colors), dtype=np.int64)
    for _ in range(iterations):
        distance = ((colors[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = np.argmin(distance, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for i in range(len(centers)):
            members = colors[labels == i]
            if len(members):
                centers[i] = members.mean(axis=0)

    return labels, centers


def _sampled_materials(
    ctx,
    features,
    style_color,
    prefix: str,
    k: int,
    inset: float,
) -> dict[int, Material]:
    """Quantiza a cor real de um conjunto de areas e devolve material por feature."""
    from ..imagery.sampling import harmonize, sample_area_color

    imagery = ctx.imagery
    sampled: dict[int, tuple[float, float, float]] = {}
    for feature in features:
        geometry = getattr(feature, "geometry", None)
        color = sample_area_color(imagery, geometry, inset=inset)
        if color is not None:
            sampled[feature.osm_id] = harmonize(color, saturation=1.15)

    if not sampled:
        return {}

    osm_ids = list(sampled)
    labels, centers = quantize(
        np.array([sampled[i] for i in osm_ids]), k, seed=ctx.settings.seed
    )
    amount = ctx.settings.area_blend
    materials = [
        Material(
            name=f"{prefix}_{i:02d}",
            color=blend(style_color, tuple(float(c) for c in center), amount),
            roughness=ctx.palette.style.roughness,
        )
        for i, center in enumerate(centers)
    ]
    return {osm_id: materials[labels[n]] for n, osm_id in enumerate(osm_ids)}


def build_area_materials(ctx, map_data) -> dict[str, dict[int, Material]]:
    """Cores reais para parques, bosques e estacionamentos.

    Mesma ideia dos telhados: a foto informa a cor, o estilo continua mandando na
    estilizacao. O que sai e superficie chapada low-poly, nao a foto colada.
    """
    if ctx.imagery is None:
        return {}
    palette = ctx.palette
    return {
        "park": _sampled_materials(ctx, map_data.parks, palette.style.grass, "grass_sat", 6, 2.0),
        "forest": _sampled_materials(
            ctx, map_data.forests, palette.style.forest_floor, "forest_sat", 6, 2.0
        ),
        "parking": _sampled_materials(
            ctx, map_data.parkings, palette.style.parking, "parking_sat", 4, 1.0
        ),
    }


def build_ground_color(ctx) -> Material | None:
    """Cor de fundo do terreno: mediana da imagem inteira.

    E o que faz o "vazio" entre as quadras deixar de ser uma cor arbitraria do
    estilo e passar a lembrar o solo daquele lugar.
    """
    from ..imagery.sampling import harmonize

    imagery = ctx.imagery
    if imagery is None:
        return None

    array = imagery.array.reshape(-1, 3)
    if len(array) > 20_000:  # amostra basta; a mediana e estavel
        array = array[:: len(array) // 20_000 + 1]
    median = tuple(float(c) / 255.0 for c in np.median(array, axis=0))

    color = blend(
        ctx.palette.style.ground,
        harmonize(median, saturation=1.05),  # type: ignore[arg-type]
        ctx.settings.area_blend,
    )
    return Material(name="ground_sat", color=color, roughness=ctx.palette.style.roughness)


def build_roof_materials(ctx, buildings) -> dict[int, Material]:
    """Material de telhado por edificio, a partir da imagem de satelite.

    Devolve um dicionario osm_id -> Material; edificios sem amostra ficam de
    fora e caem no telhado do estilo.
    """
    imagery = getattr(ctx, "imagery", None)
    if imagery is None or not buildings:
        return {}

    from ..imagery.sampling import sample_roof_colors

    sampled = sample_roof_colors(imagery, buildings)
    if not sampled:
        log.warning("Nenhuma cor de telhado amostrada da imagem")
        return {}

    osm_ids = list(sampled)
    colors = np.array([sampled[i] for i in osm_ids])
    labels, centers = quantize(
        colors, ctx.settings.roof_palette_size, seed=ctx.settings.seed
    )

    palette = ctx.palette
    amount = ctx.settings.roof_blend
    materials: list[Material] = []
    for i, center in enumerate(centers):
        # Mistura com a cor de telhado do estilo que estiver na mesma posicao,
        # para que o estilo continue influenciando o conjunto.
        style_color = palette.roof(i).color
        mixed = blend(style_color, tuple(float(c) for c in center), amount)
        materials.append(
            Material(name=f"roof_sat_{i:02d}", color=mixed, roughness=palette.style.roughness)
        )

    log.info("Telhados: %d amostrados em %d cores", len(sampled), len(centers))
    return {osm_id: materials[labels[n]] for n, osm_id in enumerate(osm_ids)}
