"""Caminhos e constantes globais do MapForge."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("MAPFORGE_DATA", ROOT / "data"))
OUTPUT_DIR = Path(os.environ.get("MAPFORGE_OUTPUT", ROOT / "output"))
CACHE_DB = DATA_DIR / "mapforge.db"

# Espelhos publicos do Overpass, tentados em ordem. Responder 504 quando esta
# ocupado e comportamento normal desses servidores, entao ter varios importa
# mais do que ter o "melhor".
#
# ATENCAO: so entra aqui espelho com o planeta inteiro. Varias instancias
# publicas servem so um pais - overpass.osm.ch, por exemplo, responde rapido e
# com sucesso para o Brasil, mas devolve zero elementos, porque so tem a Suica.
# Um espelho assim envenena o cache e o mapa sai vazio sem nenhum erro.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"

USER_AGENT = "MapForge/0.1 (procedural city generator; OSM data)"

# Validade do cache de download, em segundos (30 dias).
CACHE_TTL = 30 * 24 * 3600

# Limite de area para evitar downloads gigantes por engano (km^2).
MAX_AREA_KM2 = 25.0


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
