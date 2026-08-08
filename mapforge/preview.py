"""Visualizador 2D: confere rapidamente o que o parser extraiu, antes de gerar 3D."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .core.features import MapData
from .generation.roads import road_half_width
from .styles import get_style


def _polygon_patches(geom):
    """Achata qualquer geometria de area numa lista de (exterior, buracos).

    Os aneis saem orientados (exterior anti-horario, buracos horario) para que a
    regra de preenchimento nonzero do matplotlib abra os buracos corretamente.
    """
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry.polygon import orient

    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        polys = [geom]
    elif isinstance(geom, MultiPolygon):
        polys = list(geom.geoms)
    elif hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if isinstance(g, Polygon)]
    else:
        return []

    out = []
    for poly in polys:
        if poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty or not isinstance(poly, Polygon):
                continue
        poly = orient(poly, sign=1.0)
        out.append((list(poly.exterior.coords), [list(i.coords) for i in poly.interiors]))
    return out


def _path_patch(exterior, holes, **kwargs):
    """PathPatch composto: um contorno externo mais os buracos."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    verts, codes = [], []
    for ring in [exterior, *holes]:
        ring = list(ring)
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        verts.extend(ring)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(ring) - 2) + [MplPath.CLOSEPOLY])
    if not verts:
        return None
    return PathPatch(MplPath(verts, codes), **kwargs)


def render_preview(
    map_data: MapData,
    path: Optional[str | Path] = None,
    style_name: str = "lowpoly",
    dpi: int = 130,
    size: float = 9.0,
):
    """Desenha a planta 2D do mapa. Salva em `path` ou devolve a figura."""
    import matplotlib

    if path is not None:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    style = get_style(style_name)
    bbox = map_data.bbox
    half_w, half_h = bbox.width_m / 2.0, bbox.height_m / 2.0

    aspect = bbox.height_m / max(bbox.width_m, 1e-6)
    fig, ax = plt.subplots(figsize=(size, size * aspect), dpi=dpi)
    fig.patch.set_facecolor(style.ground)
    ax.set_facecolor(style.ground)

    def draw_areas(features, color, zorder, alpha=1.0, attr="geometry"):
        for feature in features:
            for exterior, holes in _polygon_patches(getattr(feature, attr)):
                patch = _path_patch(
                    exterior,
                    holes,
                    facecolor=color,
                    edgecolor="none",
                    alpha=alpha,
                    zorder=zorder,
                )
                if patch is not None:
                    ax.add_patch(patch)

    draw_areas(map_data.parks, style.grass, 1)
    draw_areas(map_data.forests, style.forest_floor, 2)
    draw_areas(map_data.parkings, style.parking, 3)
    draw_areas(map_data.waters, style.water, 4)

    # Vias, rios e ferrovias: espessura proporcional a largura real (1 pt = 1/72 pol).
    scale = (size * 72.0) / max(bbox.width_m, 1.0)  # pontos por metro

    rivers, river_widths = [], []
    for river in map_data.rivers:
        if river.centerline is None:
            continue
        rivers.append(list(river.centerline.coords))
        river_widths.append(max(river.width * scale, 0.5))
    if rivers:
        ax.add_collection(
            LineCollection(
                rivers,
                colors=[style.water],
                linewidths=river_widths,
                zorder=4,
                capstyle="round",
            )
        )

    segments, widths, colors = [], [], []
    for road in map_data.roads:
        if road.centerline is None:
            continue
        segments.append(list(road.centerline.coords))
        widths.append(max(road_half_width(road) * 2 * scale, 0.35))
        colors.append(style.asphalt_major if road.road_class.value in
                      {"motorway", "trunk", "primary"} else style.asphalt)
    if segments:
        ax.add_collection(
            LineCollection(segments, colors=colors, linewidths=widths, zorder=5, capstyle="round")
        )

    rails = [list(r.centerline.coords) for r in map_data.railways if r.centerline is not None]
    if rails:
        ax.add_collection(
            LineCollection(rails, colors=[style.rail], linewidths=1.4, zorder=6, linestyles="--")
        )

    # Edificios: cor da parede no preenchimento, cor do telhado no contorno.
    for building in map_data.buildings:
        for exterior, holes in _polygon_patches(building.footprint):
            patch = _path_patch(
                exterior,
                holes,
                facecolor=style.wall_palette[building.osm_id % len(style.wall_palette)],
                edgecolor=style.roof_palette[building.osm_id % len(style.roof_palette)],
                linewidth=0.3,
                zorder=7,
            )
            if patch is not None:
                ax.add_patch(patch)

    if map_data.trees:
        ax.scatter(
            [t.position.x for t in map_data.trees],
            [t.position.y for t in map_data.trees],
            s=6,
            c=[style.canopy_palette[0]],
            zorder=8,
        )

    ax.set_xlim(-half_w, half_w)
    ax.set_ylim(-half_h, half_h)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    counts = map_data.summary()
    ax.set_title(
        f"{bbox.width_m:.0f} x {bbox.height_m:.0f} m  |  "
        f"{counts['buildings']} edificios, {counts['roads']} vias, {counts['trees']} arvores",
        fontsize=9,
        color="#333333",
        pad=8,
    )
    fig.tight_layout()

    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        return path
    return fig
