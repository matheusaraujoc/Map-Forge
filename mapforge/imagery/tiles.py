"""Download e mosaico de tiles raster (Web Mercator / esquema XYZ).

O zoom nao e o da visualizacao no navegador: e escolhido pelo tamanho da regiao,
sempre o maior que o provedor oferece dentro do orcamento de pixels. Numa quadra
urbana isso costuma dar z19, ou ~0,3 m por pixel.
"""

from __future__ import annotations

import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from .. import config
from ..core.geo import BBox

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float], None]

# Circunferencia da Terra na projecao Web Mercator, por lado de tile.
EARTH_CIRCUMFERENCE = 40_075_016.686
TILE_SIZE = 256

# Teto de tiles por regiao, para nao disparar centenas de requisicoes sem querer.
#
# 400 tiles cobrem 6,4 km de lado no zoom 17 (1,19 m/px). Acima disso o zoom
# caia, e com ele **tudo o que deriva da foto**: a classificacao de cobertura
# passava a rodar em 2,39 m/px, o desenho do chao herdava a imprecisao e o mapa
# grande saia incoerente. Como o tile fica em cache e so a primeira geracao de
# cada regiao paga, o teto sobe para cobrir ~12 km no mesmo zoom.
MAX_TILES = 1600


class TileError(RuntimeError):
    pass


@dataclass(frozen=True)
class TileProvider:
    """Fonte de tiles. `url` aceita {z} {x} {y} e, no caso do Bing, {q}."""

    name: str
    label: str
    url: str
    max_zoom: int
    attribution: str
    key_env: Optional[str] = None  # variavel de ambiente com a chave
    key_param: str = "key"
    extension: str = "jpg"

    def requires_key(self) -> bool:
        return self.key_env is not None

    def api_key(self) -> Optional[str]:
        return os.environ.get(self.key_env) if self.key_env else None


