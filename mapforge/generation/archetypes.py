"""Arquetipos de edificio a partir do que a cidade declara ser.

O OSM nao diz so "aqui tem um predio": diz que ali e o hospital, a delegacia, o
corpo de bombeiros, a igreja, a escola. Ignorar isso joga fora a informacao mais
barata e mais confiavel que existe no dado.

Cada arquetipo define quantos pavimentos sao plausiveis, que telhado combina, e
se o predio ganha uma marca propria - a torre da igreja e a mais visivel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

Color = tuple[float, float, float]


@dataclass(frozen=True)
class Archetype:
    """Como construir um equipamento urbano reconhecivel."""

    name: str
    levels: tuple[int, int]
    roof: Optional[str] = None  # forca a forma do telhado
    wall_color: Optional[Color] = None
    roof_color: Optional[Color] = None
    tower: bool = False  # campanario / torre lateral
    wide: bool = False  # implantacao horizontal, sem sacada


# amenity=* -> arquetipo
AMENITY = {
    "hospital": Archetype("hospital", (2, 6), wall_color=(0.94, 0.95, 0.96), wide=True),
    "clinic": Archetype("clinica", (1, 2), wall_color=(0.93, 0.95, 0.95)),
    "police": Archetype("policia", (1, 3), wall_color=(0.72, 0.76, 0.84)),
    "fire_station": Archetype(
        "bombeiros", (1, 2), wall_color=(0.78, 0.26, 0.20), roof_color=(0.35, 0.36, 0.38), wide=True
    ),
    "school": Archetype("escola", (1, 3), wall_color=(0.93, 0.90, 0.78), wide=True),
    "college": Archetype("faculdade", (2, 4), wall_color=(0.90, 0.88, 0.80), wide=True),
    "university": Archetype("universidade", (2, 5), wall_color=(0.90, 0.88, 0.80), wide=True),
    "kindergarten": Archetype("creche", (1, 1), wall_color=(0.95, 0.85, 0.62)),
    "place_of_worship": Archetype(
        "igreja", (1, 2), roof="gable", wall_color=(0.96, 0.95, 0.92), tower=True
    ),
    "townhall": Archetype("prefeitura", (2, 3), wall_color=(0.92, 0.91, 0.86)),
    "courthouse": Archetype("forum", (2, 4), wall_color=(0.90, 0.89, 0.85)),
    "bank": Archetype("banco", (1, 3), wall_color=(0.88, 0.89, 0.90)),
    "fuel": Archetype("posto", (1, 1), roof="flat", wall_color=(0.95, 0.93, 0.88), wide=True),
    "marketplace": Archetype("mercado", (1, 1), roof="skillion", wide=True),
    "bus_station": Archetype("rodoviaria", (1, 2), roof="flat", wide=True),
    "prison": Archetype("presidio", (1, 3), wall_color=(0.78, 0.78, 0.76), wide=True),
}

# emergency=* (o corpo de bombeiros as vezes vem so aqui)
EMERGENCY = {
    "fire_station": AMENITY["fire_station"],
    "ambulance_station": Archetype("samu", (1, 2), wall_color=(0.94, 0.95, 0.96)),
}

# building=* que ja carrega o arquetipo
BUILDING = {
    "church": AMENITY["place_of_worship"],
    "chapel": Archetype("capela", (1, 1), roof="gable", tower=True),
    "cathedral": Archetype("catedral", (2, 3), roof="gable", tower=True),
    "mosque": Archetype("mesquita", (1, 2), tower=True),
    "temple": Archetype("templo", (1, 2), roof="gable"),
    "hospital": AMENITY["hospital"],
    "school": AMENITY["school"],
    "train_station": Archetype("estacao", (1, 2), roof="gable", wide=True),
    "warehouse": Archetype("galpao", (1, 1), roof="skillion", wide=True),
    "industrial": Archetype("industria", (1, 2), roof="flat", wide=True),
    "barn": Archetype("celeiro", (1, 1), roof="gable", wall_color=(0.62, 0.32, 0.24)),
    "farm_auxiliary": Archetype("galpao rural", (1, 1), roof="skillion"),
    "greenhouse": Archetype("estufa", (1, 1), roof="gable", wall_color=(0.86, 0.92, 0.90)),
    "stable": Archetype("estabulo", (1, 1), roof="gable", wall_color=(0.66, 0.44, 0.30)),
}

# shop / tourism / office: comercio de rua e hotel
OTHER = {
    ("tourism", "hotel"): Archetype("hotel", (3, 8)),
    ("tourism", "museum"): Archetype("museu", (1, 3), wide=True),
    ("office", "government"): Archetype("orgao publico", (2, 4)),
    ("leisure", "sports_centre"): Archetype("ginasio", (1, 1), roof="flat", wide=True),
    ("leisure", "stadium"): Archetype("estadio", (1, 2), roof="flat", wide=True),
    ("man_made", "works"): Archetype("fabrica", (1, 2), roof="flat", wide=True),
}


def archetype_for(tags: dict[str, str]) -> Optional[Archetype]:
    """Descobre o arquetipo de um edificio pelas tags do OSM.

    A ordem reflete a especificidade: `amenity` e o que a prefeitura declara,
    `emergency` cobre o caso em que so ele existe, e `building` e o ultimo
    recurso porque muita gente marca `building=yes` num hospital.
    """
    amenity = (tags.get("amenity") or "").lower()
    if amenity in AMENITY:
        return AMENITY[amenity]

    emergency = (tags.get("emergency") or "").lower()
    if emergency in EMERGENCY:
        return EMERGENCY[emergency]

    for (key, value), archetype in OTHER.items():
        if (tags.get(key) or "").lower() == value:
            return archetype

    building = (tags.get("building") or "").lower()
    if building in BUILDING:
        return BUILDING[building]

    # Comercio de rua: qualquer shop=* vira loja terrea com fachada.
    if tags.get("shop"):
        return Archetype("loja", (1, 2))
    return None
