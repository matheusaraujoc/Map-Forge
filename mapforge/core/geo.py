"""Bounding box e projecao geografica local (lat/lon -> metros)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

# Raio medio da Terra (WGS84) usado na aproximacao equirretangular local.
_METERS_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class BBox:
    """Caixa geografica em graus decimais."""

    south: float
    west: float
    north: float
    east: float

    def __post_init__(self) -> None:
        if self.south >= self.north or self.west >= self.east:
            raise ValueError(
                f"BBox invalida: south<north e west<east sao obrigatorios (recebido {self})"
            )

    @classmethod
    def from_string(cls, text: str) -> "BBox":
        """Aceita 'sul,oeste,norte,leste'."""
        parts = [p.strip() for p in text.replace(";", ",").split(",")]
        if len(parts) != 4:
            raise ValueError("bbox precisa de 4 numeros: sul,oeste,norte,leste")
        return cls(*(float(p) for p in parts))

    @classmethod
    def from_center(cls, lat: float, lon: float, radius_m: float) -> "BBox":
        """Caixa quadrada de lado 2*radius_m centrada em (lat, lon)."""
        dlat = radius_m / _METERS_PER_DEG_LAT
        dlon = radius_m / (_METERS_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))
        return cls(lat - dlat, lon - dlon, lat + dlat, lon + dlon)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.south + self.north) / 2.0, (self.west + self.east) / 2.0)

    @property
    def width_m(self) -> float:
        lat0 = self.center[0]
        return (self.east - self.west) * _METERS_PER_DEG_LAT * math.cos(math.radians(lat0))

    @property
    def height_m(self) -> float:
        return (self.north - self.south) * _METERS_PER_DEG_LAT

    @property
    def area_km2(self) -> float:
        return (self.width_m * self.height_m) / 1e6

    def as_overpass(self) -> str:
        return f"{self.south},{self.west},{self.north},{self.east}"

    def key(self) -> str:
        return ",".join(f"{v:.6f}" for v in (self.south, self.west, self.north, self.east))

    def __str__(self) -> str:  # pragma: no cover - apresentacao
        return self.key()


class LocalProjection:
    """Projecao equirretangular local: graus -> metros num plano tangente ao centro.

    Precisao mais que suficiente na escala de uma cidade (erro < 0.1% em ~25 km).
    A origem (0, 0) fica no centro da bbox, X aponta para leste e Y para o norte.
    """

    def __init__(self, lat0: float, lon0: float):
        self.lat0 = lat0
        self.lon0 = lon0
        self._mx = _METERS_PER_DEG_LAT * math.cos(math.radians(lat0))
        self._my = _METERS_PER_DEG_LAT

    @classmethod
    def for_bbox(cls, bbox: BBox) -> "LocalProjection":
        lat0, lon0 = bbox.center
        return cls(lat0, lon0)

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self._mx, (lat - self.lat0) * self._my)

    def project_many(self, coords: Sequence[tuple[float, float]]) -> np.ndarray:
        """coords: sequencia de (lat, lon). Retorna array (n, 2) em metros."""
        arr = np.asarray(coords, dtype=float)
        if arr.size == 0:
            return np.zeros((0, 2))
        out = np.empty_like(arr)
        out[:, 0] = (arr[:, 1] - self.lon0) * self._mx
        out[:, 1] = (arr[:, 0] - self.lat0) * self._my
        return out

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        return (y / self._my + self.lat0, x / self._mx + self.lon0)


def bbox_polygon_local(bbox: BBox, projection: LocalProjection) -> Iterable[tuple[float, float]]:
    """Retangulo da bbox em coordenadas locais (metros), sentido anti-horario."""
    sw = projection.project(bbox.south, bbox.west)
    se = projection.project(bbox.south, bbox.east)
    ne = projection.project(bbox.north, bbox.east)
    nw = projection.project(bbox.north, bbox.west)
    return [sw, se, ne, nw]
