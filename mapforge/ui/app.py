"""Janela principal do MapForge."""

from __future__ import annotations

import logging
import math
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..core.geo import BBox
from ..generation import GenerationSettings
from ..imagery.tiles import PROVIDERS, _custom_provider
from ..styles import list_styles
from .map_widget import MapView
from .viewport import Viewport, configure_surface_format
from .worker import GenerationWorker, GeocodeWorker, JobResult, RenderWorker

log = logging.getLogger(__name__)

DETAIL_LABELS = [("low", "Baixo (rapido)"), ("medium", "Medio"), ("high", "Alto")]


class PreviewPane(QLabel):
    """Mostra a imagem resultante, redimensionando com a janela."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(240)
        self.setFrameShape(QFrame.StyledPanel)
        self.setText("A previa aparece aqui depois de gerar.")
        self._pixmap: Optional[QPixmap] = None

    def show_image(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        self._pixmap = pixmap if not pixmap.isNull() else None
        self._rescale()

    def resizeEvent(self, event):  # noqa: N802 - assinatura do Qt
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        self.setPixmap(
            self._pixmap.scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MapForge - gerador de cidades 3D a partir do OpenStreetMap")
        self.resize(1500, 940)

        self.bbox: Optional[BBox] = None
        self.worker: Optional[GenerationWorker] = None
        self.geocoder: Optional[GeocodeWorker] = None
        self.renderer: Optional[RenderWorker] = None
        self.scene = None

        # A barra de status e criada antes dos widgets que emitem status nela.
        status_bar = self._build_status_bar()

        self.map = MapView()
        self.map.bboxSelected.connect(self._on_bbox)
        self.map.searchRequested.connect(self._on_search)

        self.viewport = Viewport()
        self.viewport.statusChanged.connect(self.status.setText)
        self.preview = PreviewPane()

        # Aba 1: escolher a regiao. Aba 2: navegar no resultado. Aba 3: imagens.
        self.tabs = QTabWidget()
        self.tabs.addTab(self.map, "Mapa")
        self.tabs.addTab(self.viewport, "3D")
        self.tabs.addTab(self.preview, "Imagem")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self._build_side_panel())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1050, 430])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)
        layout.addLayout(status_bar)

        self._update_region_label()

    # ------------------------------------------------------------------ painel

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        form_layout = QVBoxLayout(inner)
        form_layout.setContentsMargins(4, 4, 4, 4)

        # --- regiao ---
        region = QGroupBox("Regiao")
        region_form = QFormLayout(region)
        self.region_label = QLabel()
        self.region_label.setWordWrap(True)
        region_form.addRow(self.region_label)

        self.radius = QSpinBox()
        self.radius.setRange(50, 5000)
        self.radius.setValue(500)
        self.radius.setSingleStep(50)
        self.radius.setSuffix(" m")
        button_center = QPushButton("Selecionar em volta do centro do mapa")
        button_center.clicked.connect(
            lambda: self.map.select_around_center(float(self.radius.value()))
        )
        region_form.addRow("Raio", self.radius)
        region_form.addRow(button_center)
        form_layout.addWidget(region)

        # --- estilo ---
        style_box = QGroupBox("Estilo e detalhe")
        style_form = QFormLayout(style_box)
        self.style = QComboBox()
        for name, label in list_styles():
            self.style.addItem(label, name)
        style_form.addRow("Estilo", self.style)

        self.detail = QComboBox()
        for name, label in DETAIL_LABELS:
            self.detail.addItem(label, name)
        self.detail.setCurrentIndex(1)
        style_form.addRow("Detalhe", self.detail)

        seed_row = QHBoxLayout()
        self.seed = QSpinBox()
        self.seed.setRange(0, 999_999)
        self.seed.setValue(1)
        random_button = QPushButton("Sortear")
        random_button.clicked.connect(lambda: self.seed.setValue(random.randint(0, 999_999)))
        seed_row.addWidget(self.seed)
        seed_row.addWidget(random_button)
        style_form.addRow("Seed", seed_row)

        self.height_scale = QDoubleSpinBox()
        self.height_scale.setRange(0.2, 5.0)
        self.height_scale.setValue(1.0)
        self.height_scale.setSingleStep(0.1)
        style_form.addRow("Altura dos predios", self.height_scale)

        self.tree_density = QDoubleSpinBox()
        self.tree_density.setRange(0.1, 5.0)
        self.tree_density.setValue(1.0)
        self.tree_density.setSingleStep(0.1)
        style_form.addRow("Densidade de arvores", self.tree_density)
        form_layout.addWidget(style_box)

        # --- satelite ---
        sat_box = QGroupBox("Imagem de satelite")
        sat_form = QFormLayout(sat_box)
        self.satellite = QCheckBox("Tirar as cores reais do lugar da imagem")
        self.satellite.setToolTip(
            "Amostra telhados, terreno e areas verdes da foto de satelite.\n"
            "As superficies continuam chapadas: a foto informa a cor, nao vira textura."
        )
        self.satellite.toggled.connect(self._on_satellite_toggle)
        sat_form.addRow(self.satellite)

        self.provider = QComboBox()
        for provider in PROVIDERS.values():
            suffix = "" if not provider.requires_key() else (
                "" if provider.api_key() else "  (sem chave)"
            )
            self.provider.addItem(provider.label + suffix, provider.name)
        if _custom_provider() is not None:
            self.provider.addItem("Fonte personalizada", "custom")
        self.provider.setEnabled(False)
        sat_form.addRow("Fonte", self.provider)

        self.roof_blend = QDoubleSpinBox()
        self.roof_blend.setRange(0.0, 1.0)
        self.roof_blend.setValue(0.75)
        self.roof_blend.setSingleStep(0.05)
        self.roof_blend.setEnabled(False)
        self.roof_blend.setToolTip("0 = so a paleta do estilo, 1 = so a cor da foto")
        sat_form.addRow("Peso da foto nos telhados", self.roof_blend)

        self.area_blend = QDoubleSpinBox()
        self.area_blend.setRange(0.0, 1.0)
        self.area_blend.setValue(0.55)
        self.area_blend.setSingleStep(0.05)
        self.area_blend.setEnabled(False)
        sat_form.addRow("Peso nas areas e no terreno", self.area_blend)

        self.ground_texture = QCheckBox("Colar a foto como textura do terreno (experimental)")
        self.ground_texture.setToolTip(
            "Desligado por padrao: a foto crua costuma brigar com a leitura low-poly."
        )
        self.ground_texture.setEnabled(False)
        sat_form.addRow(self.ground_texture)
        form_layout.addWidget(sat_box)

        # --- fontes de edificios ---
        sources_box = QGroupBox("Fontes de edificios")
        sources_layout = QVBoxLayout(sources_box)
        self.extra_footprints = QCheckBox("Completar com contornos abertos (Microsoft)")
        self.extra_footprints.setToolTip(
            "O OpenStreetMap so tem predio onde alguem desenhou.\n"
            "Em cidade pequena isso costuma dar dois ou tres predios.\n"
            "Esta fonte traz contornos extraidos de imagem de satelite.\n\n"
            "O primeiro download de cada regiao e grande (10-30 MB) e fica em cache."
        )
        sources_layout.addWidget(self.extra_footprints)

        self.shadow_heights = QCheckBox("Estimar altura pela sombra na imagem")
        self.shadow_heights.setToolTip(
            "Mede a sombra projetada e converte em altura.\n"
            "Requer a imagem de satelite ligada."
        )
        self.shadow_heights.setEnabled(False)
        sources_layout.addWidget(self.shadow_heights)
        form_layout.addWidget(sources_box)

        # --- relevo ---
        relief_box = QGroupBox("Relevo")
        relief_form = QFormLayout(relief_box)
        self.elevation = QCheckBox("Terreno com relevo real")
        self.elevation.setToolTip(
            "Baixa o modelo digital de elevacao e assenta ruas, predios,\n"
            "agua e vegetacao sobre o terreno."
        )
        self.elevation.toggled.connect(lambda on: self.exaggeration.setEnabled(on))
        relief_form.addRow(self.elevation)

        self.exaggeration = QDoubleSpinBox()
        self.exaggeration.setRange(0.2, 5.0)
        self.exaggeration.setValue(1.0)
        self.exaggeration.setSingleStep(0.1)
        self.exaggeration.setEnabled(False)
        self.exaggeration.setToolTip("1.0 mantem a escala real do desnivel.")
        relief_form.addRow("Exagero vertical", self.exaggeration)
        form_layout.addWidget(relief_box)

        # --- camadas ---
        layers_box = QGroupBox("Camadas")
        layers_layout = QVBoxLayout(layers_box)
        self.layer_boxes: dict[str, QCheckBox] = {}
        for key, label in [
            ("terrain", "Terreno"),
            ("roads", "Ruas"),
            ("sidewalks", "Calcadas e meio-fio"),
            ("buildings", "Edificios"),
            ("windows", "Janelas nas fachadas"),
            ("water", "Agua"),
            ("vegetation", "Vegetacao"),
        ]:
            box = QCheckBox(label)
            box.setChecked(True)
            self.layer_boxes[key] = box
            layers_layout.addWidget(box)
        form_layout.addWidget(layers_box)

        form_layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # --- acoes ---
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("Previa 2D")
        self.preview_button.clicked.connect(lambda: self._start("preview"))
        self.generate_button = QPushButton("Gerar 3D")
        self.generate_button.setDefault(True)
        self.generate_button.clicked.connect(lambda: self._start("generate"))
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.generate_button)
        outer.addLayout(buttons)

        secondary = QHBoxLayout()
        self.render_button = QPushButton("Salvar render PNG")
        self.render_button.setEnabled(False)
        self.render_button.setToolTip("Usa o angulo atual do viewport 3D.")
        self.render_button.clicked.connect(self._save_render)
        self.reset_view_button = QPushButton("Enquadrar")
        self.reset_view_button.clicked.connect(lambda: self.viewport.reset_view())
        self.open_button = QPushButton("Abrir pasta")
        self.open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.OUTPUT_DIR)))
        )
        secondary.addWidget(self.render_button)
        secondary.addWidget(self.reset_view_button)
        secondary.addWidget(self.open_button)
        outer.addLayout(secondary)
        return panel

    def _build_status_bar(self):
        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(14)
        self.status = QLabel("Selecione uma regiao no mapa para comecar.")
        row.addWidget(self.status, 1)
        row.addWidget(self.progress, 1)
        return row

    # ------------------------------------------------------------------ eventos

    def _on_satellite_toggle(self, checked: bool) -> None:
        for widget in (self.provider, self.roof_blend, self.area_blend, self.ground_texture):
            widget.setEnabled(checked)
        # A estimativa por sombra precisa da imagem.
        self.shadow_heights.setEnabled(checked)
        if not checked:
            self.shadow_heights.setChecked(False)

    def _on_bbox(self, bbox: Optional[BBox]) -> None:
        self.bbox = bbox
        self._update_region_label()

    def _on_search(self, text: str) -> None:
        if not text.strip():
            return
        self.geocoder = GeocodeWorker(text, self)
        self.geocoder.found.connect(self._on_geocoded)
        self.geocoder.failed.connect(lambda msg: self.map.set_status(f"Busca falhou: {msg}"))
        self.geocoder.start()

    def _on_geocoded(self, lat: float, lon: float, label: str) -> None:
        self.map.go_to(lat, lon, 16)
        self.map.set_status(label)

    def _update_region_label(self) -> None:
        if self.bbox is None:
            self.region_label.setText("<i>Nenhuma regiao selecionada.</i>")
            return
        self.region_label.setText(
            f"<b>{self.bbox.width_m:.0f} x {self.bbox.height_m:.0f} m</b> "
            f"({self.bbox.area_km2:.2f} km2)<br>"
            f"<span style='color:gray'>{self.bbox.key()}</span>"
        )

    # ---------------------------------------------------------------- execucao

    def _settings(self) -> GenerationSettings:
        layers = {key: box.isChecked() for key, box in self.layer_boxes.items()}
        return GenerationSettings(
            seed=self.seed.value(),
            style=self.style.currentData(),
            detail=self.detail.currentData(),
            terrain=layers["terrain"],
            roads=layers["roads"],
            sidewalks=layers["sidewalks"],
            buildings=layers["buildings"],
            water=layers["water"],
            vegetation=layers["vegetation"],
            windows=None if layers["windows"] else False,
            tree_density=self.tree_density.value(),
            building_height_scale=self.height_scale.value(),
            satellite=self.satellite.isChecked(),
            satellite_provider=self.provider.currentData() or "esri",
            roof_blend=self.roof_blend.value(),
            area_blend=self.area_blend.value(),
            ground_texture=self.ground_texture.isChecked(),
            elevation=self.elevation.isChecked(),
            elevation_exaggeration=self.exaggeration.value(),
            extra_footprints=self.extra_footprints.isChecked(),
            shadow_heights=self.shadow_heights.isChecked(),
        )

    def _start(self, kind: str) -> None:
        if self.bbox is None:
            QMessageBox.information(
                self,
                "Nenhuma regiao",
                "Selecione uma area no mapa primeiro.\n\n"
                "Clique em 'Selecionar area' e arraste, ou use o botao "
                "'Selecionar em volta do centro do mapa'.",
            )
            return
        if self.bbox.area_km2 > config.MAX_AREA_KM2:
            QMessageBox.warning(
                self,
                "Area grande demais",
                f"A selecao tem {self.bbox.area_km2:.1f} km2, acima do limite de "
                f"{config.MAX_AREA_KM2} km2.\nReduza a area selecionada.",
            )
            return
        if self.worker is not None and self.worker.isRunning():
            return

        settings = self._settings()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = config.OUTPUT_DIR / f"mapa_{settings.style}_{stamp}.glb"
        config.ensure_dirs()

        self.worker = GenerationWorker(
            self.bbox,
            settings,
            output,
            kind=kind,
            parent=self,
        )
        self.worker.progressed.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_thread_finished)

        self._set_busy(True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.preview_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy)
        if not busy:
            self.progress.setValue(0)

    def _on_progress(self, message: str, fraction: float) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.status.setText(message)

    def _on_done(self, result: JobResult) -> None:
        if result.image is not None and result.image.exists():
            self.preview.show_image(result.image)

        counts = ", ".join(f"{k}={v}" for k, v in result.summary.items() if v)

        if result.kind == "preview":
            self.tabs.setCurrentWidget(self.preview)
            self.status.setText(f"Planta 2D em {result.seconds:.1f}s  |  {counts}")
        elif result.kind == "render":
            self.tabs.setCurrentWidget(self.preview)
            self.status.setText(f"Render salvo em {result.seconds:.1f}s: {result.image}")
        else:
            self.scene = result.scene
            self.viewport.set_scene(result.scene)
            self.tabs.setCurrentWidget(self.viewport)
            self.render_button.setEnabled(result.scene is not None)
            self.status.setText(
                f"{result.triangles:,} triangulos em {result.seconds:.1f}s  |  "
                f"{result.model.name if result.model else ''}  |  {counts}"
            )

        if result.note:
            self.map.set_status(result.note)

    def _save_render(self) -> None:
        if self.scene is None:
            return
        if self.renderer is not None and self.renderer.isRunning():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = config.OUTPUT_DIR / f"render_{stamp}.png"

        camera = self.viewport.camera
        self.renderer = RenderWorker(
            self.scene,
            output,
            azimuth=math.degrees(camera.azimuth),
            elevation=math.degrees(camera.elevation),
            parent=self,
        )
        self.renderer.succeeded.connect(self._on_done)
        self.renderer.failed.connect(self._on_failed)
        self.renderer.finished.connect(lambda: self.render_button.setEnabled(True))
        self.render_button.setEnabled(False)
        self.status.setText("Renderizando PNG...")
        self.renderer.start()

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Erro: {message}")
        if message != "cancelado":
            QMessageBox.critical(self, "Falha na geracao", message)

    def _on_thread_finished(self) -> None:
        self._set_busy(False)

    def closeEvent(self, event):  # noqa: N802 - assinatura do Qt
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(event)


def run(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(name)s: %(message)s")
    config.ensure_dirs()
    # Precisa vir antes de existir qualquer widget OpenGL.
    configure_surface_format()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("MapForge")
    window = MainWindow()
    window.show()
    return app.exec()
