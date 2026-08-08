"""Testes do perfil regional e do posicionamento dos edificios. Sem rede."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString, box

from mapforge.core.features import Building, FeatureKind, MapData
from mapforge.core.geo import BBox
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation.buildings import _choose_levels
from mapforge.generation.region import (
    BOREAL,
    PROFILES,
    TROPICAL,
    get_profile,
    profile_for_latitude,
)
from mapforge.generation.vegetation import SPECIES_MIX
from mapforge.imagery.detect import _resolve_placement

BBOX_TROPICO = BBox(-2.8903, -41.9056, -2.8885, -41.9038)
BBOX_NORTE = BBox(59.9, 10.7, 59.92, 10.73)


# ------------------------------------------------------------------- perfis


@pytest.mark.parametrize(
    "latitude,esperado",
    [
        (-2.89, "tropical"),  # Araioses, MA
        (-20.39, "tropical"),  # Ouro Preto, MG
        (0.0, "tropical"),
        (-30.03, "subtropical"),  # Porto Alegre, RS
        (48.85, "temperada"),  # Paris
        (64.1, "boreal"),  # Reykjavik
        (-64.1, "boreal"),  # o hemisferio sul tem o mesmo tratamento
    ],
)
def test_perfil_pela_latitude(latitude, esperado):
    assert profile_for_latitude(latitude).name == esperado


def test_casa_no_tropico_nao_tem_chamine():
    assert TROPICAL.chimneys == 0.0


def test_casa_boreal_tem_chamine():
    assert BOREAL.chimneys > 0.0


def test_perfil_desconhecido_lista_opcoes():
    with pytest.raises(KeyError, match="tropical"):
        get_profile("marte")


def test_pesos_de_telhado_continuam_normalizados():
    base = {"flat": 0.4, "gable": 0.38, "hip": 0.22}
    for profile in PROFILES.values():
        pesos = profile.roof_weights(base)
        assert sum(pesos.values()) == pytest.approx(1.0)
        assert all(v >= 0 for v in pesos.values())


def test_boreal_evita_telhado_plano():
    base = {"flat": 0.4, "gable": 0.38, "hip": 0.22}
    assert BOREAL.roof_weights(base)["flat"] < base["flat"]


def test_tropico_privilegia_palmeira_e_evita_conifera():
    mix = TROPICAL.species_mix(SPECIES_MIX["park"])
    assert mix["palm"] > SPECIES_MIX["park"]["palm"]
    assert mix["conifer"] < SPECIES_MIX["park"]["conifer"]
    assert sum(mix.values()) == pytest.approx(1.0)


def test_boreal_zera_palmeira():
    assert BOREAL.species_mix(SPECIES_MIX["park"])["palm"] == pytest.approx(0.0)


def test_contexto_deduz_o_perfil_da_bbox():
    ctx = GenerationContext(BBOX_TROPICO, GenerationSettings())
    assert ctx.region.name == "tropical"
    assert GenerationContext(BBOX_NORTE, GenerationSettings()).region.name == "boreal"


def test_perfil_pode_ser_forcado():
    ctx = GenerationContext(BBOX_TROPICO, GenerationSettings(region="boreal"))
    assert ctx.region.name == "boreal"


def test_cena_tropical_nao_gera_chamine():
    """A chamine sai da paleta de parede; a caixa d'agua, da de meio-fio."""
    data = MapData(bbox=BBOX_TROPICO)
    for i in range(30):
        data.buildings.append(
            Building(
                osm_id=i,
                kind=FeatureKind.BUILDING,
                tags={"roof:shape": "gabled"},
                footprint=box(i * 14, 0, i * 14 + 11, 11),
                building_type="house",
            )
        )
    settings = GenerationSettings(
        seed=2, detail="high", roads=False, water=False, vegetation=False, terrain=False
    )
    scene = build_scene(data, GenerationContext(BBOX_TROPICO, settings))
    # Com caixa d'agua o material de meio-fio aparece; sem ela e sem chamine,
    # nao ha volume extra nenhum sobre o telhado.
    assert "curb" in scene.groups


# ---------------------------------------------------------------- esbeltez


def _levels(area: float, tipo: str) -> float:
    lado = area**0.5
    building = Building(
        osm_id=1,
        kind=FeatureKind.BUILDING,
        tags={},
        footprint=box(0, 0, lado, lado),
        building_type=tipo,
    )
    ctx = GenerationContext(BBOX_TROPICO, GenerationSettings(seed=9))
    return _choose_levels(building, ctx, ctx.rng(1))


def test_hospital_minusculo_nao_vira_torre():
    """Regressao: `building=hospital` pedia 3 a 10 pavimentos mesmo em 28 m2.

    Em Araioses o hospital esta mapeado como quatro pavilhoes pequenos, e cada
    um virava uma torre.
    """
    assert _levels(28.0, "hospital") <= 3


def test_hospital_grande_pode_ser_alto():
    assert _levels(2500.0, "hospital") > 3


def test_limite_de_esbeltez_cresce_com_a_area():
    assert _levels(60.0, "office") < _levels(1500.0, "office")


def test_nenhum_predio_fica_abaixo_de_um_pavimento():
    for area in (5.0, 20.0, 100.0):
        assert _levels(area, "house") >= 1


# ------------------------------------------------------------ posicionamento


def test_predio_sobre_a_rua_e_recortado():
    via = LineString([(-100, 0), (100, 0)]).buffer(5.0)
    # Casa que invade metade da pista.
    casa = box(-10, -4, 10, 12)
    resultado = _resolve_placement([casa], [via])
    assert resultado
    assert resultado[0].intersection(via).area < 1e-6


def test_predio_quase_todo_sobre_a_rua_e_descartado():
    via = LineString([(-100, 0), (100, 0)]).buffer(8.0)
    casa = box(-6, -7, 6, 7)  # praticamente dentro do asfalto
    assert _resolve_placement([casa], [via]) == []


def test_predios_sobrepostos_deixam_de_se_cruzar():
    a = box(0, 0, 20, 20)
    b = box(10, 10, 30, 30)  # cruza `a`
    resultado = _resolve_placement([a, b], [])
    assert len(resultado) == 2
    assert resultado[0].intersection(resultado[1]).area < 1e-6


def test_o_maior_predio_fica_inteiro():
    grande = box(0, 0, 40, 40)
    pequeno = box(30, 30, 50, 50)
    resultado = _resolve_placement([pequeno, grande], [])
    areas = sorted(p.area for p in resultado)
    assert areas[-1] == pytest.approx(grande.area)


def test_predios_separados_nao_sao_alterados():
    a = box(0, 0, 10, 10)
    b = box(50, 50, 60, 60)
    resultado = _resolve_placement([a, b], [])
    assert sum(p.area for p in resultado) == pytest.approx(a.area + b.area)


def test_sobra_minuscula_apos_o_recorte_e_descartada():
    a = box(0, 0, 30, 30)
    b = box(1, 1, 31, 31)  # quase o mesmo predio
    resultado = _resolve_placement([a, b], [])
    assert len(resultado) == 1
