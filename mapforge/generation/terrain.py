"""Terreno e superficies de uso do solo.

Sem modelo de elevacao o terreno e um retangulo plano. Com ele, vira uma malha
regular cujas alturas vem do DEM - e, mais importante, um *campo de altura
consultavel*: ruas, calcadas, predios, agua e vegetacao perguntam a altura do
chao antes de se posicionar.

O campo interpola exatamente sobre os mesmos triangulos da malha do terreno.
Isso importa: se a consulta usasse bilinear e a malha fosse linear por
triangulo, as ruas afundariam ou flutuariam alguns centimetros nas encostas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from ..core.mesh import MeshBuilder
from . import layers
from .context import GenerationContext

log = logging.getLogger(__name__)

# Passo da malha do terreno (metros) por nivel de detalhe.
GRID_STEP = {"low": 18.0, "medium": 10.0, "high": 6.0}


@dataclass
class TerrainField:
    """Malha regular de alturas em coordenadas locais, com consulta exata."""

    xs: np.ndarray  # colunas, oeste -> leste
    ys: np.ndarray  # linhas, sul -> norte
    z: np.ndarray  # (len(ys), len(xs))
    base: float = 0.0  # altitude real correspondente a z = 0

    @classmethod
    def from_grid(
        cls,
        bbox,
        grid,
        step: float,
        exaggeration: float = 1.0,
    ) -> "TerrainField":
        half_w, half_h = bbox.width_m / 2.0, bbox.height_m / 2.0
        nx = max(int(bbox.width_m / step) + 1, 2)
        ny = max(int(bbox.height_m / step) + 1, 2)
        xs = np.linspace(-half_w, half_w, nx)
        ys = np.linspace(-half_h, half_h, ny)

        mesh_x, mesh_y = np.meshgrid(xs, ys)
        heights = grid.sample(mesh_x.ravel(), mesh_y.ravel()).reshape(ny, nx)

        base = float(np.min(heights))
        return cls(xs=xs, ys=ys, z=(heights - base) * exaggeration, base=base)

    # ------------------------------------------------------------------ consulta

    def height(self, x, y) -> np.ndarray:
        """Altura do terreno, linear por triangulo (igual a malha desenhada)."""
        x = np.atleast_1d(np.asarray(x, dtype=np.float64))
        y = np.atleast_1d(np.asarray(y, dtype=np.float64))

        nx, ny = len(self.xs), len(self.ys)
        dx = self.xs[1] - self.xs[0]
        dy = self.ys[1] - self.ys[0]

        fx = np.clip((x - self.xs[0]) / dx, 0, nx - 1 - 1e-9)
        fy = np.clip((y - self.ys[0]) / dy, 0, ny - 1 - 1e-9)
        i = fx.astype(np.int64)
        j = fy.astype(np.int64)
        u = fx - i
        v = fy - j

        z00 = self.z[j, i]
        z10 = self.z[j, np.minimum(i + 1, nx - 1)]
        z01 = self.z[np.minimum(j + 1, ny - 1), i]
        z11 = self.z[np.minimum(j + 1, ny - 1), np.minimum(i + 1, nx - 1)]

        # A celula e cortada pela diagonal (0,0)-(1,1); cada metade e um plano.
        lower = v <= u
        return np.where(
            lower,
            z00 + (z10 - z00) * u + (z11 - z10) * v,
            z00 + (z11 - z01) * u + (z01 - z00) * v,
        )

    def at(self, x: float, y: float) -> float:
        return float(self.height(x, y)[0])

    def min_over(self, geometry) -> float:
        """Menor altura sob um poligono (amostra o contorno e o ponto interno)."""
        coords = np.asarray(geometry.exterior.coords, dtype=np.float64)
        point = geometry.representative_point()
        samples = self.height(
            np.append(coords[:, 0], point.x), np.append(coords[:, 1], point.y)
        )
        return float(samples.min())

    def level_over(self, geometry, percentile: float = 20.0) -> float:
        """Altura representativa de uma area - usada como nivel de agua."""
        minx, miny, maxx, maxy = geometry.bounds
        xs = np.linspace(minx, maxx, 12)
        ys = np.linspace(miny, maxy, 12)
        mesh_x, mesh_y = np.meshgrid(xs, ys)
        return float(np.percentile(self.height(mesh_x.ravel(), mesh_y.ravel()), percentile))

    # ------------------------------------------------------------------ escavar

    def carve(self, geometry, depth: float, level: Optional[float] = None) -> None:
        """Rebaixa os vertices da grade dentro de um poligono.

        E assim que o leito do rio aparece: o proprio terreno afunda, em vez de
        a agua ficar por cima do relevo.
        """
        from shapely import contains_xy

        if geometry is None or geometry.is_empty:
            return
        mesh_x, mesh_y = np.meshgrid(self.xs, self.ys)
        inside = contains_xy(geometry, mesh_x.ravel(), mesh_y.ravel()).reshape(self.z.shape)
        if not inside.any():
            return
        target = (self.level_over(geometry) if level is None else level) - depth
        self.z[inside] = np.minimum(self.z[inside], target)

    # -------------------------------------------------------------------- malha

    def mesh(self) -> tuple[np.ndarray, np.ndarray]:
        nx, ny = len(self.xs), len(self.ys)
        mesh_x, mesh_y = np.meshgrid(self.xs, self.ys)
        vertices = np.column_stack([mesh_x.ravel(), mesh_y.ravel(), self.z.ravel()])

        i, j = np.meshgrid(np.arange(nx - 1), np.arange(ny - 1))
        i, j = i.ravel(), j.ravel()
        v00 = j * nx + i
        v10 = v00 + 1
        v01 = v00 + nx
        v11 = v01 + 1
        # Mesma diagonal usada em height(), no sentido anti-horario visto de cima.
        faces = np.concatenate(
            [np.column_stack([v00, v10, v11]), np.column_stack([v00, v11, v01])], axis=0
        )
        return vertices, faces

    @property
    def relief(self) -> float:
        return float(self.z.max() - self.z.min())


# ------------------------------------------------------------------- geradores


def _ground_material(ctx: GenerationContext):
    """Material do terreno.

    Tres casos, do mais comum ao mais raro: cor do estilo; cor chapada amostrada
    da imagem (padrao quando o satelite esta ligado); e a foto como textura, que
    fica desligada por padrao porque nao combina com o resto da cena low-poly.
    """
    imagery = ctx.imagery
    if imagery is None:
        return ctx.palette.ground, False

    if not ctx.settings.ground_texture:
        return (ctx.ground_material or ctx.palette.ground), False

    from ..core.mesh import Material
    from ..imagery.sampling import build_ground_texture

    texture = build_ground_texture(
        imagery,
        brightness=ctx.settings.texture_brightness,
        saturation=ctx.settings.texture_saturation,
    )
    # Cor base branca: no glTF ela multiplica a textura.
    material = Material(name="ground_satellite", color=(1.0, 1.0, 1.0), texture=texture)
    return material, True


def generate_terrain(builder: MeshBuilder, ctx: GenerationContext, water_union=None) -> None:
    """Plano (ou relevo) cobrindo toda a bbox."""
    if not ctx.settings.terrain:
        return

    half_w, half_h = ctx.half_size
    material, textured = _ground_material(ctx)
    uv_bounds = (-half_w, -half_h, half_w, half_h) if textured else None

    field = ctx.terrain
    if field is not None:
        vertices, faces = field.mesh()
        uv = None
        if textured:
            uv = np.column_stack(
                [
                    (vertices[:, 0] + half_w) / (2 * half_w),
                    (half_h - vertices[:, 1]) / (2 * half_h),
                ]
            )
        builder.add_mesh(material, vertices, faces, uv)
        # Saia lateral: fecha o volume para a cena nao parecer um recorte de papel.
        _add_skirt(builder, ctx, field)
        return

    ground = box(-half_w, -half_h, half_w, half_h)
    if water_union is not None and not water_union.is_empty:
        try:
            ground = ground.difference(water_union)
        except Exception:  # noqa: BLE001 - mantem o plano inteiro se a diferenca falhar
            pass
    if not ground.is_empty:
        builder.add_flat(material, ground, layers.Z_GROUND, uv_bounds=uv_bounds)


def _add_skirt(builder: MeshBuilder, ctx: GenerationContext, field: TerrainField) -> None:
    """Paredes verticais na borda do terreno, descendo ate abaixo do ponto mais baixo."""
    bottom = float(field.z.min()) - max(field.relief * 0.15, 8.0)
    material = ctx.palette.bank

    edges = [
        (field.xs, np.full(len(field.xs), field.ys[0]), field.z[0, :], False),
        (field.xs, np.full(len(field.xs), field.ys[-1]), field.z[-1, :], True),
        (np.full(len(field.ys), field.xs[0]), field.ys, field.z[:, 0], True),
        (np.full(len(field.ys), field.xs[-1]), field.ys, field.z[:, -1], False),
    ]
    for xs, ys, zs, flip in edges:
        n = len(xs)
        top = np.column_stack([xs, ys, zs])
        base = np.column_stack([xs, ys, np.full(n, bottom)])
        vertices = np.concatenate([top, base], axis=0)
        idx = np.arange(n - 1)
        faces = np.concatenate(
            [
                np.column_stack([idx, idx + n, idx + n + 1]),
                np.column_stack([idx, idx + n + 1, idx + 1]),
            ],
            axis=0,
        )
        if flip:
            faces = faces[:, [0, 2, 1]]
        builder.add_mesh(material, vertices, faces)


def _draw_areas(
    builder: MeshBuilder,
    ctx: GenerationContext,
    features,
    default_material,
    sampled: dict,
    z: float,
) -> None:
    """Desenha as areas agrupando por material.

    Sem imagem, todas caem no material do estilo e viram uma uniao so. Com
    imagem, cada area usa a cor amostrada dela, entao agrupamos por material
    para nao gerar uma malha por poligono.
    """
    by_material: dict[object, list] = {}
    for feature in features:
        if feature.geometry is None or feature.geometry.is_empty:
            continue
        material = sampled.get(feature.osm_id, default_material)
        by_material.setdefault(material, []).append(feature.geometry)

    for material, geometries in by_material.items():
        builder.add_flat(
            material, ctx.simplify(unary_union(geometries)), z, drape=ctx.terrain is not None
        )


def generate_landuse(builder: MeshBuilder, ctx: GenerationContext, map_data) -> None:
    """Parques, bosques e estacionamentos como superficies chapadas.

    Quando a foto entra como textura do terreno essas manchas ja aparecem nela,
    entao nao sao repintadas por cima.
    """
    if ctx.imagery is not None and ctx.settings.ground_texture and not ctx.settings.satellite_landuse:
        return

    palette = ctx.palette
    areas = ctx.area_materials

    _draw_areas(builder, ctx, map_data.parks, palette.grass, areas.get("park", {}), layers.Z_GREEN)
    _draw_areas(
        builder, ctx, map_data.forests, palette.forest_floor, areas.get("forest", {}),
        layers.Z_GREEN + 0.005,
    )
    _draw_areas(
        builder, ctx, map_data.parkings, palette.parking, areas.get("parking", {}),
        layers.Z_PARKING,
    )
