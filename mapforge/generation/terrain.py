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
GRID_STEP = {"distante": 32.0, "low": 18.0, "medium": 10.0, "high": 6.0}

# Teto de celulas da grade, independente do nivel de detalhe. Sem ele, uma
# regiao de 11 km2 em detalhe alto pedia 615 mil triangulos so de terreno e a
# geracao levava mais de dois minutos. O passo e afrouxado ate caber.
MAX_GRID_CELLS = 60_000

# Nao adianta uma grade mais fina que o proprio DEM: os tiles Terrarium tem
# cerca de 4,5 m por pixel, entao abaixo disso so se interpola o mesmo dado.
MIN_USEFUL_STEP = 4.5


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

        step = max(float(step), MIN_USEFUL_STEP, grid.meters_per_pixel * 0.9)
        # Afrouxa o passo ate a grade caber no teto de celulas.
        cells = (bbox.width_m / step) * (bbox.height_m / step)
        if cells > MAX_GRID_CELLS:
            step *= float(np.sqrt(cells / MAX_GRID_CELLS))
            log.info(
                "Passo do terreno afrouxado para %.1f m: a regiao de %.1f km2 nao cabe "
                "na grade do nivel de detalhe",
                step,
                bbox.area_km2,
            )

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

    def carve(
        self,
        geometry,
        depth: float,
        level: Optional[float] = None,
        bank_m: Optional[float] = None,
    ) -> None:
        """Rebaixa o terreno dentro de um poligono, com margem em rampa.

        E assim que o leito do rio aparece: o proprio terreno afunda, em vez de
        a agua ficar por cima do relevo.

        A primeira versao testava apenas *dentro ou fora* em cada vertice da
        grade. Como a grade tem 10 m no detalhe medio, a margem de um rio
        meandrante saia em **escada de 10 m** - o contorno da agua e curvo, mas
        o terreno em volta so sabia descer em degraus alinhados a grade.

        A correcao e trocar o teste binario por distancia: o vertice desce
        proporcionalmente a quanta margem ele tem para dentro. A transicao
        passa a acompanhar o poligono e nao a grade, e a curva volta a ser
        curva.
        """
        import shapely
        from shapely import contains_xy

        if geometry is None or geometry.is_empty:
            return
        mesh_x, mesh_y = np.meshgrid(self.xs, self.ys)
        planos_x, planos_y = mesh_x.ravel(), mesh_y.ravel()
        dentro = contains_xy(geometry, planos_x, planos_y)

        target = (self.level_over(geometry) if level is None else level) - depth

        # A rampa cobre mais de uma celula de proposito: com menos que isso ela
        # nao tem vertice onde acontecer e o degrau volta.
        banda = bank_m if bank_m is not None else max(self.step * 1.6, 6.0)
        try:
            distancia = shapely.distance(
                shapely.points(planos_x, planos_y), geometry.boundary
            )
        except Exception:  # noqa: BLE001 - topologia ruim: cai no teste binario
            if dentro.any():
                achatado = self.z.ravel()
                achatado[dentro] = np.minimum(achatado[dentro], target)
                self.z = achatado.reshape(self.z.shape)
            return

        # Dentro: desce cheio no miolo e menos perto da borda.
        # Fora: ainda desce um pouco, formando o talude - e o que faz a margem
        # encostar na agua em vez de terminar em parede.
        peso = np.where(
            dentro,
            np.clip(distancia / banda, 0.0, 1.0) * 0.5 + 0.5,
            np.clip(1.0 - distancia / banda, 0.0, 1.0) * 0.5,
        )
        if not np.any(peso > 0.0):
            return

        achatado = self.z.ravel().copy()
        rebaixado = achatado * (1.0 - peso) + target * peso
        self.z = np.minimum(achatado, rebaixado).reshape(self.z.shape)

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

    @property
    def step(self) -> float:
        """Espacamento da grade, em metros."""
        return float(self.xs[1] - self.xs[0]) if len(self.xs) > 1 else 1.0


# ------------------------------------------------------------------- geradores


