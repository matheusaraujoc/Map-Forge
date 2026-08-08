"""Perfil regional: o que e comum construir em cada faixa do planeta.

Uma casa brasileira nao tem chamine, mas tem caixa d'agua no telhado. Uma casa
alema tem chamine e nao tem caixa d'agua aparente. Gerar os dois em todo lugar
deixa a cena errada nos dois casos.

A latitude e um proxy grosseiro mas honesto: define se ha inverno com
aquecimento a lenha, se o telhado precisa de caimento forte para neve, e que
vegetacao aparece. Nao pretende ser geografia cultural - pretende evitar o
erro obvio.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegionProfile:
    """Tracos construtivos tipicos de uma faixa climatica."""

    name: str
    label: str
    chimneys: float  # probabilidade de chamine numa casa de telhado inclinado
    roof_bias: dict[str, float] = field(default_factory=dict)  # ajuste multiplicativo
    tree_bias: dict[str, float] = field(default_factory=dict)

    def roof_weights(self, base: dict[str, float]) -> dict[str, float]:
        """Aplica o vies regional sobre os pesos de telhado do estilo."""
        adjusted = {k: v * self.roof_bias.get(k, 1.0) for k, v in base.items()}
        total = sum(adjusted.values())
        return {k: v / total for k, v in adjusted.items()} if total > 0 else dict(base)

    def species_mix(self, base: dict[str, float]) -> dict[str, float]:
        adjusted = {k: v * self.tree_bias.get(k, 1.0) for k, v in base.items()}
        total = sum(adjusted.values())
        return {k: v / total for k, v in adjusted.items()} if total > 0 else dict(base)


TROPICAL = RegionProfile(
    name="tropical",
    label="Tropical",
    chimneys=0.0,  # nao se aquece casa onde nao faz frio
    # Telha ceramica de duas aguas e laje plana dominam; mansarda e rara.
    roof_bias={"gable": 1.25, "hip": 1.1, "flat": 1.15, "mansard": 0.1, "skillion": 0.8},
    tree_bias={"palm": 2.4, "umbrella": 1.4, "conifer": 0.25, "pine": 0.15, "cypress": 0.4},
)

SUBTROPICAL = RegionProfile(
    name="subtropical",
    label="Subtropical",
    chimneys=0.12,
    roof_bias={"gable": 1.15, "mansard": 0.4, "skillion": 0.9},
    tree_bias={"palm": 1.2, "conifer": 0.7, "pine": 0.8},
)

TEMPERATE = RegionProfile(
    name="temperada",
    label="Temperada",
    chimneys=0.45,
    roof_bias={"gable": 1.2, "mansard": 1.2},
    tree_bias={"palm": 0.05, "conifer": 1.3, "pine": 1.3},
)

BOREAL = RegionProfile(
    name="boreal",
    label="Boreal",
    chimneys=0.62,
    # Caimento forte para a neve escorrer: quase nada de telhado plano.
    roof_bias={"gable": 1.6, "hip": 1.2, "flat": 0.35, "skillion": 0.7},
    tree_bias={"palm": 0.0, "conifer": 2.0, "pine": 2.0, "umbrella": 0.4},
)

PROFILES = {p.name: p for p in (TROPICAL, SUBTROPICAL, TEMPERATE, BOREAL)}


def profile_for_latitude(latitude: float) -> RegionProfile:
    """Perfil pela latitude absoluta, nos limites climaticos classicos."""
    absolute = abs(latitude)
    if absolute <= 23.5:  # entre os tropicos
        return TROPICAL
    if absolute <= 35.0:
        return SUBTROPICAL
    if absolute <= 55.0:
        return TEMPERATE
    return BOREAL


def get_profile(name: str) -> RegionProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(
            f"perfil regional desconhecido: {name!r}. Disponiveis: {', '.join(sorted(PROFILES))}"
        ) from None


def list_profiles() -> list[tuple[str, str]]:
    return [(p.name, p.label) for p in PROFILES.values()]

