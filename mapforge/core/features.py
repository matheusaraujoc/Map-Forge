"""Objetos internos do mapa: o resultado do parser OSM.

Toda geometria aqui ja esta em metros, no sistema local (X=leste, Y=norte),
usando tipos do Shapely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from shapely.geometry import LineString, Point, Polygon

from .geo import BBox


class FeatureKind(str, Enum):
    BUILDING = "building"
    ROAD = "road"
    WATER = "water"
    PARK = "park"
    FOREST = "forest"
    RAILWAY = "railway"
    PARKING = "parking"
    RIVER = "river"
    TREE = "tree"


class RoadClass(str, Enum):
    """Classificacao viaria que define largura, material e presenca de calcada."""

    MOTORWAY = "motorway"
    TRUNK = "trunk"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"
    RESIDENTIAL = "residential"
    SERVICE = "service"
    PEDESTRIAN = "pedestrian"
    FOOTWAY = "footway"
    CYCLEWAY = "cycleway"
    TRACK = "track"


class BuildingClass(str, Enum):
    """Porte da construcao, derivado da area do poligono."""

    HOUSE = "house"
    TOWNHOUSE = "townhouse"
    BLOCK = "block"
    MALL = "mall"


@dataclass
class Feature:
    """Base de todo objeto do mapa."""

    osm_id: int
    kind: FeatureKind
    tags: dict[str, str] = field(default_factory=dict)

    def tag(self, *names: str) -> Optional[str]:
        for name in names:
            value = self.tags.get(name)
            if value:
                return value
        return None


@dataclass
class Building(Feature):
    footprint: Polygon = None  # type: ignore[assignment]
    levels: Optional[float] = None
    height: Optional[float] = None
    min_height: float = 0.0
    building_type: str = "yes"

    @property
    def area(self) -> float:
        return self.footprint.area if self.footprint else 0.0

    def classify(self) -> BuildingClass:
        area = self.area
        if area < 220:
            return BuildingClass.HOUSE
        if area < 700:
            return BuildingClass.TOWNHOUSE
        if area < 3500:
            return BuildingClass.BLOCK
        return BuildingClass.MALL


@dataclass
class Road(Feature):
    centerline: LineString = None  # type: ignore[assignment]
    road_class: RoadClass = RoadClass.RESIDENTIAL
    lanes: Optional[int] = None
    width: Optional[float] = None
    bridge: bool = False
    tunnel: bool = False
    oneway: bool = False
    layer: int = 0


@dataclass
class Water(Feature):
    geometry: Polygon = None  # type: ignore[assignment]


@dataclass
class River(Feature):
    centerline: LineString = None  # type: ignore[assignment]
    width: float = 8.0


@dataclass
class Park(Feature):
    geometry: Polygon = None  # type: ignore[assignment]


@dataclass
class Forest(Feature):
    geometry: Polygon = None  # type: ignore[assignment]


@dataclass
class Parking(Feature):
    geometry: Polygon = None  # type: ignore[assignment]


@dataclass
class Railway(Feature):
    centerline: LineString = None  # type: ignore[assignment]
    gauge_width: float = 3.2


@dataclass
class Tree(Feature):
    position: Point = None  # type: ignore[assignment]
    height: Optional[float] = None


@dataclass
class MapData:
    """Tudo que o parser extraiu de uma regiao."""

    bbox: BBox
    buildings: list[Building] = field(default_factory=list)
    roads: list[Road] = field(default_factory=list)
    waters: list[Water] = field(default_factory=list)
    rivers: list[River] = field(default_factory=list)
    parks: list[Park] = field(default_factory=list)
    forests: list[Forest] = field(default_factory=list)
    parkings: list[Parking] = field(default_factory=list)
    railways: list[Railway] = field(default_factory=list)
    trees: list[Tree] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "buildings": len(self.buildings),
            "roads": len(self.roads),
            "waters": len(self.waters),
            "rivers": len(self.rivers),
            "parks": len(self.parks),
            "forests": len(self.forests),
            "parkings": len(self.parkings),
            "railways": len(self.railways),
            "trees": len(self.trees),
        }

    def is_empty(self) -> bool:
        return not any(self.summary().values())
