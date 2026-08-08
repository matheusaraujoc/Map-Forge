"""Viewport 3D interativo (ModernGL dentro de um QOpenGLWidget).

Desenha a mesma Scene que vai para o GLB, com a mesma iluminacao do renderizador
offscreen, para que a previa e o arquivo exportado combinem.

A normal e calculada no fragment shader por derivada da posicao em mundo. Isso
da o sombreamento facetado do low-poly sem precisar de atributo de normal e sem
triplicar os vertices para quebrar o compartilhamento - o que importa numa cena
de centenas de milhares de triangulos.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ..core.mesh import Scene

log = logging.getLogger(__name__)

VERTEX_SHADER = """
#version 330
uniform mat4 mvp;
in vec3 in_position;
in vec2 in_uv;
out vec3 world_pos;
out vec2 uv;
void main() {
    world_pos = in_position;
    uv = in_uv;
    gl_Position = mvp * vec4(in_position, 1.0);
}
"""

FRAGMENT_SHADER = """
#version 330
uniform vec3 base_color;
uniform vec3 key_dir;
uniform vec3 fill_dir;
uniform float use_texture;
uniform sampler2D color_map;

in vec3 world_pos;
in vec2 uv;
out vec4 frag_color;

void main() {
    // Normal exata da face a partir das derivadas de tela: sombreamento
    // facetado sem atributo de normal.
    vec3 normal = normalize(cross(dFdx(world_pos), dFdy(world_pos)));
    if (!gl_FrontFacing) normal = -normal;

    float key  = max(dot(normal, key_dir), 0.0);
    float fill = max(dot(normal, fill_dir), 0.0);
    float intensity = 0.42 + 0.50 * key + 0.16 * fill;

    vec3 albedo = base_color;
    if (use_texture > 0.5) albedo = texture(color_map, uv).rgb;
    frag_color = vec4(clamp(albedo * intensity, 0.0, 1.0), 1.0);
}
"""


def configure_surface_format() -> None:
    """Prepara o processo para ter QOpenGLWidget e QtWebEngine na mesma janela.

    Duas coisas precisam acontecer antes de existir qualquer widget:

    1. Pedir um contexto OpenGL 3.3 core, que e o que o viewport usa.
    2. Dizer ao Qt Quick para usar OpenGL tambem. No Windows o Quick escolhe
       Direct3D por padrao; como o QOpenGLWidget forca a composicao da janela
       para OpenGL, o QQuickWidget de dentro do QtWebEngine nao consegue um RHI
       compativel e o mapa fica preto ("Failed to get a QRhi").
    """
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)

    try:
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
    except Exception as exc:  # noqa: BLE001 - sem Quick, nao ha o que alinhar
        log.debug("Nao foi possivel alinhar o backend do Qt Quick: %s", exc)


class OrbitCamera:
    """Camera orbital em torno de um alvo."""

    def __init__(self):
        self.target = np.zeros(3)
        self.distance = 800.0
        self.azimuth = math.radians(225.0)
        self.elevation = math.radians(32.0)
        self.fov = 45.0
        self.min_elevation = math.radians(2.0)
        self.max_elevation = math.radians(88.0)

    def eye(self) -> np.ndarray:
        horizontal = math.cos(self.elevation) * self.distance
        return self.target + np.array(
            [
                horizontal * math.cos(self.azimuth),
                horizontal * math.sin(self.azimuth),
                math.sin(self.elevation) * self.distance,
            ]
        )

    def orbit(self, d_azimuth: float, d_elevation: float) -> None:
        self.azimuth += d_azimuth
        self.elevation = min(
            self.max_elevation, max(self.min_elevation, self.elevation + d_elevation)
        )

    def zoom(self, factor: float) -> None:
        self.distance = float(np.clip(self.distance * factor, 5.0, 60_000.0))

    def pan(self, dx: float, dy: float) -> None:
        """Desloca o alvo no plano da tela, na escala da distancia atual."""
        forward = self.target - self.eye()
        forward /= np.linalg.norm(forward) or 1.0
        right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
        right /= np.linalg.norm(right) or 1.0
        up = np.cross(right, forward)
        scale = self.distance * 0.0015
        self.target = self.target + (-right * dx + up * dy) * scale

    def frame(self, bounds: np.ndarray) -> None:
        """Enquadra uma caixa (2x3: minimos e maximos)."""
        self.target = (bounds[0] + bounds[1]) / 2.0
        radius = float(np.linalg.norm(bounds[1] - bounds[0]) / 2.0) or 100.0
        self.distance = radius / math.tan(math.radians(self.fov) / 2.0) * 1.15

    def matrix(self, aspect: float) -> np.ndarray:
        eye = self.eye()
        forward = self.target - eye
        forward /= np.linalg.norm(forward) or 1.0
        right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
        norm = np.linalg.norm(right)
        right = right / norm if norm > 1e-6 else np.array([1.0, 0.0, 0.0])
        up = np.cross(right, forward)

        view = np.eye(4, dtype=np.float32)
        view[0, :3], view[1, :3], view[2, :3] = right, up, -forward
        view[:3, 3] = -view[:3, :3] @ eye

        near = max(self.distance * 0.001, 0.1)
        far = self.distance * 12.0 + 5000.0
        f = 1.0 / math.tan(math.radians(self.fov) / 2.0)
        projection = np.zeros((4, 4), dtype=np.float32)
        projection[0, 0] = f / aspect
        projection[1, 1] = f
        projection[2, 2] = (far + near) / (near - far)
        projection[2, 3] = (2 * far * near) / (near - far)
        projection[3, 2] = -1.0
        return projection @ view


class Viewport(QOpenGLWidget):
    """Widget de navegacao 3D."""

    statusChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self.setFocusPolicy(Qt.StrongFocus)

        self.camera = OrbitCamera()
        self._scene: Optional[Scene] = None
        self._pending: Optional[Scene] = None
        self._ctx = None
        self._program = None
        self._fbo = None
        self._fbo_id = -1
        self._batches: list[dict] = []
        self._textures: list = []
        self._last_mouse = None
        self._background = (0.86, 0.90, 0.95)

        self._key_dir = np.array([-0.45, -0.62, 0.65], dtype="f4")
        self._key_dir /= np.linalg.norm(self._key_dir)
        self._fill_dir = np.array([0.55, 0.35, 0.25], dtype="f4")
        self._fill_dir /= np.linalg.norm(self._fill_dir)

    # -------------------------------------------------------------- API publica

    def set_scene(self, scene: Optional[Scene]) -> None:
        """Troca a cena mostrada. Pode ser chamado antes do contexto existir."""
        self._pending = scene
        if self._ctx is not None:
            self._upload(scene)
        self.update()

    def reset_view(self) -> None:
        if self._scene is not None:
            self.camera.frame(self._scene.bounds())
        self.update()

    def set_angles(self, azimuth_deg: float, elevation_deg: float) -> None:
        self.camera.azimuth = math.radians(azimuth_deg)
        self.camera.elevation = math.radians(
            min(max(elevation_deg, 2.0), 88.0)
        )
        self.update()

    # ------------------------------------------------------------------ OpenGL

    def initializeGL(self) -> None:  # noqa: N802 - assinatura do Qt
        import moderngl

        self._ctx = moderngl.create_context()
        self._ctx.enable(moderngl.DEPTH_TEST)
        self._program = self._ctx.program(
            vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER
        )
        self._program["key_dir"].value = tuple(self._key_dir.tolist())
        self._program["fill_dir"].value = tuple(self._fill_dir.tolist())
        if self._pending is not None:
            self._upload(self._pending)

    def _release(self) -> None:
        for batch in self._batches:
            batch["vao"].release()
            batch["vbo"].release()
            batch["ibo"].release()
        for texture in self._textures:
            texture.release()
        self._batches = []
        self._textures = []

    def _upload(self, scene: Optional[Scene]) -> None:
        """Envia a cena para a GPU: um buffer por material."""
        import moderngl

        self.makeCurrent()
        try:
            self._release()
            self._scene = scene
            if scene is None or not scene.groups:
                return

            for name, group in scene.groups.items():
                if len(group.faces) == 0:
                    continue
                uv = group.uv
                if uv is None:
                    uv = np.zeros((len(group.vertices), 2), dtype="f4")
                data = np.hstack(
                    [group.vertices.astype("f4"), np.asarray(uv, dtype="f4")]
                ).astype("f4")

                vbo = self._ctx.buffer(data.tobytes())
                ibo = self._ctx.buffer(group.faces.astype("i4").tobytes())
                vao = self._ctx.vertex_array(
                    self._program,
                    [(vbo, "3f 2f", "in_position", "in_uv")],
                    index_buffer=ibo,
                    index_element_size=4,
                )

                texture = None
                image = group.material.texture
                if image is not None:
                    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
                    texture = self._ctx.texture((rgb.shape[1], rgb.shape[0]), 3, rgb.tobytes())
                    texture.build_mipmaps()
                    texture.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
                    self._textures.append(texture)

                self._batches.append(
                    {
                        "name": name,
                        "vao": vao,
                        "vbo": vbo,
                        "ibo": ibo,
                        "color": tuple(float(c) for c in group.material.color),
                        "texture": texture,
                    }
                )

            self.camera.frame(scene.bounds())
            self.statusChanged.emit(
                f"{scene.triangle_count:,} triangulos, {len(self._batches)} materiais"
            )
        finally:
            self.doneCurrent()

    def _target_framebuffer(self):
        """O QOpenGLWidget desenha num FBO proprio, nao no 0.

        Sem apontar o ModernGL para ele, tudo vai para um framebuffer que
        ninguem apresenta e a tela sai so com a cor de fundo.
        """
        fbo_id = self.defaultFramebufferObject()
        if self._fbo is None or self._fbo_id != fbo_id:
            self._fbo = self._ctx.detect_framebuffer(fbo_id)
            self._fbo_id = fbo_id
        return self._fbo

    def paintGL(self) -> None:  # noqa: N802 - assinatura do Qt
        if self._ctx is None:
            return

        ratio = self.devicePixelRatioF()
        width = max(int(self.width() * ratio), 1)
        height = max(int(self.height() * ratio), 1)

        fbo = self._target_framebuffer()
        fbo.use()
        fbo.viewport = (0, 0, width, height)
        fbo.clear(*self._background, 1.0)
        if not self._batches:
            return

        aspect = max(self.width(), 1) / max(self.height(), 1)
        mvp = self.camera.matrix(aspect).astype("f4")
        self._program["mvp"].write(np.ascontiguousarray(mvp.T).tobytes())

        for batch in self._batches:
            self._program["base_color"].value = batch["color"]
            has_texture = batch["texture"] is not None
            self._program["use_texture"].value = 1.0 if has_texture else 0.0
            if has_texture:
                batch["texture"].use(0)
                self._program["color_map"].value = 0
            batch["vao"].render()

    def resizeGL(self, width: int, height: int) -> None:  # noqa: N802
        # O FBO do widget e recriado no redimensionamento; forca nova deteccao.
        self._fbo = None
        self._fbo_id = -1

    # ------------------------------------------------------------------- mouse

    def mousePressEvent(self, event):  # noqa: N802
        self._last_mouse = event.position()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._last_mouse is None:
            return
        delta = event.position() - self._last_mouse
        self._last_mouse = event.position()

        if event.buttons() & Qt.LeftButton:
            self.camera.orbit(-delta.x() * 0.008, delta.y() * 0.008)
        elif event.buttons() & (Qt.MiddleButton | Qt.RightButton):
            self.camera.pan(delta.x(), delta.y())
        else:
            return
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._last_mouse = None

    def wheelEvent(self, event):  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        self.camera.zoom(0.86**steps)
        self.update()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key_F, Qt.Key_Home):
            self.reset_view()
        else:
            super().keyPressEvent(event)
