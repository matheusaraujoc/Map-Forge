"""Testes do modulo Overture: cache, decodificacao e fusao.

A consulta de verdade bate na rede e leva dezenas de segundos, entao aqui so
testamos as partes locais, com a consulta trocada por um dublê.
"""

from shapely.geometry import Polygon

from mapforge.core.features import Building, FeatureKind
from mapforge.core.geo import BBox, LocalProjection
from mapforge.data import merge_into
from mapforge.data.overture import (
    FALLBACK_RELEASE,
    _cache_key,
    _decode,
    _encode,
    attribution,
    latest_release,
)


def _item(x, y, lado=0.0002, **extra):
    poly = Polygon([(x, y), (x + lado, y), (x + lado, y + lado), (x, y + lado)])
    base = {
        "polygon": poly,
        "dataset": "GoogleOpenBuildings",
        "height": None,
        "num_floors": None,
        "subtype": None,
        "class": None,
        "roof_shape": None,
        "roof_color": None,
        "roof_material": None,
    }
    base.update(extra)
    return base


def test_encode_decode_preserva_geometria_e_atributos():
    itens = [_item(-41.905, -2.891, height=8.5, roof_shape="gabled")]
    voltou = _decode(_encode(itens))

    assert len(voltou) == 1
    assert voltou[0]["height"] == 8.5
    assert voltou[0]["roof_shape"] == "gabled"
    assert voltou[0]["polygon"].equals(itens[0]["polygon"])


def test_cache_key_muda_com_a_bbox_e_com_o_release():
    a = BBox(-2.90, -41.91, -2.88, -41.89)
    b = BBox(-2.90, -41.91, -2.88, -41.88)
    assert _cache_key(a, "2026-07-22.0") != _cache_key(b, "2026-07-22.0")
    assert _cache_key(a, "2026-07-22.0") != _cache_key(a, "2026-06-17.0")


def test_latest_release_cai_no_conhecido_sem_rede(monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("sem rede")

    monkeypatch.setattr("mapforge.data.overture.requests.get", explode)
    assert latest_release(cache=None) == FALLBACK_RELEASE


def test_attribution_cita_as_fontes():
    texto = attribution(["GoogleOpenBuildings", "OpenStreetMap", None])
    assert "Overture" in texto
    assert "GoogleOpenBuildings" in texto
    assert "OpenStreetMap" in texto
    assert "ODbL" in texto


def test_merge_into_marca_a_origem_e_traz_os_atributos():
    bbox = BBox(-2.90, -41.91, -2.88, -41.89)
    proj = LocalProjection.for_bbox(bbox)

    class Fake:
        def __init__(self):
            self.bbox = bbox
            self.buildings = []

    md = Fake()
    itens = [_item(-41.900, -2.890, lado=0.0002, height=9.0, num_floors=3,
                   roof_shape="hipped", subtype="residential")]
    added = merge_into(md, itens, proj, source="overture", id_base=-1_000_000)

    assert added == 1
    predio = md.buildings[0]
    assert predio.tags["source"] == "overture"
    assert predio.tags["roof_shape"] == "hipped"
    assert predio.osm_id == -1_000_000
    assert predio.height == 9.0
    assert predio.levels == 3.0
    assert predio.building_type == "residential"


def test_merge_into_nao_duplica_o_que_ja_existe():
    bbox = BBox(-2.90, -41.91, -2.88, -41.89)
    proj = LocalProjection.for_bbox(bbox)
    item = _item(-41.900, -2.890, lado=0.0004)
    ja = Polygon(proj.project_many([(lat, lon) for lon, lat in item["polygon"].exterior.coords]))

    class Fake:
        def __init__(self):
            self.bbox = bbox
            self.buildings = [
                Building(osm_id=1, kind=FeatureKind.BUILDING, tags={}, footprint=ja)
            ]

    md = Fake()
    assert merge_into(md, [item], proj, source="overture") == 0
    assert len(md.buildings) == 1


def test_ids_da_microsoft_e_do_overture_nao_colidem():
    bbox = BBox(-2.90, -41.91, -2.88, -41.89)
    proj = LocalProjection.for_bbox(bbox)

    class Fake:
        def __init__(self):
            self.bbox = bbox
            self.buildings = []

    md = Fake()
    merge_into(md, [_item(-41.9000, -2.8900)], proj, source="overture", id_base=-1_000_000)
    merge_into(md, [_item(-41.9050, -2.8950)], proj, source="microsoft")

    ids = [b.osm_id for b in md.buildings]
    assert len(set(ids)) == len(ids)
