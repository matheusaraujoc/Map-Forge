"""Renderizador offscreen em software (numpy).

Rasterizador com z-buffer e sombreamento facetado. Nao substitui o renderizador
em tempo real (ModernGL/engine), mas permite gerar uma imagem da cena em
qualquer maquina, sem contexto OpenGL - util para conferir o resultado e para
gerar miniaturas em lote.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..core.mesh import Scene

log = logging.getLogger(__name__)

# Luz direcional principal, em coordenadas de mundo (Z-up).
KEY_LIGHT = np.array([-0.45, -0.62, 0.65])
KEY_LIGHT /= np.linalg.norm(KEY_LIGHT)
FILL_LIGHT = np.array([0.55, 0.35, 0.25])
FILL_LIGHT /= np.linalg.norm(FILL_LIGHT)


@dataclass
class Camera:
    """Camera orbital: azimute/elevacao em graus, olhando para o centro da cena."""

    azimuth: float = 225.0
    elevation: float = 32.0
    perspective: bool = False
    fov: float = 42.0
    margin: float = 1.05

    def basis(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        az = np.radians(self.azimuth)
        el = np.radians(self.elevation)
        # Vetor do alvo para a camera.
        offset = np.array(
            [np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], dtype=float
        )
        forward = -offset
        world_up = np.array([0.0, 0.0, 1.0])
        right = np.cross(forward, world_up)
        norm = np.linalg.norm(right)
        if norm < 1e-6:  # camera exatamente no zenite
            right = np.array([1.0, 0.0, 0.0])
        else:
            right /= norm
        up = np.cross(right, forward)
        return right, up, forward


@dataclass
class _Batch:
    """Cena achatada em buffers unicos, prontos para projetar e rasterizar."""

    vertices: np.ndarray
    faces: np.ndarray
    colors: np.ndarray  # cor base por face
    face_uv: np.ndarray  # (m, 3, 2); zeros quando a face nao tem textura
    face_texture: np.ndarray  # (m,) indice em `textures`; -1 = sem textura
    textures: list[np.ndarray]


def _gather(scene: Scene) -> _Batch:
    verts_all, faces_all, colors_all, uv_all, tex_all = [], [], [], [], []
    textures: list[np.ndarray] = []
    offset = 0

    from ..generation.colliders import is_collider

    for name, group in scene.groups.items():
        if len(group.faces) == 0:
            continue
        # A malha de fisica existe para a engine, nao para a imagem: desenha-la
        # cobriria o mapa inteiro com caixas.
        if is_collider(name):
            continue
        material = group.material
        n_faces = len(group.faces)

        verts_all.append(group.vertices)
        faces_all.append(group.faces + offset)
        colors_all.append(np.tile(np.asarray(material.color, dtype=np.float32), (n_faces, 1)))

        if material.texture is not None and group.uv is not None:
            texture = np.asarray(material.texture.convert("RGB"), dtype=np.float32) / 255.0
            textures.append(texture)
            uv_all.append(group.uv[group.faces])
            tex_all.append(np.full(n_faces, len(textures) - 1, dtype=np.int64))
        else:
            uv_all.append(np.zeros((n_faces, 3, 2), dtype=np.float64))
            tex_all.append(np.full(n_faces, -1, dtype=np.int64))

        offset += len(group.vertices)

    if not faces_all:
        raise ValueError("cena vazia: nada para renderizar")

    return _Batch(
        vertices=np.concatenate(verts_all).astype(np.float64),
        faces=np.concatenate(faces_all).astype(np.int64),
        colors=np.concatenate(colors_all).astype(np.float32),
        face_uv=np.concatenate(uv_all).astype(np.float64),
        face_texture=np.concatenate(tex_all),
        textures=textures,
    )


def _intensity(normals: np.ndarray) -> np.ndarray:
    """Sombreamento facetado: luz principal + preenchimento + ambiente."""
    key = np.clip(normals @ KEY_LIGHT, 0.0, 1.0)
    fill = np.clip(normals @ FILL_LIGHT, 0.0, 1.0)
    return 0.42 + 0.50 * key + 0.16 * fill


def _rasterize(
    screen: np.ndarray,
    depth: np.ndarray,
    tri: np.ndarray,
    color: np.ndarray,
    width: int,
    height: int,
    uv: np.ndarray | None = None,
    texture: np.ndarray | None = None,
    intensity: float = 1.0,
) -> None:
    """Preenche um triangulo com teste de profundidade (coordenadas baricentricas)."""
    x0 = max(int(np.floor(tri[:, 0].min())), 0)
    x1 = min(int(np.ceil(tri[:, 0].max())) + 1, width)
    y0 = max(int(np.floor(tri[:, 1].min())), 0)
    y1 = min(int(np.ceil(tri[:, 1].max())) + 1, height)
    if x1 <= x0 or y1 <= y0:
        return

    ax, ay, az = tri[0]
    bx, by, bz = tri[1]
    cx, cy, cz = tri[2]
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if abs(area) < 1e-9:
        return

    ys, xs = np.mgrid[y0:y1, x0:x1]
    px = xs + 0.5
    py = ys + 0.5

    # Funcoes de aresta normalizadas: w_ab pesa C, w_bc pesa A, o resto pesa B.
    w_ab = ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / area
    w_bc = ((cx - bx) * (py - by) - (cy - by) * (px - bx)) / area
    w_ca = 1.0 - w_ab - w_bc
    inside = (w_ab >= 0) & (w_bc >= 0) & (w_ca >= 0)
    if not inside.any():
        return

    z = w_bc * az + w_ca * bz + w_ab * cz

    window_depth = depth[y0:y1, x0:x1]
    visible = inside & (z < window_depth)
    if not visible.any():
        return
    window_depth[visible] = z[visible]

    if texture is None or uv is None:
        screen[y0:y1, x0:x1][visible] = color
        return

    # Textura: interpola UV com os mesmos pesos baricentricos e amostra.
    u = w_bc[visible] * uv[0, 0] + w_ca[visible] * uv[1, 0] + w_ab[visible] * uv[2, 0]
    v = w_bc[visible] * uv[0, 1] + w_ca[visible] * uv[1, 1] + w_ab[visible] * uv[2, 1]
    tex_h, tex_w = texture.shape[:2]
    cols = np.clip((u * (tex_w - 1)).astype(np.int32), 0, tex_w - 1)
    rows = np.clip((v * (tex_h - 1)).astype(np.int32), 0, tex_h - 1)
    screen[y0:y1, x0:x1][visible] = np.clip(texture[rows, cols] * intensity, 0.0, 1.0)


def render_scene(
    scene: Scene,
    path: Optional[str | Path] = None,
    width: int = 1400,
    height: int = 900,
    camera: Optional[Camera] = None,
    supersample: int = 2,
    background: tuple[float, float, float] = (0.86, 0.90, 0.95),
    horizon: tuple[float, float, float] = (0.97, 0.96, 0.92),
) -> np.ndarray | Path:
    """Renderiza a cena e salva um PNG (ou devolve o array RGB se path for None)."""
    supersample = max(1, int(supersample))
    render_w, render_h = width * supersample, height * supersample

    batch = _gather(scene)
    vertices, faces, colors = batch.vertices, batch.faces, batch.colors
    cam = camera or Camera()
    right, up, forward = cam.basis()

    mins, maxs = vertices.min(axis=0), vertices.max(axis=0)
    center = (mins + maxs) / 2.0
    radius = float(np.linalg.norm(maxs - mins) / 2.0) or 1.0

    local = vertices - center
    u = local @ right
    v = local @ up
    w = local @ forward  # profundidade: maior = mais longe

    if cam.perspective:
        # Afasta a camera o suficiente para que nenhum vertice fique atras dela.
        half_fov = np.radians(cam.fov) / 2.0
        distance = radius / np.tan(half_fov) * 1.25
        w = w + distance
        sx = u / np.maximum(w, 1e-3)
        sy = v / np.maximum(w, 1e-3)
    else:
        sx, sy = u, v

    # Enquadramento comum: centraliza e amplia ate preencher a imagem.
    sx = sx - (sx.min() + sx.max()) / 2.0
    sy = sy - (sy.min() + sy.max()) / 2.0
    zoom = min(
        render_w / max(sx.max() - sx.min(), 1e-9),
        render_h / max(sy.max() - sy.min(), 1e-9),
    ) / max(cam.margin, 1e-3)

    screen_x = sx * zoom + render_w / 2.0
    screen_y = render_h / 2.0 - sy * zoom  # eixo Y da imagem cresce para baixo
    projected = np.column_stack([screen_x, screen_y, w])

    # Normais por face, em coordenadas de mundo (para a luz nao girar com a camera).
    tri_world = vertices[faces]
    normals = np.cross(
        tri_world[:, 1] - tri_world[:, 0], tri_world[:, 2] - tri_world[:, 0]
    )
    lengths = np.linalg.norm(normals, axis=1)
    valid = lengths > 1e-12
    normals[valid] /= lengths[valid][:, None]
    # Face virada para longe da camera: inverte a normal em vez de descartar,
    # porque a cena tem superficies abertas (chao, telhados, placas de janela).
    facing = normals @ forward
    normals[facing > 0] *= -1.0

    intensity = _intensity(normals)
    shaded = np.clip(colors * intensity[:, None], 0.0, 1.0)

    tri_screen = projected[faces]
    # Descarta triangulos totalmente fora da tela.
    min_xy = tri_screen[:, :, :2].min(axis=1)
    max_xy = tri_screen[:, :, :2].max(axis=1)
    on_screen = (
        valid
        & (max_xy[:, 0] >= 0)
        & (min_xy[:, 0] < render_w)
        & (max_xy[:, 1] >= 0)
        & (min_xy[:, 1] < render_h)
    )
    tri_screen = tri_screen[on_screen]
    shaded = shaded[on_screen]
    face_uv = batch.face_uv[on_screen]
    face_texture = batch.face_texture[on_screen]
    face_intensity = intensity[on_screen]

    # Fundo em degrade vertical (ceu -> horizonte).
    gradient = np.linspace(0.0, 1.0, render_h)[:, None]
    sky = np.array(background, dtype=np.float32)
    ground = np.array(horizon, dtype=np.float32)
    image = (sky[None, None, :] * (1 - gradient[:, :, None]) + ground[None, None, :] * gradient[:, :, None])
    image = np.repeat(image, render_w, axis=1).astype(np.float32)
    depth = np.full((render_h, render_w), np.inf, dtype=np.float64)

    log.info("Rasterizando %d triangulos em %dx%d", len(tri_screen), render_w, render_h)
    textures = batch.textures
    for i in range(len(tri_screen)):
        tex_id = face_texture[i]
        _rasterize(
            image,
            depth,
            tri_screen[i],
            shaded[i],
            render_w,
            render_h,
            uv=face_uv[i] if tex_id >= 0 else None,
            texture=textures[tex_id] if tex_id >= 0 else None,
            intensity=float(face_intensity[i]),
        )

    if supersample > 1:
        image = image.reshape(height, supersample, width, supersample, 3).mean(axis=(1, 3))

    rgb = (np.clip(image, 0.0, 1.0) * 255).astype(np.uint8)

    if path is None:
        return rgb

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_png(rgb, path)
    return path


def _save_png(rgb: np.ndarray, path: Path) -> None:
    try:
        from PIL import Image

        Image.fromarray(rgb).save(path)
        return
    except ImportError:  # pragma: no cover - fallback sem Pillow
        pass
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.imsave(path, rgb)
