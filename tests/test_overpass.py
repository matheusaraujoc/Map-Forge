"""Testes do cliente Overpass: espelhos, fallback e verbosidade. Sem rede real."""

from __future__ import annotations

import logging

import pytest

from mapforge import config
from mapforge.core.geo import BBox
from mapforge.data.cache import Cache
from mapforge.data.overpass import LAST_GOOD_KEY, OverpassError, _endpoint_order, _request

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


class _Resposta:
    """Resposta minima no formato que o cliente espera."""

    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {"elements": []}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _mock_post(monkeypatch, comportamento):
    """Substitui requests.post por uma funcao host -> resposta (ou excecao)."""
    chamadas: list[str] = []

    def fake_post(url, **kwargs):
        chamadas.append(url)
        resultado = comportamento(url)
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    monkeypatch.setattr("mapforge.data.overpass.requests.post", fake_post)
    return chamadas


# ------------------------------------------------------------------- ordem


def test_sem_preferencia_usa_a_ordem_da_configuracao():
    assert _endpoint_order(None) == list(config.OVERPASS_ENDPOINTS)


def test_espelho_preferido_vai_para_a_frente(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        escolhido = config.OVERPASS_ENDPOINTS[-1]
        cache.set_setting(LAST_GOOD_KEY, escolhido)
        assert _endpoint_order(cache)[0] == escolhido


def test_ordem_preserva_todos_os_espelhos(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        cache.set_setting(LAST_GOOD_KEY, config.OVERPASS_ENDPOINTS[-1])
        assert sorted(_endpoint_order(cache)) == sorted(config.OVERPASS_ENDPOINTS)


def test_preferencia_desconhecida_e_ignorada(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        cache.set_setting(LAST_GOOD_KEY, "https://servidor-que-sumiu/api")
        assert _endpoint_order(cache) == list(config.OVERPASS_ENDPOINTS)


# ---------------------------------------------------------------- fallback


def test_espelho_ocupado_cai_para_o_proximo(monkeypatch):
    """504 e a resposta normal de um Overpass ocupado; o proximo assume."""
    primeiro = config.OVERPASS_ENDPOINTS[0]
    chamadas = _mock_post(
        monkeypatch,
        lambda url: _Resposta(504) if url == primeiro else _Resposta(200, {"elements": [1, 2]}),
    )
    data = _request("consulta", timeout=10, progress=None, max_retries=1)
    assert data["elements"] == [1, 2]
    assert len(chamadas) == 2  # tentou o ocupado e depois o que respondeu


def test_fallback_bem_sucedido_nao_gera_warning(monkeypatch, caplog):
    """Regressao: o console assustava com WARNING mesmo quando dava certo."""
    primeiro = config.OVERPASS_ENDPOINTS[0]
    _mock_post(
        monkeypatch,
        lambda url: _Resposta(504) if url == primeiro else _Resposta(200, {"elements": []}),
    )
    with caplog.at_level(logging.INFO, logger="mapforge.data.overpass"):
        _request("consulta", timeout=10, progress=None, max_retries=1)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # A informacao continua disponivel, so que em INFO.
    assert any("indisponivel" in r.message for r in caplog.records)


def test_falha_total_gera_warning_e_erro_claro(monkeypatch, caplog):
    _mock_post(monkeypatch, lambda url: _Resposta(504))
    with caplog.at_level(logging.WARNING, logger="mapforge.data.overpass"):
        with pytest.raises(OverpassError, match="espelhos do Overpass"):
            _request("consulta", timeout=10, progress=None, max_retries=1)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_progresso_avisa_a_troca_de_espelho(monkeypatch):
    primeiro = config.OVERPASS_ENDPOINTS[0]
    _mock_post(
        monkeypatch,
        lambda url: _Resposta(504) if url == primeiro else _Resposta(200, {"elements": []}),
    )
    mensagens: list[str] = []
    _request("consulta", timeout=10, progress=lambda m, f: mensagens.append(m), max_retries=1)
    assert any("ocupado" in m for m in mensagens)


def test_resposta_sem_elements_conta_como_falha(monkeypatch):
    _mock_post(monkeypatch, lambda url: _Resposta(200, {"algo": "errado"}))
    with pytest.raises(OverpassError):
        _request("consulta", timeout=10, progress=None, max_retries=1)


def test_erro_de_conexao_tambem_cai_para_o_proximo(monkeypatch):
    primeiro = config.OVERPASS_ENDPOINTS[0]
    _mock_post(
        monkeypatch,
        lambda url: (
            ConnectionError("recusou") if url == primeiro else _Resposta(200, {"elements": [7]})
        ),
    )
    assert _request("consulta", timeout=10, progress=None, max_retries=1)["elements"] == [7]


# ------------------------------------------------- espelho sem cobertura
#
# Regressao seria: `overpass.osm.ch` responde rapido e com sucesso para o
# Brasil, mas so tem a Suica - devolve zero elementos. A preferencia de espelho
# o memorizou e o mapa passou a sair vazio, sem erro nenhum.


def test_espelho_que_devolve_vazio_cede_para_o_proximo(monkeypatch):
    primeiro = config.OVERPASS_ENDPOINTS[0]
    _mock_post(
        monkeypatch,
        lambda url: _Resposta(200, {"elements": []})
        if url == primeiro
        else _Resposta(200, {"elements": [1, 2, 3]}),
    )
    data = _request("consulta", timeout=10, progress=None, max_retries=1)
    assert data["elements"] == [1, 2, 3]


def test_espelho_vazio_nao_e_memorizado(monkeypatch, tmp_path):
    vazio = config.OVERPASS_ENDPOINTS[0]
    cheio = config.OVERPASS_ENDPOINTS[-1]
    _mock_post(
        monkeypatch,
        lambda url: _Resposta(200, {"elements": []})
        if url == vazio
        else _Resposta(200, {"elements": [1]}),
    )
    with Cache(tmp_path / "t.db") as cache:
        _request("consulta", timeout=10, progress=None, max_retries=1, cache=cache)
        assert cache.get_setting(LAST_GOOD_KEY) == cheio


def test_vazio_de_todos_os_espelhos_e_aceito(monkeypatch):
    """Se todos concordam, a regiao e mesmo deserta - nao pode virar erro."""
    _mock_post(monkeypatch, lambda url: _Resposta(200, {"elements": []}))
    assert _request("consulta", timeout=10, progress=None, max_retries=1)["elements"] == []


def test_todos_os_espelhos_configurados_sao_planetarios():
    """Espelho regional envenena o cache; nenhum pode entrar na lista."""
    regionais = ("osm.ch", "osm.jp", "osm.fr", "osm.be", "osm.pl")
    for url in config.OVERPASS_ENDPOINTS:
        assert not any(r in url for r in regionais), url


# -------------------------------------------------------------- cache


def test_resposta_vazia_nao_vai_para_o_cache(monkeypatch, tmp_path):
    from mapforge.core.geo import BBox
    from mapforge.data.overpass import download_region

    _mock_post(monkeypatch, lambda url: _Resposta(200, {"elements": []}))
    with Cache(tmp_path / "t.db") as cache:
        download_region(BBOX, cache=cache, max_retries=1)
        assert cache.list_osm() == []


def test_cache_vazio_e_ignorado_e_a_consulta_refeita(monkeypatch, tmp_path):
    """Um vazio ja gravado nao pode ficar colado para sempre."""
    from mapforge.core.geo import BBox
    from mapforge.data.overpass import QUERY_TEMPLATE, _cache_key, download_region

    query = QUERY_TEMPLATE.format(bbox=BBOX.as_overpass(), timeout=180)
    with Cache(tmp_path / "t.db") as cache:
        cache.put_osm(_cache_key(BBOX, query), BBOX.key(), {"elements": []})

        _mock_post(monkeypatch, lambda url: _Resposta(200, {"elements": [1, 2]}))
        data = download_region(BBOX, cache=cache)
        assert data["elements"] == [1, 2]  # consultou de novo em vez de crer no vazio


def test_cache_com_dados_e_reaproveitado(monkeypatch, tmp_path):
    from mapforge.core.geo import BBox
    from mapforge.data.overpass import QUERY_TEMPLATE, _cache_key, download_region

    query = QUERY_TEMPLATE.format(bbox=BBOX.as_overpass(), timeout=180)
    with Cache(tmp_path / "t.db") as cache:
        cache.put_osm(_cache_key(BBOX, query), BBOX.key(), {"elements": [9]})

        def nao_deve_chamar(url, **kwargs):
            raise AssertionError("nao devia consultar a rede: havia cache valido")

        monkeypatch.setattr("mapforge.data.overpass.requests.post", nao_deve_chamar)
        assert download_region(BBOX, cache=cache)["elements"] == [9]


# -------------------------------------------------------------- memoria


def test_espelho_que_funcionou_e_memorizado(monkeypatch, tmp_path):
    bom = config.OVERPASS_ENDPOINTS[-1]
    _mock_post(
        monkeypatch,
        # Com dados de verdade: resposta vazia nao conta como espelho bom.
        lambda url: _Resposta(200, {"elements": [1]}) if url == bom else _Resposta(504),
    )
    with Cache(tmp_path / "t.db") as cache:
        _request("consulta", timeout=10, progress=None, max_retries=1, cache=cache)
        assert cache.get_setting(LAST_GOOD_KEY) == bom
        # E na proxima vez ele vem primeiro.
        assert _endpoint_order(cache)[0] == bom


def test_sem_cache_o_cliente_continua_funcionando(monkeypatch):
    _mock_post(monkeypatch, lambda url: _Resposta(200, {"elements": []}))
    assert _request("consulta", timeout=10, progress=None, max_retries=1, cache=None) is not None


def test_todos_os_espelhos_sao_https():
    """Consulta de mapa nao deve sair em texto claro."""
    assert all(u.startswith("https://") for u in config.OVERPASS_ENDPOINTS)


def test_ha_mais_de_um_espelho():
    assert len(config.OVERPASS_ENDPOINTS) >= 2


# ------------------------------------------------------- verbosidade da GUI


def test_flag_do_chromium_e_definida(monkeypatch):
    """O Chromium reclama de interfaces do Windows que a placa nao expoe."""
    from mapforge.ui.app import _quiet_chromium

    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    _quiet_chromium()
    import os

    assert "--log-level" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]


def test_flag_do_usuario_e_respeitada(monkeypatch):
    from mapforge.ui.app import _quiet_chromium

    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--minha-flag")
    _quiet_chromium()
    import os

    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--minha-flag"
