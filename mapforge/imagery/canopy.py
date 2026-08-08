"""Deteccao de vegetacao na imagem de satelite.

O OpenStreetMap so tem mata onde alguem desenhou o poligono. Em Araioses-MA a
cidade inteira tem 10 areas verdes mapeadas e nenhuma mata - e o resultado e uma
mancha de floresta de 20 ha completamente pelada no modelo 3D.

Vegetacao e o caso *facil* da visao computacional em banda visivel, ao contrario
de telhado: o excesso de verde (2G - R - B) e um discriminador robusto, que
resiste a variacao de iluminacao e nao confunde com solo exposto, asfalto ou
telha. Por isso aqui a deteccao vale a pena, enquanto na deteccao de edificio
nao valia - la a fonte pronta (Overture) ganha de longe.

O que separa copa de gramado nao e a cor, e a *textura*: copa e um aglomerado de
formas irregulares que sombreiam umas as outras, entao o desvio padrao local e
alto; pasto e campo sao lisos. Brilho ajuda como segundo sinal, porque copa
densa e mais escura.

Saida: poligonos em coordenadas locais (metros), prontos para entrar na geracao
como se fossem areas verdes do OSM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .segment import local_variation, rgb_to_hsv

log = logging.getLogger(__name__)

# Excesso de verde a partir do qual o pixel e vegetacao. Calibrado para pegar
# pasto seco de fim de estacao sem deixar entrar telhado esverdeado de zinco.
MIN_GREEN_EXCESS = 0.025

# Textura (desvio padrao local) que separa copa de gramado, medida num raio de
# ~2,5 m: e a escala de uma copa isolada.
TEXTURE_RADIUS_M = 2.5
CANOPY_TEXTURE = 0.055

# Copa densa tambem e escura; serve como segundo sinal quando a textura falha
# (mata fechada e homogenea vista de cima).
CANOPY_VALUE = 0.34

# Lado da celula usada para vetorizar a mascara. Menor da contorno melhor e
# custa mais; 5 m e o suficiente para a borda de uma mata.
CELL_M = 5.0

# Area minima de uma mancha, em metros quadrados. Abaixo disso e arvore isolada
# de quintal, que nao vale um poligono.
MIN_PATCH_M2 = 120.0


@dataclass
class CanopyMap:
    """Vegetacao encontrada na foto, em mascara e em poligono."""

    canopy: np.ndarray  # bool: copa densa (mata, bosque, quintal arborizado)
    grass: np.ndarray  # bool: vegetacao rasteira (campo, pasto, gramado)
    meters_per_pixel: float
    canopy_polygons: list[Polygon] = field(default_factory=list)
    grass_polygons: list[Polygon] = field(default_factory=list)

    @property
    def canopy_area(self) -> float:
        return float(self.canopy.sum()) * self.meters_per_pixel**2

    @property
    def grass_area(self) -> float:
        return float(self.grass.sum()) * self.meters_per_pixel**2

    def summary(self) -> str:
        return (
            f"copa {self.canopy_area / 1e4:.2f} ha em {len(self.canopy_polygons)} manchas, "
            f"rasteira {self.grass_area / 1e4:.2f} ha em {len(self.grass_polygons)} manchas"
        )


def _clean(mask: np.ndarray, meters_per_pixel: float, abertura_m: float) -> np.ndarray:
    """Abertura seguida de fechamento: tira pixel solto e fecha buraco pequeno."""
    from scipy import ndimage

    raio = max(int(round(abertura_m / max(meters_per_pixel, 0.05))), 1)
    estrutura = np.ones((2 * raio + 1, 2 * raio + 1), dtype=bool)
    aberta = ndimage.binary_opening(mask, structure=estrutura)
    return ndimage.binary_closing(aberta, structure=estrutura)


def _exclusion_mask(geo, map_data) -> Optional[np.ndarray]:
    """Onde o mapa vetorial ja sabe que nao ha vegetacao: via, agua, edificio."""
    if map_data is None:
        return None

    from .detect import _rasterize
    from .segment import _road_mask, _water_mask

    fora = _road_mask(geo, map_data)
    agua = _water_mask(geo, map_data)
    if agua is not None:
        fora = fora | agua

    footprints = [b.footprint for b in map_data.buildings if b.footprint is not None]
    if footprints:
        # Copa que invade telhado e sombra de arvore vizinha, nao arvore.
        fora = fora | _rasterize(geo, footprints)
    return fora


def _polygonize(mask: np.ndarray, geo, cell_m: float, min_area: float) -> list[Polygon]:
    """Vetoriza a mascara agregando em celulas quadradas.

    Nao ha `rasterio` aqui, e nao vale acrescentar a dependencia: agregar a
    mascara numa grade grosseira e unir as celulas cheias da um contorno bom o
    bastante para plantar arvore, que e o uso.
    """
    if not mask.any():
        return []

    mpp = geo.meters_per_pixel
    passo = max(int(round(cell_m / max(mpp, 0.05))), 1)
    altura, largura = mask.shape

    # Fracao de pixels de vegetacao em cada celula; meia celula ja conta.
    linhas = altura // passo
    colunas = largura // passo
    if linhas == 0 or colunas == 0:
        return []
    recorte = mask[: linhas * passo, : colunas * passo]
    ocupacao = recorte.reshape(linhas, passo, colunas, passo).mean(axis=(1, 3))
    cheias = ocupacao >= 0.5

    # Fecha buraco de uma celula so na grade grosseira. Sem isso, uma mata
    # fechada sai como um cardume de tiras finas, porque a ocupacao oscila em
    # volta do limiar de celula para celula.
    if cheias.any():
        from scipy import ndimage

        # `border_value=1`: sem isso a erosao do fechamento come uma celula em
        # toda mancha encostada na borda da imagem, e a mata do canto encolhe.
        cheias = ndimage.binary_closing(
            cheias, structure=np.ones((3, 3), dtype=bool), border_value=1
        )
    if not cheias.any():
        return []

    # Celula -> caixa em metros. As duas bordas saem da MESMA formula: escrever
    # `x1 = x0 + lado` faz o lado direito de uma celula diferir do lado esquerdo
    # da vizinha no ultimo bit, e a uniao entao nao funde as celulas - era o que
    # produzia mata listrada em vez de mata inteira.
    lado = passo * mpp
    meia_largura = largura * mpp / 2.0
    meia_altura = altura * mpp / 2.0
    ys, xs = np.nonzero(cheias)
    x0 = -meia_largura + xs * lado
    x1 = -meia_largura + (xs + 1) * lado
    y1 = meia_altura - ys * lado
    y0 = meia_altura - (ys + 1) * lado

    caixas = shapely.box(x0, y0, x1, y1)
    juntas = unary_union(caixas.tolist())

    saida = []
    for poly in getattr(juntas, "geoms", [juntas]):
        if isinstance(poly, Polygon) and poly.area >= min_area:
            # Simplifica na escala da celula: o contorno em escada nao ajuda em
            # nada e multiplica os vertices.
            limpo = poly.simplify(cell_m * 0.5, preserve_topology=True)
            if not limpo.is_empty and limpo.area >= min_area:
                saida.append(limpo)
    return saida


def detect_canopy(
    geo,
    map_data=None,
    cell_m: float = CELL_M,
    min_patch_m2: float = MIN_PATCH_M2,
) -> CanopyMap:
    """Separa copa densa de vegetacao rasteira na foto e devolve os poligonos."""
    rgb = geo.array.astype(np.float64) / 255.0
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    _, _, valor = rgb_to_hsv(rgb)
    cinza = 0.2126 * r + 0.7152 * g + 0.0722 * b

    mpp = geo.meters_per_pixel
    textura = local_variation(cinza, radius_px=max(int(round(TEXTURE_RADIUS_M / max(mpp, 0.05))), 1))

    vegetacao = (2.0 * g - r - b) > MIN_GREEN_EXCESS

    fora = _exclusion_mask(geo, map_data)
    if fora is not None:
        vegetacao = vegetacao & ~fora

    # Copa: texturada OU escura. Gramado e o resto da vegetacao.
    copa = vegetacao & ((textura > CANOPY_TEXTURE) | (valor < CANOPY_VALUE))
    rasteira = vegetacao & ~copa

    copa = _clean(copa, mpp, abertura_m=1.5)
    # A limpeza da copa pode devolver pixel para o gramado; recalcula depois.
    rasteira = _clean(rasteira & ~copa, mpp, abertura_m=2.5)

    mapa = CanopyMap(canopy=copa, grass=rasteira, meters_per_pixel=mpp)
    mapa.canopy_polygons = _polygonize(copa, geo, cell_m, min_patch_m2)
    mapa.grass_polygons = _polygonize(rasteira, geo, cell_m, min_patch_m2 * 4)

    log.info("Vegetacao detectada: %s", mapa.summary())
    return mapa
