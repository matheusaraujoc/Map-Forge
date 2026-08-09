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


def build_water_materials(ctx, waters, rivers) -> dict[int, Material]:
    """Cor real de cada corpo d'agua.

    No Brasil isso nao e detalhe: o Negro e preto de tanino, o Solimoes e barro,
    o Parnaiba e esverdeado de sedimento, uma lagoa costeira e quase turquesa.
    Pintar tudo com o mesmo azul de estilo e o que mais denuncia o mapa como
    desenho - e a foto sabe a resposta em cada ponto.

    Amostra com recuo para dentro, para nao pegar a margem, e por baixo de uma
    faixa de saturacao menor que a das areas verdes: agua realcada demais vira
    piscina.
    """
    from ..imagery.sampling import harmonize, sample_area_color

    imagery = ctx.imagery
    if imagery is None:
        return {}

    palette = ctx.palette
    amostras: dict[int, tuple[float, float, float]] = {}

    for water in waters:
        cor = sample_area_color(imagery, getattr(water, "geometry", None), inset=3.0)
        if cor is not None:
            amostras[water.osm_id] = harmonize(cor, saturation=1.05)

    for river in rivers:
        linha = getattr(river, "centerline", None)
        if linha is None or linha.is_empty:
            continue
        largura = max(river.width or 8.0, 4.0)
        # Faixa estreita no eixo: a borda do rio tem margem, banco de areia e
        # sombra de mata, que puxariam a cor para longe da agua.
        faixa = linha.buffer(largura * 0.3)
        cor = sample_area_color(imagery, faixa)
        if cor is not None:
            amostras[river.osm_id] = harmonize(cor, saturation=1.05)

    if not amostras:
        return {}

    ids = list(amostras)
    labels, centers = quantize(
        np.array([amostras[i] for i in ids]), 5, seed=ctx.settings.seed
    )
    amount = ctx.settings.area_blend
    materiais = [
        Material(
            name=f"water_sat_{i:02d}",
            color=blend(palette.style.water, tuple(float(c) for c in centro), amount),
            # Os mesmos parametros do material de agua do estilo: sem eles a agua
            # amostrada perderia o brilho e a transparencia e viraria chapa azul.
            roughness=0.15,
            metallic=0.1,
            opacity=palette.style.water_opacity,
        )
        for i, centro in enumerate(centers)
    ]
    log.info("Agua: %d corpos em %d cores", len(amostras), len(centers))
    return {osm_id: materiais[labels[n]] for n, osm_id in enumerate(ids)}


def build_road_materials(ctx, roads) -> dict[str, Material]:
    """Cor real de cada tipo de pavimento, medida na foto.

    O cinza de tabela nao existe na rua: asfalto novo e quase preto, asfalto
    gasto de cidade pequena e cinza-claro puxado para o marrom da poeira, e via
    de terra tem a cor do solo *daquele lugar* - vermelha no oeste paulista,
    clara no litoral, cinza no basalto. Uma cor por classe de superficie, medida
    onde a classe realmente aparece.

    E uma cor por classe, nao por via: as vias sao unidas por classe antes de
    virar geometria, entao cor por via exigiria desfazer a uniao - troca cara
    por um ganho que, a esta distancia, ninguem percebe.
    """
    from ..imagery.sampling import harmonize, sample_area_color

    from .roads import road_half_width, road_surface_key

    imagery = ctx.imagery
    if imagery is None or not roads:
        return {}

    palette = ctx.palette
    por_classe: dict[str, list] = {}
    for road in roads:
        linha = getattr(road, "centerline", None)
        if linha is None or linha.is_empty or linha.length < 12.0:
            continue
        por_classe.setdefault(road_surface_key(road), []).append(
            (linha, road_half_width(road))
        )

    padrao = {
        "major": palette.style.asphalt_major,
        "road": palette.style.asphalt,
        "foot": palette.style.footway,
        "cobble": palette.cobble.color,
        "dirt": palette.dirt.color,
    }

    saida: dict[str, Material] = {}
    for chave, itens in por_classe.items():
        amostras = []
        # Faixa estreita no eixo: a borda da via pega meio-fio, calcada e sombra.
        for linha, meia in sorted(itens, key=lambda it: -it[0].length)[:40]:
            cor = sample_area_color(imagery, linha.buffer(max(meia * 0.45, 0.8)))
            if cor is not None:
                amostras.append(cor)
        if len(amostras) < 3:
            continue
        media = tuple(float(c) for c in np.median(np.array(amostras), axis=0))
        base = padrao.get(chave)
        if base is None:
            continue
        saida[chave] = Material(
            name=f"{chave}_sat",
            # Saturacao baixa: pavimento realcado vira pista de corrida.
            color=blend(base, harmonize(media, saturation=0.95), ctx.settings.area_blend),
            roughness=palette.style.roughness,
        )

    if saida:
        log.info("Pavimento: %s medidos na foto", ", ".join(sorted(saida)))
    return saida


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
