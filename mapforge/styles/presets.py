"""Definicao dos estilos e da paleta de materiais derivada deles."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.mesh import Material

Color = tuple[float, float, float]


def hexcolor(value: str) -> Color:
    value = value.lstrip("#")
    return (
        int(value[0:2], 16) / 255.0,
        int(value[2:4], 16) / 255.0,
        int(value[4:6], 16) / 255.0,
    )


def _mix(a: Color, b: Color, t: float) -> Color:
    """Interpolacao linear entre duas cores (t=0 devolve `a`)."""
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))  # type: ignore[return-value]


def _scale(color: Color, factor: float) -> Color:
    return tuple(min(1.0, max(0.0, c * factor)) for c in color)  # type: ignore[return-value]


@dataclass(frozen=True)
class Style:
    """Paleta e parametros visuais de um estilo."""

    name: str
    label: str

    ground: Color
    asphalt: Color
    asphalt_major: Color
    footway: Color
    sidewalk: Color
    curb: Color
    parking: Color
    rail: Color
    water: Color
    grass: Color
    forest_floor: Color
    trunk: Color

    wall_palette: tuple[Color, ...]
    roof_palette: tuple[Color, ...]
    canopy_palette: tuple[Color, ...]

    window: Color = (0.35, 0.45, 0.55)
    window_emissive: Color | None = None
    water_opacity: float = 0.85
    roughness: float = 0.92

    # Opcionais: quando ausentes, a Palette deriva a partir das cores acima.
    cobble: Color | None = None  # calcamento de pedra (paving_stones, sett)
    dirt: Color | None = None  # via sem pavimento
    road_line: Color | None = None  # sinalizacao horizontal
    water_bed: Color | None = None  # leito, abaixo da lamina
    bank: Color | None = None  # talude da margem

    # Parametros de geracao influenciados pelo estilo.
    height_scale: float = 1.0
    roof_weights: dict[str, float] = field(
        default_factory=lambda: {"flat": 0.45, "gable": 0.35, "hip": 0.20}
    )
    windows: bool = True

    def palette(self) -> "Palette":
        return Palette(self)


class Palette:
    """Fabrica de materiais de um estilo, com cache por nome."""

    def __init__(self, style: Style):
        self.style = style
        self._cache: dict[str, Material] = {}

    def _make(self, name: str, color: Color, **kwargs) -> Material:
        material = self._cache.get(name)
        if material is None:
            kwargs.setdefault("roughness", self.style.roughness)
            material = self._cache[name] = Material(name=name, color=color, **kwargs)
        return material

    # --- superficies ---
    @property
    def ground(self) -> Material:
        return self._make("ground", self.style.ground)

    @property
    def asphalt(self) -> Material:
        return self._make("road", self.style.asphalt)

    @property
    def asphalt_major(self) -> Material:
        return self._make("road_major", self.style.asphalt_major)

    @property
    def footway(self) -> Material:
        return self._make("footway", self.style.footway)

    @property
    def cobble(self) -> Material:
        color = self.style.cobble or _mix(self.style.asphalt, self.style.sidewalk, 0.45)
        return self._make("cobble", color)

    @property
    def dirt(self) -> Material:
        color = self.style.dirt or _mix(self.style.footway, self.style.ground, 0.4)
        return self._make("dirt", color)

    @property
    def road_line(self) -> Material:
        color = self.style.road_line or (0.93, 0.92, 0.86)
        return self._make("road_line", color, roughness=0.6)

    @property
    def water_bed(self) -> Material:
        color = self.style.water_bed or _scale(self.style.water, 0.55)
        return self._make("water_bed", color)

    @property
    def bank(self) -> Material:
        color = self.style.bank or _mix(self.style.ground, self.style.grass, 0.35)
        return self._make("bank", color)

    @property
    def sidewalk(self) -> Material:
        return self._make("sidewalk", self.style.sidewalk)

    @property
    def curb(self) -> Material:
        return self._make("curb", self.style.curb)

    @property
    def parking(self) -> Material:
        return self._make("parking", self.style.parking)

    @property
    def rail(self) -> Material:
        return self._make("rail", self.style.rail, roughness=0.5, metallic=0.6)

    @property
    def water(self) -> Material:
        return self._make(
            "water",
            self.style.water,
            roughness=0.15,
            metallic=0.1,
            opacity=self.style.water_opacity,
        )

    @property
    def grass(self) -> Material:
        return self._make("grass", self.style.grass)

    @property
    def forest_floor(self) -> Material:
        return self._make("forest_floor", self.style.forest_floor)

    @property
    def trunk(self) -> Material:
        return self._make("trunk", self.style.trunk)

    @property
    def window(self) -> Material:
        return self._make(
            "window",
            self.style.window,
            roughness=0.12,
            metallic=0.35,
            emissive=self.style.window_emissive,
        )

    @property
    def door(self) -> Material:
        # Derivada do tronco escurecida: casa com qualquer paleta sem virar campo proprio.
        color = tuple(c * 0.62 for c in self.style.trunk)
        return self._make("door", color, roughness=0.55)

    # --- variantes indexadas ---
    def wall(self, index: int) -> Material:
        pal = self.style.wall_palette
        i = index % len(pal)
        return self._make(f"wall_{i:02d}", pal[i])

    def roof(self, index: int) -> Material:
        pal = self.style.roof_palette
        i = index % len(pal)
        return self._make(f"roof_{i:02d}", pal[i])

    def canopy(self, index: int) -> Material:
        pal = self.style.canopy_palette
        i = index % len(pal)
        return self._make(f"canopy_{i:02d}", pal[i])

    def wall_count(self) -> int:
        return len(self.style.wall_palette)

    def roof_count(self) -> int:
        return len(self.style.roof_palette)

    def canopy_count(self) -> int:
        return len(self.style.canopy_palette)


LOW_POLY = Style(
    name="lowpoly",
    label="Low Poly",
    ground=hexcolor("bcc0a4"),
    asphalt=hexcolor("6f7276"),
    asphalt_major=hexcolor("5d6165"),
    footway=hexcolor("b09878"),
    sidewalk=hexcolor("d6d2c6"),
    curb=hexcolor("aea99c"),
    parking=hexcolor("9a9791"),
    rail=hexcolor("8a8378"),
    water=hexcolor("5b9dc9"),
    grass=hexcolor("8fb96a"),
    forest_floor=hexcolor("6d9451"),
    trunk=hexcolor("7a5a3c"),
    wall_palette=(
        hexcolor("efe7d8"),
        hexcolor("e3d5c1"),
        hexcolor("d9c8ae"),
        hexcolor("cfd6d3"),
        hexcolor("e8dcd0"),
        hexcolor("c6cdc4"),
        hexcolor("ddd2c0"),
        hexcolor("f2ece0"),
    ),
    roof_palette=(
        hexcolor("b5573f"),
        hexcolor("8c5a45"),
        hexcolor("6f6a63"),
        hexcolor("a4634a"),
        hexcolor("5e6b6e"),
        hexcolor("94513c"),
    ),
    canopy_palette=(
        hexcolor("6ea34f"),
        hexcolor("5c9147"),
        hexcolor("7fae5c"),
        hexcolor("4f8140"),
    ),
    window=hexcolor("6b8496"),
    cobble=hexcolor("9a9186"),
    dirt=hexcolor("b3a184"),
    road_line=hexcolor("e6e2d6"),
    water_bed=hexcolor("2f6b8e"),
    bank=hexcolor("bdb49c"),
    height_scale=1.0,
    roof_weights={"flat": 0.40, "gable": 0.38, "hip": 0.22},
)

CARTOON = Style(
    name="cartoon",
    label="Cartoon",
    ground=hexcolor("cfe0a2"),
    asphalt=hexcolor("7d7f96"),
    asphalt_major=hexcolor("6b6d85"),
    footway=hexcolor("e0a86a"),
    sidewalk=hexcolor("f6ead2"),
    curb=hexcolor("d3bb95"),
    parking=hexcolor("9b9bb0"),
    rail=hexcolor("9a8f80"),
    water=hexcolor("35b6e8"),
    grass=hexcolor("6fd05a"),
    forest_floor=hexcolor("46b04a"),
    trunk=hexcolor("8b5a2b"),
    wall_palette=(
        hexcolor("ff8f6b"),
        hexcolor("ffd166"),
        hexcolor("6fd3d6"),
        hexcolor("f6f0e2"),
        hexcolor("b28ce0"),
        hexcolor("7ec8f2"),
        hexcolor("f4a6c8"),
        hexcolor("a8e06a"),
    ),
    roof_palette=(
        hexcolor("e04b3a"),
        hexcolor("2f6fb5"),
        hexcolor("7a4a9e"),
        hexcolor("d9822b"),
        hexcolor("2f9e6e"),
        hexcolor("c1354f"),
    ),
    canopy_palette=(
        hexcolor("52c95a"),
        hexcolor("3fae4a"),
        hexcolor("77d96a"),
        hexcolor("2f9147"),
    ),
    window=hexcolor("bfe9ff"),
    cobble=hexcolor("a99cb0"),
    dirt=hexcolor("d6a86a"),
    road_line=hexcolor("fff8e4"),
    water_bed=hexcolor("1a7ba8"),
    bank=hexcolor("d9c9a2"),
    water_opacity=0.8,
    roughness=0.98,
    height_scale=1.15,
    roof_weights={"flat": 0.25, "gable": 0.50, "hip": 0.25},
)

FUTURISTA = Style(
    name="futurista",
    label="Futurista",
    ground=hexcolor("1c1f2b"),
    asphalt=hexcolor("2a2e3d"),
    asphalt_major=hexcolor("232635"),
    footway=hexcolor("34394b"),
    sidewalk=hexcolor("3a3f52"),
    curb=hexcolor("454b60"),
    parking=hexcolor("262a37"),
    rail=hexcolor("6f7896"),
    water=hexcolor("134a63"),
    grass=hexcolor("1f4a3a"),
    forest_floor=hexcolor("1a3d31"),
    trunk=hexcolor("32384a"),
    wall_palette=(
        hexcolor("2b3145"),
        hexcolor("353b52"),
        hexcolor("232839"),
        hexcolor("3c4460"),
        hexcolor("1e2333"),
        hexcolor("2f3750"),
    ),
    roof_palette=(
        hexcolor("444d6b"),
        hexcolor("2c3348"),
        hexcolor("515b7d"),
    ),
    canopy_palette=(
        hexcolor("2f6f57"),
        hexcolor("28604c"),
        hexcolor("35805f"),
    ),
    window=hexcolor("7fe6ff"),
    window_emissive=hexcolor("3fb6d8"),
    cobble=hexcolor("353c50"),
    dirt=hexcolor("3a4050"),
    road_line=hexcolor("5fd8f0"),
    water_bed=hexcolor("0a2f42"),
    bank=hexcolor("242a38"),
    water_opacity=0.9,
    roughness=0.45,
    height_scale=1.6,
    roof_weights={"flat": 0.85, "gable": 0.0, "hip": 0.15},
)

STYLES: dict[str, Style] = {s.name: s for s in (LOW_POLY, CARTOON, FUTURISTA)}


def get_style(name: str) -> Style:
    try:
        return STYLES[name]
    except KeyError:
        raise KeyError(
            f"estilo desconhecido: {name!r}. Disponiveis: {', '.join(sorted(STYLES))}"
        ) from None


def list_styles() -> list[tuple[str, str]]:
    return [(s.name, s.label) for s in STYLES.values()]
