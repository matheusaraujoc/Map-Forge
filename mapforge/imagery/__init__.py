"""Imagens de satelite: download de tiles, mosaico e amostragem de cores.

O mapa vetorial do OSM da a estrutura (onde estao ruas, casas, quadras); a
imagem de satelite da a cor real. Os dois juntos evitam tanto o mapa "de
brinquedo" monocromatico quanto a fotogrametria pesada.
"""

from .sampling import GeoImage, harmonize, sample_area_color, sample_roof_colors
from .tiles import (
    PROVIDERS,
    TileError,
    TileProvider,
    fetch_imagery,
    get_provider,
    list_providers,
)

__all__ = [
    "GeoImage",
    "harmonize",
    "sample_area_color",
    "sample_roof_colors",
    "PROVIDERS",
    "TileError",
    "TileProvider",
    "fetch_imagery",
    "get_provider",
    "list_providers",
]
