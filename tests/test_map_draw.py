"""A ferramenta de desenho de area, executada de verdade.

Este teste existe porque o mesmo defeito voltou duas vezes e nenhum teste de
Python o pegava: a logica do desenho mora em JavaScript, dentro de `map.html`,
e a ponte para o Python continuava correta enquanto a tela ficava vazia.

O arnes em `tests/js/map_harness.js` carrega o script da pagina com um DOM
minimo e simula o que o usuario faz - ligar a ferramenta, clicar tres vezes,
fechar por duplo clique. Rodado contra a versao com o bug ele acusa: nenhum no
SVG desenhado e um vertice a mais no contorno.

Precisa do `node`. Onde ele nao existir, o teste e pulado - nao vale tornar o
Node dependencia do projeto so por isto.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
MAPA = RAIZ / "mapforge" / "ui" / "map.html"
ARNES = Path(__file__).parent / "js" / "map_harness.js"


def _script_da_pagina() -> str:
    html = MAPA.read_text(encoding="utf-8")
    blocos = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocos, "map.html sem bloco <script>"
    return blocos[-1]


@pytest.fixture(scope="module")
def node() -> str:
    caminho = shutil.which("node")
    if not caminho:
        pytest.skip("node nao encontrado; a checagem do mapa e pulada")
    return caminho


def test_o_script_do_mapa_tem_sintaxe_valida(node, tmp_path):
    destino = tmp_path / "map.js"
    destino.write_text(_script_da_pagina(), encoding="utf-8")

    resultado = subprocess.run(
        [node, "--check", str(destino)], capture_output=True, text=True, timeout=60
    )
    assert resultado.returncode == 0, resultado.stderr


def test_desenhar_area_marca_pontos_e_fecha_o_contorno(node, tmp_path):
    """O caminho completo: liga, clica tres vezes, fecha, envia ao Python."""
    destino = tmp_path / "map.js"
    destino.write_text(_script_da_pagina(), encoding="utf-8")

    resultado = subprocess.run(
        [node, str(ARNES), str(destino)], capture_output=True, text=True, timeout=120
    )
    saida = resultado.stdout

    # A mensagem inteira entra no assert: quando quebrar, o motivo ja vem junto.
    assert resultado.returncode == 0, saida + resultado.stderr
    assert "botao ficou ativo........: true" in saida
    assert "cursor virou cruz........: true" in saida
    assert "poligonos enviados.......: 1" in saida
    assert "vertices no poligono.....: 3" in saida


def test_o_desenho_produz_nos_svg(node, tmp_path):
    """O sintoma relatado era exatamente este: clique entra, nada aparece."""
    destino = tmp_path / "map.js"
    destino.write_text(_script_da_pagina(), encoding="utf-8")

    resultado = subprocess.run(
        [node, str(ARNES), str(destino)], capture_output=True, text=True, timeout=120
    )
    achado = re.search(r"nos SVG apos 3 cliques\.\.\.: (\d+)", resultado.stdout)
    assert achado, resultado.stdout
    # Uma polilinha e tres circulos, no minimo.
    assert int(achado.group(1)) >= 4, resultado.stdout
