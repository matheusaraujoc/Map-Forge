"""A ponte entre o mapa (JavaScript) e o Python.

E a fronteira onde uma falha some sem mensagem: se a conversao do contorno nao
casar, o desenho simplesmente nao chega, e da interface parece que a ferramenta
nao funciona.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from mapforge.ui.map_widget import MapBridge  # noqa: E402


def _coletar(bridge):
    recebidos = {"poligono": [], "selecao": []}
    bridge.polygonChanged.connect(lambda p: recebidos["poligono"].append(p))
    bridge.selectionChanged.connect(lambda b: recebidos["selecao"].append(b))
    return recebidos


def test_contorno_plano_vira_lista_de_pontos():
    """O JS manda [lat, lon, lat, lon, ...] porque o canal nao leva objetos."""
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setPolygon([-20.39, -43.51, -20.38, -43.51, -20.38, -43.50])

    assert recebidos["poligono"] == [
        [(-20.39, -43.51), (-20.38, -43.51), (-20.38, -43.50)]
    ]


def test_o_contorno_tambem_define_a_bbox():
    """Os downloads sao sempre retangulares, entao a bbox tem de vir junto."""
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setPolygon([-20.39, -43.51, -20.38, -43.51, -20.38, -43.50])

    bbox = recebidos["selecao"][0]
    assert bbox is not None
    assert bbox.south == pytest.approx(-20.39)
    assert bbox.north == pytest.approx(-20.38)
    assert bbox.west == pytest.approx(-43.51)
    assert bbox.east == pytest.approx(-43.50)


def test_numeros_vindos_como_texto_nao_derrubam_a_ponte():
    """O canal pode entregar numero como string dependendo do trajeto."""
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setPolygon(["-20.39", "-43.51", "-20.38", "-43.51", "-20.38", "-43.50"])

    assert len(recebidos["poligono"]) == 1
    assert len(recebidos["poligono"][0]) == 3


@pytest.mark.parametrize(
    "entrada",
    [
        [],
        [-20.39, -43.51],  # um ponto so
        [-20.39, -43.51, -20.38, -43.51],  # dois pontos: nao fecha area
        [-20.39, -43.51, -20.38],  # quantidade impar
        ["x", "y", "z", "w", "a", "b"],  # nao numerico
    ],
)
def test_contorno_invalido_e_ignorado_em_silencio(entrada):
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setPolygon(entrada)

    assert recebidos["poligono"] == []
    assert recebidos["selecao"] == []


def test_contorno_degenerado_nao_vira_bbox_invalida():
    """Todos os pontos no mesmo lugar: a BBox recusaria, e nao pode explodir."""
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setPolygon([-20.39, -43.51] * 3)

    assert recebidos["selecao"] == []


def test_retangulo_zera_o_contorno_desenhado():
    """Escolher retangulo tem de descartar o desenho, senao os dois valem."""
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setSelection(-20.39, -43.51, -20.38, -43.50)

    assert recebidos["poligono"] == [None]
    assert recebidos["selecao"][0] is not None


def test_limpar_zera_os_dois():
    bridge = MapBridge()
    recebidos = _coletar(bridge)

    bridge.setSelection(0.0, 0.0, 0.0, 0.0)

    assert recebidos["poligono"] == [None]
    assert recebidos["selecao"] == [None]
