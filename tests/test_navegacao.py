"""Voo livre pela cena 3D.

A camera continua orbital: o que o teclado move e o **alvo**, e a camera vai
junto. Assim o voo nao briga com o arrasto do mouse - gira-se a vista com o
mouse e anda-se para onde ela aponta com o teclado.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("PySide6")

from mapforge.ui.viewport import (  # noqa: E402
    FLIGHT_FRACTION,
    FLIGHT_KEYS,
    OrbitCamera,
)


def _camera(azimute_graus: float = 0.0) -> OrbitCamera:
    cam = OrbitCamera()
    cam.target = np.zeros(3)
    cam.distance = 500.0
    cam.azimuth = math.radians(azimute_graus)
    cam.elevation = math.radians(35.0)
    return cam


def test_avancar_move_na_direcao_da_vista():
    cam = _camera(azimute_graus=0.0)
    # Azimute 0: o olho fica a leste, entao a vista aponta para oeste.
    olho = cam.eye()
    cam.fly(10.0, 0.0, 0.0)

    andou = cam.target
    assert np.linalg.norm(andou[:2]) == pytest.approx(10.0, abs=1e-6)
    # Andou aproximando-se de onde a camera olhava.
    assert np.dot(andou[:2], -olho[:2] / np.linalg.norm(olho[:2])) > 0


def test_avancar_nao_mergulha_no_chao():
    """O avanco e horizontal.

    Usar o vetor de visao cheio faria a camera afundar no terreno ao andar para
    a frente com a vista inclinada - que e a posicao normal neste mapa.
    """
    cam = _camera()
    cam.elevation = math.radians(70.0)  # olhando quase para baixo
    cam.fly(50.0, 0.0, 0.0)
    assert cam.target[2] == pytest.approx(0.0, abs=1e-9)


def test_lateral_e_perpendicular_ao_avanco():
    frente = _camera()
    frente.fly(10.0, 0.0, 0.0)
    lado = _camera()
    lado.fly(0.0, 10.0, 0.0)

    assert np.dot(frente.target[:2], lado.target[:2]) == pytest.approx(0.0, abs=1e-6)


def test_subir_so_mexe_na_altura():
    cam = _camera()
    cam.fly(0.0, 0.0, 25.0)
    assert cam.target[2] == pytest.approx(25.0)
    assert np.allclose(cam.target[:2], 0.0)


def test_a_camera_acompanha_o_alvo():
    cam = _camera()
    antes = cam.eye()
    cam.fly(30.0, 0.0, 0.0)
    assert np.linalg.norm(cam.eye() - antes) == pytest.approx(30.0, abs=1e-6)
    # A distancia ao alvo nao muda: voar nao e aproximar.
    assert np.linalg.norm(cam.eye() - cam.target) == pytest.approx(cam.distance, abs=1e-6)


def test_o_giro_da_vista_muda_para_onde_se_anda():
    norte = _camera(azimute_graus=90.0)
    norte.fly(10.0, 0.0, 0.0)
    leste = _camera(azimute_graus=0.0)
    leste.fly(10.0, 0.0, 0.0)
    assert not np.allclose(norte.target, leste.target)


def test_setas_e_wasd_fazem_a_mesma_coisa():
    from PySide6.QtCore import Qt

    assert FLIGHT_KEYS[Qt.Key_Up] == FLIGHT_KEYS[Qt.Key_W]
    assert FLIGHT_KEYS[Qt.Key_Down] == FLIGHT_KEYS[Qt.Key_S]
    assert FLIGHT_KEYS[Qt.Key_Left] == FLIGHT_KEYS[Qt.Key_A]
    assert FLIGHT_KEYS[Qt.Key_Right] == FLIGHT_KEYS[Qt.Key_D]


def test_o_passo_acompanha_a_distancia_da_camera():
    """De longe se cobre a cidade; de perto se anda pela rua.

    Com passo fixo, o que e util a 50 m atravessa o mapa inteiro a 5 km.
    """
    perto, longe = _camera(), _camera()
    perto.distance, longe.distance = 50.0, 5000.0

    perto.fly(perto.distance * FLIGHT_FRACTION, 0.0, 0.0)
    longe.fly(longe.distance * FLIGHT_FRACTION, 0.0, 0.0)

    assert np.linalg.norm(longe.target) == pytest.approx(
        np.linalg.norm(perto.target) * 100.0, rel=1e-6
    )


def test_teclas_opostas_se_cancelam():
    """Segurar as duas nao pode empurrar a camera para um lado."""
    soma = np.zeros(3)
    from PySide6.QtCore import Qt

    for tecla in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right, Qt.Key_Q, Qt.Key_E):
        soma += np.array(FLIGHT_KEYS[tecla])
    assert np.allclose(soma, 0.0)
