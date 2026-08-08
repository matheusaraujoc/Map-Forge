"""Interface de linha de comando do MapForge."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config
from .core.geo import BBox
from .data import Cache, geocode
from .generation import GenerationSettings
from .pipeline import generate, load_map
from .styles import list_styles


# --------------------------------------------------------------------- saida


class Progress:
    """Barra de progresso simples em uma linha."""

    def __init__(self, enabled: bool = True, width: int = 28):
        self.enabled = enabled and sys.stderr.isatty()
        self.width = width
        self.started = time.perf_counter()
        self._last = ""

    def __call__(self, message: str, fraction: float) -> None:
        if not self.enabled:
            if message != self._last:
                print(f"  {message}", file=sys.stderr)
                self._last = message
            return
        filled = int(self.width * fraction)
        bar = "#" * filled + "." * (self.width - filled)
        elapsed = time.perf_counter() - self.started
        line = f"\r  [{bar}] {fraction * 100:5.1f}%  {elapsed:5.1f}s  {message[:46]:<46}"
        sys.stderr.write(line)
        sys.stderr.flush()

    def done(self) -> None:
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


def _resolve_bbox(args) -> tuple[BBox, str]:
    """Monta a bbox a partir de --bbox ou de --place/--center + --radius."""
    if args.bbox:
        return BBox.from_string(args.bbox), args.bbox
    if args.center:
        lat, lon = (float(v) for v in args.center.replace(";", ",").split(","))
        return BBox.from_center(lat, lon, args.radius), f"{lat},{lon}"
    if args.place:
        lat, lon, label = geocode(args.place)
        print(f"  local: {label}  ({lat:.5f}, {lon:.5f})", file=sys.stderr)
        return BBox.from_center(lat, lon, args.radius), args.place
    raise SystemExit("informe --bbox, --center ou --place")


def _settings_from_args(args) -> GenerationSettings:
    return GenerationSettings(
        seed=args.seed,
        style=args.style,
        detail=args.detail,
        region=args.region,
        terrain=not args.no_terrain,
        roads=not args.no_roads,
        buildings=not args.no_buildings,
        water=not args.no_water,
        vegetation=not args.no_vegetation,
        sidewalks=not args.no_sidewalks,
        windows=False if args.no_windows else None,
        tree_density=args.tree_density,
        max_trees=args.max_trees,
        building_height_scale=args.height_scale,
        satellite=args.satellite,
        satellite_provider=args.provider,
        satellite_zoom=args.satellite_zoom,
        ground_texture=args.ground_texture,
        roof_blend=args.roof_blend,
        area_blend=args.area_blend,
        elevation=args.elevation,
        elevation_zoom=args.elevation_zoom,
        elevation_exaggeration=args.exaggeration,
        overture=args.overture,
        extra_footprints=args.footprints,
        shadow_heights=args.shadow_heights,
        detect_buildings=args.detect_buildings,
        detect_vegetation=args.detect_vegetation,
        urban_scale=args.urban_scale,
    )


def _default_output(label: str, settings: GenerationSettings) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40].strip("_")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return config.OUTPUT_DIR / f"{safe or 'mapa'}_{settings.style}_{stamp}.glb"


# ------------------------------------------------------------------ comandos


def cmd_generate(args) -> int:
    bbox, label = _resolve_bbox(args)
    settings = _settings_from_args(args)

    print(
        f"  regiao: {bbox}  ({bbox.width_m:.0f} x {bbox.height_m:.0f} m, "
        f"{bbox.area_km2:.2f} km2)",
        file=sys.stderr,
    )

    output = Path(args.out) if args.out else _default_output(label, settings)
    progress = Progress(not args.quiet)

    with Cache() as cache:
        result = generate(
            bbox,
            settings=settings,
            output=output,
            cache=cache,
            force_download=args.force,
            progress=progress,
        )
        if args.save_project:
            cache.save_project(
                args.save_project, bbox.key(), settings.seed, settings.style, settings.to_dict()
            )
    progress.done()

    scene = result.scene
    counts = result.map_data.summary()
    size_mb = result.output.stat().st_size / 1e6 if result.output else 0.0

    print()
    print(f"  fonte     : {', '.join(f'{k}={v}' for k, v in counts.items() if v)}")
    print(f"  malha     : {scene.triangle_count:,} triangulos, {scene.vertex_count:,} vertices")
    print(f"  materiais : {len(scene.groups)}")
    print(f"  tempo     : {scene.metadata['build_seconds']}s de geracao")
    print(f"  arquivo   : {result.output}  ({size_mb:.1f} MB)")
    if args.save_project:
        print(f"  projeto   : '{args.save_project}' salvo (apenas parametros)")

    if args.preview:
        from .preview import render_preview

        preview_path = Path(args.preview) if isinstance(args.preview, str) else output.with_suffix(
            ".png"
        )
        render_preview(result.map_data, preview_path, style_name=settings.style)
        print(f"  preview   : {preview_path}")

    if args.render:
        from .render import Camera, render_scene

        render_path = (
            Path(args.render)
            if isinstance(args.render, str)
            else output.with_name(output.stem + "_3d.png")
        )
        started = time.perf_counter()
        render_scene(
            result.scene,
            render_path,
            width=args.render_width,
            height=args.render_height,
            camera=Camera(
                azimuth=args.azimuth,
                elevation=args.elevation,
                perspective=args.perspective,
            ),
            supersample=args.supersample,
        )
        print(f"  render 3D : {render_path}  ({time.perf_counter() - started:.1f}s)")

    return 0


def cmd_preview(args) -> int:
    from .preview import render_preview

    bbox, label = _resolve_bbox(args)
    progress = Progress(not args.quiet)
    with Cache() as cache:
        map_data, _ = load_map(bbox, cache, force_download=args.force, progress=progress)
    progress.done()

    output = Path(args.out) if args.out else _default_output(label, GenerationSettings()).with_suffix(
        ".png"
    )
    render_preview(map_data, output, style_name=args.style)
    print()
    print(f"  fonte   : {', '.join(f'{k}={v}' for k, v in map_data.summary().items() if v)}")
    print(f"  preview : {output}")
    return 0


def cmd_region(args) -> int:
    """Gera uma regiao grande em blocos, com manifesto de montagem."""
    from .tiling import generate_tiled, plan_tiles

    bbox, label = _resolve_bbox(args)
    settings = _settings_from_args(args)

    plano, cols, rows = plan_tiles(bbox, args.tile_km)
    print(
        f"  regiao : {bbox.width_m / 1000:.1f} x {bbox.height_m / 1000:.1f} km "
        f"({bbox.area_km2:.1f} km2)"
    )
    print(f"  blocos : {cols} x {rows} = {len(plano)} de {args.tile_km} km de lado")
    if args.dry_run:
        for tile in plano[:8]:
            print(f"    r{tile.row:02d}c{tile.col:02d}  deslocamento {tile.offset} m")
        if len(plano) > 8:
            print(f"    ... e mais {len(plano) - 8}")
        return 0

    nome = args.name or "".join(
        c if c.isalnum() or c in "-_" else "_" for c in label
    )[:40].strip("_") or "regiao"
    destino = Path(args.out) if args.out else config.OUTPUT_DIR / nome

    progress = Progress(not args.quiet)
    with Cache() as cache:
        resultado = generate_tiled(
            bbox,
            settings=settings,
            output_dir=destino,
            name=nome,
            tile_km=args.tile_km,
            cache=cache,
            progress=progress,
            skip_existing=args.resume,
        )
    progress.done()

    falhas = [t for t in resultado.tiles if t.error]
    print()
    print(f"  gerados  : {len(resultado.ok)} de {len(resultado.tiles)} blocos")
    print(f"  malha    : {resultado.triangles:,} triangulos, {resultado.buildings:,} edificios")
    print(f"  tempo    : {resultado.seconds}s")
    print(f"  pasta    : {destino}")
    print(f"  manifesto: {resultado.manifest}")
    if falhas:
        print(f"  {len(falhas)} bloco(s) sem dados ou com erro:")
        for tile in falhas[:5]:
            print(f"    r{tile.row:02d}c{tile.col:02d}: {tile.error[:70]}")
    return 0


def cmd_satellite(args) -> int:
    """Baixa so a imagem, para conferir a resolucao antes de gerar a cena."""
    from .imagery import fetch_imagery
    from .imagery.tiles import choose_zoom, get_provider, meters_per_pixel

    bbox, label = _resolve_bbox(args)
    provider = get_provider(args.provider)
    ideal = args.satellite_zoom or choose_zoom(bbox, provider)

    print(f"  regiao   : {bbox.width_m:.0f} x {bbox.height_m:.0f} m")
    print(f"  provedor : {provider.label} (zoom maximo {provider.max_zoom})")
    print(f"  zoom alvo: {ideal}  ({meters_per_pixel(bbox.center[0], ideal):.2f} m/pixel)")

    progress = Progress(not args.quiet)
    with Cache() as cache:
        geo = fetch_imagery(
            bbox,
            provider_name=args.provider,
            cache=cache,
            zoom=args.satellite_zoom,
            progress=progress,
            force=args.force,
        )
        cache.flush_tiles()
    progress.done()

    output = Path(args.out) if args.out else _default_output(label, GenerationSettings()).with_name(
        f"satelite_{provider.name}.png"
    )
    geo.save(output)
    print()
    if geo.zoom < ideal:
        print(f"  zoom real: {geo.zoom} - o provedor nao tem cobertura em z{ideal} aqui")
    else:
        print(f"  zoom real: {geo.zoom}")
    print(f"  imagem   : {output}  ({geo.size[0]}x{geo.size[1]} px)")
    print(f"  escala   : {geo.meters_per_pixel:.2f} m/pixel")
    print(f"  credito  : {geo.attribution}")
    return 0


def cmd_gui(_args) -> int:
    try:
        from .ui import run as run_gui
    except ImportError as exc:
        raise SystemExit(
            f"interface grafica indisponivel ({exc}). Instale com: pip install PySide6"
        ) from None
    return run_gui()


def cmd_segment(args) -> int:
    """Converte a imagem de satelite num desenho 2D classificado por cobertura."""
    from .data import download_region, parse_osm
    from .imagery import fetch_imagery
    from .imagery.detect import detect_buildings
    from .imagery.segment import classify, render

    bbox, label = _resolve_bbox(args)
    progress = Progress(not args.quiet)

    with Cache() as cache:
        geo = fetch_imagery(
            bbox, provider_name=args.provider, cache=cache,
            zoom=args.satellite_zoom, progress=progress, force=args.force,
        )
        cache.flush_tiles()
        map_data = parse_osm(download_region(bbox, cache=cache, progress=progress), bbox)
    progress.done()

    seg = classify(geo, map_data)

    poligonos = None
    if args.with_buildings:
        deteccao = detect_buildings(geo, map_data)
        poligonos = deteccao.polygons
        print(f"  deteccao : {deteccao.summary()}")

    saida = Path(args.out) if args.out else _default_output(label, GenerationSettings()).with_name(
        f"classificado_{label[:24].replace(' ', '_')}.png"
    )
    render(seg, saida, geo=geo, side_by_side=not args.only_map, polygons=poligonos)

    print()
    print(f"  imagem   : {geo.size[0]}x{geo.size[1]} px, {geo.meters_per_pixel:.2f} m/pixel")
    print("  cobertura:")
    for chave, fracao in sorted(seg.fractions().items(), key=lambda kv: -kv[1]):
        print(f"    {chave:<16} {fracao * 100:5.1f}%")
    print(f"  desenho  : {saida}")
    return 0


def cmd_providers(_args) -> int:
    from .imagery.tiles import PROVIDERS, _custom_provider

    print("  Fontes de imagem disponiveis:")
    for provider in PROVIDERS.values():
        key = ""
        if provider.requires_key():
            state = "definida" if provider.api_key() else "AUSENTE"
            key = f"  [precisa de ${provider.key_env}: {state}]"
        print(f"    {provider.name:<10} z<={provider.max_zoom:<3} {provider.label}{key}")

    custom = _custom_provider()
    if custom:
        print(f"    {'custom':<10} z<={custom.max_zoom:<3} {custom.label}")
    else:
        print("    custom     -    defina MAPFORGE_TILE_URL com um template XYZ para usar")
    print()
    print("  Atribuicao e obrigatoria em qualquer publicacao que use as imagens.")
    return 0


def cmd_styles(_args) -> int:
    print("  Estilos disponiveis:")
    for name, label in list_styles():
        print(f"    {name:<12} {label}")
    return 0


def cmd_cache(args) -> int:
    with Cache() as cache:
        if args.action == "clear":
            removed = cache.clear_osm()
            print(f"  {removed} entradas removidas de {cache.path}")
            return 0
        rows = cache.list_osm()
        if not rows:
            print("  cache vazio")
            return 0
        print(f"  {len(rows)} regioes em cache ({cache.path}):")
        for row in rows:
            when = datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M")
            print(
                f"    {row['bbox']:<46} {row['elements']:>7} elementos"
                f"  {row['bytes'] / 1e6:>6.1f} MB  {when}"
            )
    return 0


def cmd_projects(args) -> int:
    with Cache() as cache:
        if args.name:
            project = cache.load_project(args.name)
            if project is None:
                print(f"  projeto '{args.name}' nao encontrado")
                return 1
            print(f"  nome   : {project['name']}")
            print(f"  bbox   : {project['bbox']}")
            print(f"  seed   : {project['seed']}")
            print(f"  estilo : {project['style']}")
            print(f"  config : {project['settings']}")
            return 0
        rows = cache.list_projects()
        if not rows:
            print("  nenhum projeto salvo")
            return 0
        for row in rows:
            when = datetime.fromtimestamp(row["updated_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"    {row['name']:<20} {row['style']:<10} seed={row['seed']:<8} {when}")
    return 0


# --------------------------------------------------------------------- parser


def _add_region_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("regiao")
    group.add_argument("--bbox", help="sul,oeste,norte,leste em graus decimais")
    group.add_argument("--center", help="lat,lon do centro (usar com --radius)")
    group.add_argument("--place", help="nome do lugar, resolvido via Nominatim")
    group.add_argument(
        "--radius", type=float, default=600.0, help="meia-aresta em metros (padrao: 600)"
    )
    parser.add_argument("--force", action="store_true", help="ignora o cache e rebaixa do OSM")
    parser.add_argument("-q", "--quiet", action="store_true", help="sem barra de progresso")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapforge",
        description="Gera mapas 3D estilizados a partir de dados do OpenStreetMap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exemplos:\n"
            "  mapforge generate --place \"Ouro Preto, MG\" --radius 500 --style cartoon\n"
            "  mapforge generate --bbox -25.44,-49.28,-25.42,-49.26 --detail high\n"
            "  mapforge preview --place \"Paraty, RJ\" --radius 700\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log detalhado")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="gera a cena 3D e exporta")
    _add_region_args(gen)
    gen.add_argument("--out", help="arquivo de saida (.glb, .gltf, .obj, .ply)")
    gen.add_argument("--style", default="lowpoly", help="estilo grafico (ver: mapforge styles)")
    gen.add_argument(
        "--detail", default="medium", choices=("low", "medium", "high"), help="nivel de detalhe"
    )
    gen.add_argument("--seed", type=int, default=1, help="seed da geracao procedural")
    gen.add_argument(
        "--region",
        default=None,
        help="perfil regional (tropical, subtropical, temperada, boreal). "
        "Padrao: deduzido da latitude",
    )
    gen.add_argument("--tree-density", type=float, default=1.0, dest="tree_density")
    gen.add_argument("--max-trees", type=int, default=40_000, dest="max_trees")
    gen.add_argument("--height-scale", type=float, default=1.0, dest="height_scale")
    gen.add_argument("--no-terrain", action="store_true")
    gen.add_argument("--no-roads", action="store_true")
    gen.add_argument("--no-buildings", action="store_true")
    gen.add_argument("--no-water", action="store_true")
    gen.add_argument("--no-vegetation", action="store_true")
    gen.add_argument("--no-sidewalks", action="store_true")
    gen.add_argument(
        "--no-windows",
        action="store_true",
        help="sem janelas nas fachadas (reduz muito o tamanho do arquivo)",
    )
    gen.add_argument(
        "--preview", nargs="?", const=True, default=False, help="tambem grava a planta 2D em PNG"
    )
    gen.add_argument(
        "--render",
        nargs="?",
        const=True,
        default=False,
        help="tambem grava um render 3D offscreen em PNG",
    )
    gen.add_argument("--render-width", type=int, default=1500, dest="render_width")
    gen.add_argument("--render-height", type=int, default=950, dest="render_height")
    gen.add_argument("--azimuth", type=float, default=225.0, help="angulo da camera (graus)")
    gen.add_argument("--elevation", type=float, default=34.0, help="inclinacao da camera (graus)")
    gen.add_argument("--perspective", action="store_true", help="camera em perspectiva")
    gen.add_argument("--supersample", type=int, default=2, help="antialiasing (1 = desligado)")
    sat = gen.add_argument_group("imagem de satelite")
    sat.add_argument(
        "--satellite",
        action="store_true",
        help="tira as cores reais do lugar da imagem (telhados, terreno, areas verdes)",
    )
    sat.add_argument("--provider", default="esri", help="fonte dos tiles (ver: mapforge providers)")
    sat.add_argument(
        "--satellite-zoom", type=int, default=None, dest="satellite_zoom",
        help="forca o nivel de zoom (padrao: o maior com cobertura real)",
    )
    sat.add_argument(
        "--roof-blend", type=float, default=0.75, dest="roof_blend",
        help="peso da foto na cor dos telhados: 0 = so estilo, 1 = so foto (padrao: 0.75)",
    )
    sat.add_argument(
        "--area-blend", type=float, default=0.55, dest="area_blend",
        help="idem para terreno, parques e bosques (padrao: 0.55)",
    )
    sat.add_argument(
        "--ground-texture", action="store_true", dest="ground_texture",
        help="experimental: cola a foto como textura do terreno em vez de so tirar a cor",
    )
    fontes = gen.add_argument_group("fontes de edificios")
    fontes.add_argument(
        "--overture",
        action="store_true",
        help="completa o OSM com o Overture Maps (OSM + Microsoft + Esri + Google "
        "Open Buildings) - a melhor fonte para cidade pequena; exige duckdb",
    )
    fontes.add_argument(
        "--footprints",
        action="store_true",
        help="completa o OSM com os contornos abertos da Microsoft "
        "(reserva do --overture; primeiro download por regiao e grande)",
    )
    fontes.add_argument(
        "--detect-buildings",
        action="store_true",
        dest="detect_buildings",
        help="detecta telhados na propria imagem onde nao ha contorno pronto "
        "(exige --satellite; aproximado, retangulos orientados)",
    )
    fontes.add_argument(
        "--urban-scale",
        dest="urban_scale",
        choices=("povoado", "pequena", "media", "grande"),
        default=None,
        help="forca o porte do assentamento, que e o teto de pavimentos de predio "
        "sem altura em tag (padrao: medir nos proprios contornos)",
    )
    fontes.add_argument(
        "--detect-vegetation",
        action="store_true",
        dest="detect_vegetation",
        help="encontra mata e gramado na imagem (exige --satellite); o OSM quase "
        "nunca desenha mata em cidade pequena",
    )
    fontes.add_argument(
        "--shadow-heights",
        action="store_true",
        dest="shadow_heights",
        help="estima a altura dos predios pela sombra na imagem (exige --satellite)",
    )

    relief = gen.add_argument_group("relevo")
    # Nao pode se chamar --elevation: esse nome ja e o angulo da camera.
    relief.add_argument(
        "--relief",
        action="store_true",
        dest="elevation",
        help="terreno com relevo real (modelo digital de elevacao)",
    )
    relief.add_argument(
        "--relief-zoom", type=int, default=None, dest="elevation_zoom",
        help="nivel dos tiles de elevacao (padrao: o maior que couber, ate z15)",
    )
    relief.add_argument(
        "--exaggeration", type=float, default=1.0,
        help="multiplica o desnivel; 1.0 mantem a escala real",
    )

    gen.add_argument("--save-project", help="guarda os parametros no banco local com esse nome")
    gen.set_defaults(func=cmd_generate)

    prev = sub.add_parser("preview", help="apenas a planta 2D (rapido, sem gerar malha)")
    _add_region_args(prev)
    prev.add_argument("--out", help="arquivo PNG de saida")
    prev.add_argument("--style", default="lowpoly")
    prev.set_defaults(func=cmd_preview)

    styles = sub.add_parser("styles", help="lista os estilos disponiveis")
    styles.set_defaults(func=cmd_styles)

    region = sub.add_parser(
        "region",
        help="gera uma regiao grande em blocos (sem limite de area) + manifesto",
        description="Corta a regiao em blocos, gera um de cada vez e grava o "
        "manifesto de montagem. E o caminho para cidade inteira.",
    )
    _add_region_args(region)
    region.add_argument("--out", help="pasta de saida")
    region.add_argument("--name", help="nome base dos arquivos")
    region.add_argument(
        "--tile-km", type=float, default=1.5, dest="tile_km",
        help="lado do bloco em km (padrao: 1.5)",
    )
    region.add_argument(
        "--resume", action="store_true", help="pula blocos ja gerados na pasta"
    )
    region.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="so mostra o plano de blocos, sem gerar",
    )
    # Mesmas opcoes de geracao do comando `generate`.
    region.add_argument("--style", default="lowpoly")
    region.add_argument("--detail", default="medium", choices=("low", "medium", "high"))
    region.add_argument("--region", default=None, dest="region")
    region.add_argument("--seed", type=int, default=1)
    region.add_argument("--tree-density", type=float, default=1.0, dest="tree_density")
    region.add_argument("--max-trees", type=int, default=40_000, dest="max_trees")
    region.add_argument("--height-scale", type=float, default=1.0, dest="height_scale")
    region.add_argument("--satellite", action="store_true")
    region.add_argument("--provider", default="esri")
    region.add_argument("--satellite-zoom", type=int, default=None, dest="satellite_zoom")
    region.add_argument("--roof-blend", type=float, default=0.75, dest="roof_blend")
    region.add_argument("--area-blend", type=float, default=0.55, dest="area_blend")
    region.add_argument("--ground-texture", action="store_true", dest="ground_texture")
    region.add_argument("--overture", action="store_true")
    region.add_argument("--footprints", action="store_true")
    region.add_argument("--detect-buildings", action="store_true", dest="detect_buildings")
    region.add_argument("--detect-vegetation", action="store_true", dest="detect_vegetation")
    region.add_argument(
        "--urban-scale", dest="urban_scale", default=None,
        choices=("povoado", "pequena", "media", "grande"),
    )
    region.add_argument("--shadow-heights", action="store_true", dest="shadow_heights")
    region.add_argument("--relief", action="store_true", dest="elevation")
    region.add_argument("--relief-zoom", type=int, default=None, dest="elevation_zoom")
    region.add_argument("--exaggeration", type=float, default=1.0)
    for flag in ("no-terrain", "no-roads", "no-buildings", "no-water",
                 "no-vegetation", "no-sidewalks", "no-windows"):
        region.add_argument(f"--{flag}", action="store_true", dest=flag.replace("-", "_"))
    region.set_defaults(func=cmd_region)

    satellite = sub.add_parser(
        "satellite", help="baixa so a imagem de satelite da regiao (para conferir a resolucao)"
    )
    _add_region_args(satellite)
    satellite.add_argument("--out", help="arquivo PNG de saida")
    satellite.add_argument("--provider", default="esri")
    satellite.add_argument("--satellite-zoom", type=int, default=None, dest="satellite_zoom")
    satellite.set_defaults(func=cmd_satellite)

    segment = sub.add_parser(
        "segment",
        help="converte a imagem de satelite num desenho 2D classificado",
        description="Desenha o que o sistema enxerga na foto: telha, laje, "
        "vegetacao, solo, agua e sombra, cada uma com a sua cor.",
    )
    _add_region_args(segment)
    segment.add_argument("--out", help="arquivo PNG de saida")
    segment.add_argument("--provider", default="esri")
    segment.add_argument("--satellite-zoom", type=int, default=None, dest="satellite_zoom")
    segment.add_argument(
        "--only-map", action="store_true", dest="only_map",
        help="grava so o desenho, sem a foto ao lado",
    )
    segment.add_argument(
        "--with-buildings", action="store_true", dest="with_buildings",
        help="desenha por cima os contornos que o detector extraiu",
    )
    segment.set_defaults(func=cmd_segment)

    providers = sub.add_parser("providers", help="lista as fontes de imagem de satelite")
    providers.set_defaults(func=cmd_providers)

    gui = sub.add_parser("gui", help="abre a interface grafica")
    gui.set_defaults(func=cmd_gui)

    cache = sub.add_parser("cache", help="inspeciona ou limpa o cache de downloads")
    cache.add_argument("action", nargs="?", default="list", choices=("list", "clear"))
    cache.set_defaults(func=cmd_cache)

    projects = sub.add_parser("projects", help="lista ou detalha projetos salvos")
    projects.add_argument("name", nargs="?")
    projects.set_defaults(func=cmd_projects)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="  %(levelname)s %(name)s: %(message)s",
    )
    config.ensure_dirs()

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  cancelado", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - erro amigavel na CLI
        if args.verbose:
            raise
        print(f"\n  erro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
