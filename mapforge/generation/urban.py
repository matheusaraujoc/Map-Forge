"""Escala do assentamento, medida nos proprios contornos.

Araioses-MA nao tem predio de dez andares. Nem Tiradentes-MG. A regra que
escolhia pavimentos so olhava a area do contorno - e area grande virava predio
alto -, entao um galpao de 1.000 m2 num povoado do delta do Parnaiba nascia com
doze pavimentos e 42 m de altura. Numa metropole a mesma regra esta certa.

O que faltava era o contexto: *que tipo de lugar e este?* Nao ha essa informacao
pronta e confiavel (o `place=*` do OSM falta justamente onde o mapeamento e
ralo), mas ela esta no proprio conjunto de contornos. Medido em janelas de
1,4 km com edificios do Overture:

    regiao                       n   ocup%   mediana   p99
    Araioses-MA (povoado)     1543     9,7        96   531
    Tiradentes-MG (historica) 1533     9,8        84   719
    Parnaiba-PI (media)       2862    27,6       137  1085
    Sao Paulo-SP (centro)     3619    45,6       162  1508
    Belo Horizonte-MG (centro)1632    28,8       187  2761

O sinal que decide e o **p99 da area do contorno**: existe predio grande aqui?
Ele e monotonico na tabela e resiste ao galpao isolado, que e o que estraga o
maximo (Araioses tem um contorno de 4.339 m2 e ainda assim p99 de 531).

A taxa de ocupacao entra so como piso. Ela parece um bom sinal - 9,7 / 9,8 /
27,6 / 45,6 - ate Belo Horizonte, onde a janela pega o Parque Municipal e cai
para 28,8, no mesmo patamar de Parnaiba. Como corte principal ela rebaixaria um
centro de metropole a cidade pequena; como piso para nucleo saturado, funciona.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UrbanProfile:
    """Teto de pavimentos para predio que nao traz altura em tag."""

    key: str
    label: str
    max_levels: int
    occupancy: float = 0.0
    p99_area: float = 0.0

    def describe(self) -> str:
        return (
            f"{self.label} (ocupacao {self.occupancy:.1f}%, "
            f"p99 {self.p99_area:.0f} m2, teto {self.max_levels} pavimentos)"
        )


# Ordem crescente de porte. O teto e o limite superior de pavimentos para quem
# nao tem tag; predio com `building:levels` no OSM continua obedecendo a tag.
SCALES: tuple[UrbanProfile, ...] = (
    UrbanProfile("povoado", "povoado / vilarejo", 2),
    UrbanProfile("pequena", "cidade pequena", 4),
    UrbanProfile("media", "cidade media", 8),
    UrbanProfile("grande", "cidade grande", 14),
)

# Limite superior de cada faixa de p99 de area (m2). A ultima nao tem teto.
P99_AREA_BREAKS = (600.0, 900.0, 1300.0)

# Ocupacao a partir da qual a regiao e nucleo saturado, independente do porte
# dos contornos: nao existe povoado com metade do solo sob laje.
DENSE_OCCUPANCY = 42.0
DENSE_FLOOR = 2  # indice minimo em SCALES quando a ocupacao passa do corte

# Abaixo disso a amostra e pequena demais para dizer qualquer coisa.
MIN_SAMPLE = 25


def _bucket(value: float, breaks: tuple[float, ...]) -> int:
    return int(np.searchsorted(np.asarray(breaks), value, side="right"))


def profile_for(map_data, bbox) -> UrbanProfile:
    """Classifica a regiao pelo porte, so olhando os contornos que ela tem."""
    areas = np.array(
        [b.area for b in map_data.buildings if b.footprint is not None and b.area > 0.0]
    )
    if len(areas) < MIN_SAMPLE:
        # Sem contorno suficiente nao ha o que medir. "Cidade pequena" e o palpite
        # que erra menos: nao poe torre onde nao deve nem achata um centro urbano.
        log.info("Perfil urbano: amostra de %d contornos, assumindo cidade pequena", len(areas))
        return SCALES[1]

    area_regiao = max(bbox.width_m * bbox.height_m, 1.0)
    ocupacao = 100.0 * float(areas.sum()) / area_regiao
    p99 = float(np.percentile(areas, 99))

    indice = _bucket(p99, P99_AREA_BREAKS)
    if ocupacao >= DENSE_OCCUPANCY:
        indice = max(indice, DENSE_FLOOR)
    escolhido = SCALES[indice]

    perfil = UrbanProfile(
        key=escolhido.key,
        label=escolhido.label,
        max_levels=escolhido.max_levels,
        occupancy=ocupacao,
        p99_area=p99,
    )
    log.info("Perfil urbano: %s", perfil.describe())
    return perfil
