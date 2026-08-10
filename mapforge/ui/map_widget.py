"""Mapa deslizante embutido, para escolher a regiao com o mouse.

O mapa e uma pagina HTML propria (sem biblioteca externa) carregada num
QWebEngineView. A comunicacao com o Python passa por QWebChannel: a pagina
avisa a selecao e pede geocodificacao, o Python reposiciona a vista.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from .. import config
from ..core.geo import BBox

log = logging.getLogger(__name__)

MAP_HTML = Path(__file__).with_name("map.html")


class MapBridge(QObject):
    """Objeto exposto ao JavaScript da pagina."""

    selectionChanged = Signal(object)  # BBox ou None
    polygonChanged = Signal(object)  # lista de (lat, lon) ou None
    searchRequested = Signal(str)
    pageReady = Signal()

    @Slot(float, float, float, float)
    def setSelection(self, south: float, west: float, north: float, east: float) -> None:
        if south == 0.0 and west == 0.0 and north == 0.0 and east == 0.0:
            self.polygonChanged.emit(None)
            self.selectionChanged.emit(None)
            return
        try:
            self.polygonChanged.emit(None)  # retangulo substitui o desenho
            self.selectionChanged.emit(BBox(south, west, north, east))
        except ValueError:  # retangulo degenerado (clique sem arrastar)
            self.selectionChanged.emit(None)

    @Slot(list)
    def setPolygon(self, flat: list) -> None:
        """Contorno desenhado a mao, como lista plana [lat, lon, lat, lon, ...].

        Plana porque o QWebChannel nao transporta lista de objetos do JS.
        """
        try:
            valores = [float(v) for v in flat]
        except (TypeError, ValueError):
            return
        if len(valores) < 6 or len(valores) % 2:
            return
        pontos = list(zip(valores[0::2], valores[1::2]))

        lats = [p[0] for p in pontos]
        lons = [p[1] for p in pontos]
        try:
            bbox = BBox(min(lats), min(lons), max(lats), max(lons))
        except ValueError:  # contorno degenerado
            return

        self.polygonChanged.emit(pontos)
        # A bbox continua valendo: os downloads sao sempre retangulares.
        self.selectionChanged.emit(bbox)

    @Slot(str)
    def search(self, text: str) -> None:
        self.searchRequested.emit(text)

    @Slot()
    def ready(self) -> None:
        self.pageReady.emit()


class MapView(QWebEngineView):
    """Vista do mapa com selecao retangular."""

    bboxSelected = Signal(object)
    polygonSelected = Signal(object)
    searchRequested = Signal(str)
    pageReady = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bridge = MapBridge(self)
        self.bridge.selectionChanged.connect(self.bboxSelected)
        self.bridge.polygonChanged.connect(self.polygonSelected)
        self.bridge.searchRequested.connect(self.searchRequested)
        self.bridge.pageReady.connect(self.pageReady)

        # A pagina e local (file://) mas os tiles vem da rede. Sem liberar isso,
        # o QtWebEngine bloqueia toda requisicao remota e o mapa fica vazio.
        settings = self.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)

        # A politica de uso dos tiles do OSM pede um User-Agent identificavel.
        profile: QWebEngineProfile = self.page().profile()
        profile.setHttpUserAgent(f"{profile.httpUserAgent()} {config.USER_AGENT}")

        channel = QWebChannel(self)
        channel.registerObject("bridge", self.bridge)
        self.page().setWebChannel(channel)
        # Carrega com a data do arquivo na URL.
        #
        # O QtWebEngine tem cache de disco proprio e ele vale tambem para
        # `file://`. Sem isto, editar `map.html` e reabrir o programa podia
        # continuar servindo a versao anterior - e a correcao simplesmente nao
        # chegava a tela, sem nenhum sinal de que estava velha.
        url = QUrl.fromLocalFile(str(MAP_HTML))
        try:
            url.setQuery(f"v={int(MAP_HTML.stat().st_mtime)}")
        except OSError:  # pragma: no cover - arquivo sempre existe
            pass
        self.setUrl(url)
        self.setMinimumWidth(420)

    # ------------------------------------------------------------ comandos JS

    def _call(self, expression: str) -> None:
        self.page().runJavaScript(expression)

    def go_to(self, lat: float, lon: float, zoom: Optional[int] = None) -> None:
        self._call(f"window.mapforge.goTo({lat}, {lon}, {json.dumps(zoom)})")

    def select_around_center(self, radius_m: float) -> None:
        self._call(f"window.mapforge.selectAroundCenter({radius_m})")

    def set_selection(self, bbox: BBox) -> None:
        self._call(
            "window.mapforge.setSelectionBox("
            f"{bbox.south}, {bbox.west}, {bbox.north}, {bbox.east})"
        )

    def set_status(self, text: str) -> None:
        self._call(f"window.mapforge.setStatus({json.dumps(text)})")
