"""Threads de trabalho: geracao e busca ficam fora da thread da interface."""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..core.geo import BBox
from ..data import Cache, geocode
from ..generation import GenerationSettings

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    """O que a interface precisa mostrar quando o trabalho termina."""

    kind: str  # 'preview' | 'generate' | 'render'
    summary: dict
    image: Optional[Path] = None
    model: Optional[Path] = None
    scene: object | None = None  # Scene, para o viewport 3D
    triangles: int = 0
    vertices: int = 0
    seconds: float = 0.0
    note: str = ""


class GeocodeWorker(QThread):
    """Busca de lugar por nome (Nominatim)."""

    found = Signal(float, float, str)
    failed = Signal(str)

    def __init__(self, query: str, parent=None):
        super().__init__(parent)
        self.query = query

    def run(self) -> None:
        try:
            lat, lon, label = geocode(self.query)
            self.found.emit(lat, lon, label)
        except Exception as exc:  # noqa: BLE001 - vira mensagem na interface
            self.failed.emit(str(exc))


class GenerationWorker(QThread):
    """Executa o pipeline sem travar a interface."""

    progressed = Signal(str, float)
    succeeded = Signal(object)  # JobResult
    failed = Signal(str)

    def __init__(
        self,
        bbox: BBox,
        settings: GenerationSettings,
        output: Path,
        kind: str = "generate",
        render_camera: Optional[dict] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.bbox = bbox
        self.settings = settings
        self.output = output
        self.kind = kind
        self.render_camera = render_camera or {}
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _progress(self, message: str, fraction: float) -> None:
        if self._cancelled:
            raise InterruptedError("cancelado pelo usuario")
        self.progressed.emit(message, fraction)

    def run(self) -> None:
        try:
            if self.kind == "preview":
                self._run_preview()
            else:
                self._run_generate()
        except InterruptedError:
            self.failed.emit("cancelado")
        except Exception as exc:  # noqa: BLE001 - a interface mostra a mensagem
            log.debug("Falha no worker:\n%s", traceback.format_exc())
            self.failed.emit(str(exc))

    # ------------------------------------------------------------------ modos

    def _run_preview(self) -> None:
        import time

        from ..pipeline import load_map
        from ..preview import render_preview

        started = time.perf_counter()
        with Cache() as cache:
            map_data, _ = load_map(self.bbox, cache, progress=self._progress)

        self._progress("desenhando planta 2D", 0.85)
        path = self.output.with_suffix(".png")
        render_preview(map_data, path, style_name=self.settings.style)

        self.succeeded.emit(
            JobResult(
                kind="preview",
                summary=map_data.summary(),
                image=Path(path),
                seconds=time.perf_counter() - started,
            )
        )

    def _run_generate(self) -> None:
        import time

        from ..pipeline import generate

        started = time.perf_counter()
        with Cache() as cache:
            result = generate(
                self.bbox,
                settings=self.settings,
                output=self.output,
                cache=cache,
                progress=self._progress,
            )

        # Sem render offscreen aqui: quem mostra o resultado e o viewport, que
        # recebe a cena direto. O PNG so e gerado quando o usuario pede.
        note = ""
        imagery = result.scene.metadata.get("imagery")
        if imagery:
            note = (
                f"satelite {imagery['provider']} z{imagery['zoom']} "
                f"({imagery['meters_per_pixel']} m/px) - {imagery['attribution']}"
            )
        elevation = result.scene.metadata.get("elevation")
        if elevation:
            note = (note + "  |  " if note else "") + (
                f"relevo {elevation['provider']} z{elevation['zoom']}, "
                f"{elevation['min_m']:.0f}-{elevation['max_m']:.0f} m"
            )

        self.succeeded.emit(
            JobResult(
                kind="generate",
                summary=result.map_data.summary(),
                model=result.output,
                scene=result.scene,
                triangles=result.scene.triangle_count,
                vertices=result.scene.vertex_count,
                seconds=time.perf_counter() - started,
                note=note,
            )
        )


class RenderWorker(QThread):
    """Gera o PNG offscreen sob demanda, com o angulo atual do viewport."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, scene, output: Path, azimuth: float, elevation: float,
                 perspective: bool = False, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.output = output
        self.azimuth = azimuth
        self.elevation = elevation
        self.perspective = perspective

    def run(self) -> None:
        import time

        from ..render import Camera, render_scene

        try:
            started = time.perf_counter()
            render_scene(
                self.scene,
                self.output,
                width=1500,
                height=950,
                camera=Camera(
                    azimuth=self.azimuth,
                    elevation=self.elevation,
                    perspective=self.perspective,
                ),
                supersample=2,
            )
            self.succeeded.emit(
                JobResult(
                    kind="render",
                    summary={},
                    image=self.output,
                    seconds=time.perf_counter() - started,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