def _ground_material(ctx: GenerationContext):
    """Material do terreno.

    Quatro casos, do mais comum ao mais raro: cor do estilo; cor chapada
    amostrada da imagem (padrao quando o satelite esta ligado); a textura
    *pintada*, classe a classe, com as cores medidas na propria foto; e a foto
    crua, que fica por ultimo porque nao combina com o resto da cena low-poly.
    """
    imagery = ctx.imagery
    if imagery is None:
        return ctx.palette.ground, False

    if not ctx.settings.ground_texture:
        return (ctx.ground_material or ctx.palette.ground), False

    from ..core.mesh import Material

    if ctx.settings.ground_texture_mode == "foto":
        from ..imagery.sampling import build_ground_texture

        texture = build_ground_texture(
            imagery,
            brightness=ctx.settings.texture_brightness,
            saturation=ctx.settings.texture_saturation,
        )
        name = "ground_satellite"
    elif ctx.settings.redraw_ground:
        # Redesenho com pincel: copa vira disco de copa, areia vira granulado.
        # E o unico modo que tem *forma*, e nao so cor modulada por ruido.
        from ..imagery.redraw import redraw_ground

        desenhado = redraw_ground(
            imagery,
            ctx.map_data,
            target_mpp=ctx.settings.texture_detail_m or 0.35,
            seed=ctx.settings.seed,
        )
        ctx.painted_ground = desenhado
        material = Material(
            name="ground_redesenhado", color=(1.0, 1.0, 1.0), texture=desenhado.image
        )
        ctx.report(f"chao redesenhado: {desenhado.summary()}", 0.05)
        return material, True
    else:
        from ..imagery.painted import paint_ground

        pintado = paint_ground(
            imagery,
            ctx.map_data,
            variation=ctx.settings.texture_variation,
            target_mpp=ctx.settings.texture_detail_m,
            detail=ctx.settings.texture_detail_m > 0.0,
            seed=ctx.settings.seed,
            grain=ctx.settings.texture_grain,
        )
        ctx.painted_ground = pintado
        texture = pintado.image
        name = "ground_pintado"
        ctx.report(f"textura pintada: {pintado.summary()}", 0.05)

    # Cor base branca: no glTF ela multiplica a textura.
    material = Material(name=name, color=(1.0, 1.0, 1.0), texture=texture)
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
        if ctx.clip is not None:
            # Regiao desenhada a mao: o terreno vira uma superficie assentada
            # sobre o relevo, recortada pelo mesmo poligono que recortou o resto.
            builder.add_flat(
                material, ctx.clip, layers.Z_GROUND, uv_bounds=uv_bounds, drape=True
            )
            _add_polygon_skirt(builder, ctx, ctx.clip, field)
            return

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

    ground = ctx.clip if ctx.clip is not None else box(-half_w, -half_h, half_w, half_h)
    if water_union is not None and not water_union.is_empty:
        try:
            ground = ground.difference(water_union)
        except Exception:  # noqa: BLE001 - mantem o plano inteiro se a diferenca falhar
            pass
    if not ground.is_empty:
        builder.add_flat(material, ground, layers.Z_GROUND, uv_bounds=uv_bounds)
        if ctx.clip is not None:
            _add_polygon_skirt(builder, ctx, ctx.clip, None)


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


def _add_polygon_skirt(builder: MeshBuilder, ctx: GenerationContext, clip, field) -> None:
    """Parede lateral ao longo do contorno desenhado, fechando o volume."""
    from shapely import segmentize

    polys = [clip] if clip.geom_type == "Polygon" else list(getattr(clip, "geoms", []))
    material = ctx.palette.bank

    for poly in polys:
        if poly.geom_type != "Polygon" or poly.is_empty:
            continue
        # Densifica para a saia acompanhar o relevo do contorno.
        passo = float(getattr(field, "step", 0.0) or 0.0) / 2.0 if field is not None else 0.0
        borda = poly.exterior if passo <= 0 else segmentize(poly.exterior, passo)
        coords = np.asarray(borda.coords, dtype=np.float64)[:-1]
        if len(coords) < 3:
            continue

        topo_z = np.zeros(len(coords))
        if field is not None:
            topo_z = np.asarray(field.height(coords[:, 0], coords[:, 1]), dtype=np.float64)
        fundo = float(topo_z.min()) - max(
            (float(topo_z.max()) - float(topo_z.min())) * 0.15, 8.0
        )

        n = len(coords)
        nxt = np.roll(coords, -1, axis=0)
        topo_nxt = np.roll(topo_z, -1)
        vertices = np.concatenate(
            [
                np.column_stack([coords, topo_z]),
                np.column_stack([nxt, topo_nxt]),
                np.column_stack([nxt, np.full(n, fundo)]),
                np.column_stack([coords, np.full(n, fundo)]),
            ],
            axis=0,
        )
        idx = np.arange(n)
        faces = np.concatenate(
            [
                np.column_stack([idx, idx + n, idx + 2 * n]),
                np.column_stack([idx, idx + 2 * n, idx + 3 * n]),
            ],
            axis=0,
        )
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
    entao nao sao repintadas por cima. Na textura *pintada* isso e obrigatorio,
    nao opcional: e a sobreposicao das duas que produz o degrau duro na divisa -
    o retalho chapado por cima de uma textura que ja tem a cor medida ali. Sao
    25 mil triangulos em Araioses que so pioram a imagem.
    """
    if ctx.imagery is None or not ctx.settings.ground_texture:
        pass
    elif ctx.settings.ground_texture_mode == "pintada" or not ctx.settings.satellite_landuse:
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
    # Lavoura: tom proprio, entre a terra exposta e o gramado.
    _draw_areas(
        builder, ctx, map_data.farmlands, palette.farmland, areas.get("farmland", {}),
        layers.Z_GREEN - 0.005,
    )
