"""Estilos graficos.

Um estilo e um conjunto de paletas + parametros de geracao. Trocar o estilo muda
completamente a aparencia do mesmo mapa sem regerar os dados do OSM.
"""

from .presets import STYLES, Palette, Style, get_style, list_styles

__all__ = ["STYLES", "Palette", "Style", "get_style", "list_styles"]