PROVIDERS: dict[str, TileProvider] = {
    # Padrao: nao exige chave e permite uso academico com atribuicao.
    "esri": TileProvider(
        name="esri",
        label="Esri World Imagery",
        url=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        max_zoom=19,
        attribution="Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    ),
    "mapbox": TileProvider(
        name="mapbox",
        label="Mapbox Satellite",
        url="https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}@2x.jpg90",
        max_zoom=22,
        attribution="(c) Mapbox, (c) Maxar",
        key_env="MAPBOX_TOKEN",
        key_param="access_token",
    ),
    "bing": TileProvider(
        name="bing",
        label="Bing Aerial",
        url="https://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1",
        max_zoom=20,
        attribution="(c) Microsoft, (c) Maxar",
        key_env="BING_KEY",
    ),
}


def _custom_provider() -> Optional[TileProvider]:
    """Fonte definida pelo usuario via MAPFORGE_TILE_URL.

    Existe para quem tem acesso licenciado a outra imagem (ortofoto municipal,
    convenio institucional, chave propria de um servico): basta apontar o
    template XYZ, sem tocar no codigo.
    """
    url = os.environ.get("MAPFORGE_TILE_URL")
    if not url:
        return None
    return TileProvider(
        name="custom",
        label=os.environ.get("MAPFORGE_TILE_LABEL", "Fonte personalizada"),
        url=url,
        max_zoom=int(os.environ.get("MAPFORGE_TILE_MAXZOOM", "20")),
        attribution=os.environ.get("MAPFORGE_TILE_ATTRIBUTION", "fonte personalizada"),
    )


def get_provider(name: str) -> TileProvider:
    if name == "custom":
        provider = _custom_provider()
        if provider is None:
            raise TileError(
                "provedor 'custom' exige a variavel de ambiente MAPFORGE_TILE_URL "
                "com um template XYZ, por exemplo https://.../{z}/{x}/{y}.jpg"
            )
        return provider
    try:
        return PROVIDERS[name]
    except KeyError:
        raise TileError(
            f"provedor desconhecido: {name!r}. Disponiveis: {', '.join(list_providers())}"
        ) from None


def list_providers() -> list[str]:
    names = list(PROVIDERS)
    if _custom_provider() is not None:
        names.append("custom")
    return names


# ------------------------------------------------------------ math do mercator


def lonlat_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Coordenada de tile fracionaria (x, y) em Web Mercator."""
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def meters_per_pixel(lat: float, zoom: int) -> float:
    return EARTH_CIRCUMFERENCE * math.cos(math.radians(lat)) / (TILE_SIZE * 2.0**zoom)


# Lado maximo da foto montada, em pixels.
#
# Era 4096, e esse era o **verdadeiro** limitador da resolucao em regiao grande -
# nao o teto de tiles. Numa regiao de 6 km, 4096 px forcam 1,46 m/px, entao o
# zoom 17 (1,19 m/px) nao cabia e caia para o 16 (2,39 m/px). Como tudo que vem
# da foto herda isso - classificacao de cobertura, cor amostrada, deteccao -, o
# mapa grande saia incoerente por causa de uma constante.
#
# 8192 sustenta 1,19 m/px ate ~9,7 km de lado. A foto ocupa 201 MB em memoria no
# pior caso, e os tiles ficam em cache: so a primeira geracao da regiao paga.
DEFAULT_MAX_PIXELS = 8192


def choose_zoom(
    bbox: BBox, provider: TileProvider, max_pixels: int = DEFAULT_MAX_PIXELS
) -> int:
    """Maior zoom cuja imagem ainda cabe em `max_pixels` no lado maior."""
    lat = bbox.center[0]
    largest = max(bbox.width_m, bbox.height_m)
    for zoom in range(provider.max_zoom, 0, -1):
        if largest / meters_per_pixel(lat, zoom) <= max_pixels:
            return zoom
    return 1


def quadkey(x: int, y: int, zoom: int) -> str:
    """Chave de tile do Bing Maps."""
    key = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        key.append(str(digit))
    return "".join(key)


def _tile_url(provider: TileProvider, x: int, y: int, zoom: int) -> str:
    url = provider.url.format(z=zoom, x=x, y=y, q=quadkey(x, y, zoom))
    key = provider.api_key()
    if provider.requires_key():
        if not key:
            raise TileError(
                f"provedor '{provider.name}' precisa da variavel de ambiente "
                f"{provider.key_env} com a chave de acesso"
            )
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{provider.key_param}={key}"
    return url


# ------------------------------------------------------------------- download


def _fetch_tile(session: requests.Session, provider: TileProvider, x: int, y: int, zoom: int):
    url = _tile_url(provider, x, y, zoom)
    response = session.get(url, timeout=30, headers={"User-Agent": config.USER_AGENT})
    response.raise_for_status()
    return response.content


def is_placeholder(blob: bytes) -> bool:
    """Detecta o tile de 'imagem indisponivel'.

    O Esri responde 200 com um retangulo cinza escrito "Map data not yet
    available" quando nao tem cobertura naquele zoom - se nao for detectado, o
    mapa inteiro sai texturizado de cinza. A assinatura e ser acromatico (os
    tres canais praticamente iguais) e ter variacao baixissima.
    """
    import io

    import numpy as np
    from PIL import Image

    try:
        array = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"), dtype=np.int16)
    except Exception:  # noqa: BLE001 - conteudo ilegivel conta como inutil
        return True

    chroma = float(
        np.abs(array[:, :, 0] - array[:, :, 1]).mean()
        + np.abs(array[:, :, 1] - array[:, :, 2]).mean()
    )
    if chroma > 4.0:
        return False
    return float(array.std()) < 22.0


def probe_zoom(
    bbox: BBox,
    provider: TileProvider,
    zoom: int,
    min_zoom: int = 14,
    session: Optional[requests.Session] = None,
) -> int:
    """Maior zoom com cobertura real, testando um tile central por nivel."""
    owns = session is None
    session = session or requests.Session()
    try:
        for candidate in range(zoom, min_zoom - 1, -1):
            lat, lon = bbox.center
            xf, yf = lonlat_to_tile(lat, lon, candidate)
            try:
                blob = _fetch_tile(session, provider, int(xf), int(yf), candidate)
            except Exception as exc:  # noqa: BLE001 - tenta o nivel de baixo
                log.debug("Sondagem z%d falhou: %s", candidate, exc)
                continue
            if not is_placeholder(blob):
                return candidate
            log.info("Sem cobertura em z%d (%s), tentando z%d", candidate, provider.name, candidate - 1)
        return min_zoom
    finally:
        if owns:
            session.close()


def fetch_imagery(
    bbox: BBox,
    provider_name: str = "esri",
    cache=None,
    zoom: Optional[int] = None,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    workers: int = 12,
    progress: Optional[ProgressFn] = None,
    force: bool = False,
):
    """Baixa e costura a imagem de satelite que cobre exatamente a bbox.

    Devolve um GeoImage (imagem PIL + metadados de georreferencia).
    """
    from .sampling import GeoImage

    provider = get_provider(provider_name)
    image, actual_zoom = download_mosaic(
        bbox, provider, zoom, cache, max_pixels, workers, progress, force, probe=True
    )
    if progress:
        progress(f"satelite {image.width}x{image.height} px", 0.7)
    return GeoImage(
        image=image,
        bbox=bbox,
        zoom=actual_zoom,
        provider=provider.name,
        attribution=provider.attribution,
    )


def download_mosaic(
    bbox: BBox,
    provider: TileProvider,
    zoom: Optional[int] = None,
    cache=None,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    workers: int = 12,
    progress: Optional[ProgressFn] = None,
    force: bool = False,
    probe: bool = True,
):
    """Baixa os tiles, costura o mosaico e recorta na bbox.

    Serve tanto para imagem de satelite quanto para o modelo de elevacao: muda
    so como os pixels sao interpretados depois. Devolve (imagem PIL, zoom real).
    """
    from PIL import Image

    if zoom is not None:
        zoom = min(zoom, provider.max_zoom)
        if probe:
            # Mesmo pedido a mao, o zoom precisa ter cobertura: sem esta
            # verificacao o mosaico vira o retangulo cinza de "sem imagem" e
            # tudo a jusante interpreta aquilo como superficie clara.
            real = probe_zoom(bbox, provider, zoom)
            if real < zoom:
                log.warning(
                    "Zoom %d sem cobertura em %s; usando z%d", zoom, provider.name, real
                )
                if progress:
                    progress(f"z{zoom} sem cobertura, usando z{real}", 0.02)
                zoom = real
    elif probe:
        # Sonda a cobertura real: o zoom maximo do provedor nem sempre existe
        # para a regiao, e um placeholder cinza passaria despercebido.
        if progress:
            progress("verificando cobertura", 0.01)
        zoom = probe_zoom(bbox, provider, choose_zoom(bbox, provider, max_pixels))
    else:
        zoom = choose_zoom(bbox, provider, max_pixels)

    x0f, y0f = lonlat_to_tile(bbox.north, bbox.west, zoom)  # canto superior esquerdo
    x1f, y1f = lonlat_to_tile(bbox.south, bbox.east, zoom)  # canto inferior direito
    x0, y0 = int(math.floor(x0f)), int(math.floor(y0f))
    x1, y1 = int(math.ceil(x1f)), int(math.ceil(y1f))

    columns, rows = x1 - x0, y1 - y0
    total = columns * rows
    if total > MAX_TILES:
        # Cai um nivel de zoom ate caber, em vez de recusar o pedido.
        while total > MAX_TILES and zoom > 1:
            zoom -= 1
            x0f, y0f = lonlat_to_tile(bbox.north, bbox.west, zoom)
            x1f, y1f = lonlat_to_tile(bbox.south, bbox.east, zoom)
            x0, y0 = int(math.floor(x0f)), int(math.floor(y0f))
            x1, y1 = int(math.ceil(x1f)), int(math.ceil(y1f))
            columns, rows = x1 - x0, y1 - y0
            total = columns * rows
        log.warning("Zoom reduzido para %d para caber em %d tiles", zoom, MAX_TILES)

    if progress:
        mpp = meters_per_pixel(bbox.center[0], zoom)
        progress(f"{provider.name} z{zoom} ({mpp:.2f} m/px, {total} tiles)", 0.02)

    coords = [(x, y) for y in range(y0, y1) for x in range(x0, x1)]
    images: dict[tuple[int, int], bytes] = {}
    missing: list[tuple[int, int]] = []

    if cache is not None and not force:
        for x, y in coords:
            blob = cache.get_tile(provider.name, zoom, x, y)
            if blob is None:
                missing.append((x, y))
            else:
                images[(x, y)] = blob
    else:
        missing = list(coords)

    if missing:
        done = 0
        with requests.Session() as session:
            # O pool padrao do urllib3 e menor que o numero de workers e ficaria
            # descartando conexoes a cada tile.
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=workers, pool_maxsize=workers
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_fetch_tile, session, provider, x, y, zoom): (x, y)
                    for x, y in missing
                }
                for future in futures:
                    key = futures[future]
                    try:
                        blob = future.result()
                        images[key] = blob
                        if cache is not None:
                            cache.put_tile(provider.name, zoom, key[0], key[1], blob)
                    except Exception as exc:  # noqa: BLE001 - tile faltando vira buraco
                        log.debug("Tile %s falhou: %s", key, exc)
                    done += 1
                    if progress and done % 8 == 0:
                        progress(
                            f"{provider.name} {done}/{len(missing)} tiles",
                            0.02 + 0.6 * done / len(missing),
                        )

    if not images:
        raise TileError(
            f"nenhum tile obtido de '{provider.name}' - verifique a conexao ou a chave de acesso"
        )

    # --- mosaico ---
    import io

    mosaic = Image.new("RGB", (columns * TILE_SIZE, rows * TILE_SIZE), (128, 128, 128))
    for (x, y), blob in images.items():
        try:
            tile = Image.open(io.BytesIO(blob)).convert("RGB")
        except Exception:  # noqa: BLE001 - tile corrompido
            continue
        if tile.size != (TILE_SIZE, TILE_SIZE):
            tile = tile.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
        mosaic.paste(tile, ((x - x0) * TILE_SIZE, (y - y0) * TILE_SIZE))

    # --- recorte exato da bbox ---
    left = int(round((x0f - x0) * TILE_SIZE))
    top = int(round((y0f - y0) * TILE_SIZE))
    right = int(round((x1f - x0) * TILE_SIZE))
    bottom = int(round((y1f - y0) * TILE_SIZE))
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    return mosaic.crop((left, top, right, bottom)), zoom
