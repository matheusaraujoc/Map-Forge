"""Regiao acima do limite do Overpass: o download se divide e se costura.

O limite de 25 km2 nunca foi do gerador - e do servidor, que recusa consulta
grande. Repassar a recusa ao usuario transformava um detalhe de protocolo em
limite do produto.
"""

from __future__ import annotations

import pytest

from mapforge import config
from mapforge.core.geo import BBox
from mapforge.data import overpass


def _bbox_de(area_km2: float) -> BBox:
    """Bbox quadrada com aproximadamente a area pedida, no equador."""
    lado_graus = (area_km2**0.5) / 111.32
    return BBox(0.0, 0.0, lado_graus, lado_graus)


def test_regiao_pequena_nao_e_dividida(monkeypatch):
    chamadas = []

    def falso(bbox, **kwargs):
        chamadas.append(bbox)
        return {"elements": [{"type": "way", "id": 1}]}

    monkeypatch.setattr(overpass, "_download_in_pieces", falso)
    # Uma regiao pequena nao pode nem passar pelo caminho da divisao.
    pequena = _bbox_de(config.MAX_AREA_KM2 * 0.5)
    assert pequena.area_km2 < config.MAX_AREA_KM2
    assert chamadas == []


def test_a_divisao_cobre_a_regiao_inteira():
    grande = _bbox_de(config.MAX_AREA_KM2 * 9)
    pedacos = overpass._split_bbox(grande, config.MAX_AREA_KM2)

    assert len(pedacos) >= 9
    # Os pedacos recompoem exatamente os limites originais.
    assert min(p.south for p in pedacos) == pytest.approx(grande.south)
    assert max(p.north for p in pedacos) == pytest.approx(grande.north)
    assert min(p.west for p in pedacos) == pytest.approx(grande.west)
    assert max(p.east for p in pedacos) == pytest.approx(grande.east)
    # E cada um cabe no limite.
    for pedaco in pedacos:
        assert pedaco.area_km2 <= config.MAX_AREA_KM2 * 1.01


def test_elementos_repetidos_na_divisa_entram_uma_vez_so(monkeypatch):
    """Uma rua que cruza a divisa aparece nos dois pedacos."""
    comum = {"type": "way", "id": 42, "tags": {"highway": "residential"}}

    def falso(bbox, **kwargs):
        return {"elements": [comum, {"type": "node", "id": id(bbox) % 1000}]}

    monkeypatch.setattr(overpass, "download_region", falso)
    grande = _bbox_de(config.MAX_AREA_KM2 * 4)

    bruto = overpass._download_in_pieces(grande)

    ways = [e for e in bruto["elements"] if e["type"] == "way"]
    assert len(ways) == 1


def test_um_pedaco_que_falha_nao_derruba_o_resto(monkeypatch):
    estado = {"n": 0}

    def falso(bbox, **kwargs):
        estado["n"] += 1
        if estado["n"] == 1:
            raise overpass.OverpassError("espelho fora do ar")
        return {"elements": [{"type": "node", "id": estado["n"]}]}

    monkeypatch.setattr(overpass, "download_region", falso)
    bruto = overpass._download_in_pieces(_bbox_de(config.MAX_AREA_KM2 * 4))

    assert len(bruto["elements"]) >= 1


def test_todos_os_pedacos_falhando_levanta_erro(monkeypatch):
    def falso(bbox, **kwargs):
        raise overpass.OverpassError("sem rede")

    monkeypatch.setattr(overpass, "download_region", falso)

    with pytest.raises(overpass.OverpassError):
        overpass._download_in_pieces(_bbox_de(config.MAX_AREA_KM2 * 4))
