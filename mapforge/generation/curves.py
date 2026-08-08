"""Suavizacao de eixos.

As polilinhas do OSM sao angulosas: um cruzamento em T vira um canto de 90 graus
exato e um rio vira uma sequencia de retas. Aqui elas viram curvas.

Duas estrategias, porque os dois casos sao diferentes:

* Vias tem poucos vertices e cantos fechados -> arredonda cada canto com um raio
  proporcional a largura da via, deixando os trechos retos intactos (nao gasta
  triangulo onde nao precisa).
* Rios tem muitos vertices e curvas suaves -> Chaikin global, que transforma o
  ziguezague de vertices em meandro.

As pontas nunca se movem: e o que mantem os cruzamentos conectados depois da
suavizacao.
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString

# Abaixo deste angulo de deflexao o canto ja e praticamente reto.
MIN_TURN = np.radians(8.0)


def _coords(line: LineString) -> np.ndarray:
    return np.asarray(line.coords, dtype=np.float64)


def _dedupe(coords: np.ndarray, tol: float = 1e-6) -> np.ndarray:
    """Remove vertices repetidos, que zeram as normais e quebram os angulos."""
    if len(coords) < 2:
        return coords
    keep = np.ones(len(coords), dtype=bool)
    keep[1:] = np.linalg.norm(np.diff(coords, axis=0), axis=1) > tol
    out = coords[keep]
    return out if len(out) >= 2 else coords[:2]


def chaikin(coords: np.ndarray, iterations: int = 2) -> np.ndarray:
    """Corte de cantos de Chaikin, preservando as duas extremidades.

    Pontos no meio de um trecho reto continuam sobre a reta, entao trechos retos
    nao ganham ondulacao.
    """
    coords = _dedupe(coords)
    for _ in range(max(0, iterations)):
        if len(coords) < 3:
            break
        a = coords[:-1]
        b = coords[1:]
        q = a + 0.25 * (b - a)
        r = a + 0.75 * (b - a)
        merged = np.empty((2 * len(a), 2), dtype=np.float64)
        merged[0::2] = q
        merged[1::2] = r
        coords = np.vstack([coords[0], merged, coords[-1]])
    return coords


def round_corners(
    coords: np.ndarray,
    radius: float,
    segments: int = 3,
    min_turn: float = MIN_TURN,
) -> np.ndarray:
    """Substitui cada canto por um arco (Bezier quadratica com controle no vertice).

    O raio efetivo e limitado a 40% do menor segmento vizinho, para que dois
    cantos proximos nao se sobreponham.
    """
    coords = _dedupe(coords)
    if len(coords) < 3 or radius <= 0 or segments < 1:
        return coords

    out = [coords[0]]
    for i in range(1, len(coords) - 1):
        prev, vertex, nxt = coords[i - 1], coords[i], coords[i + 1]
        to_prev = prev - vertex
        to_next = nxt - vertex
        len_prev = float(np.linalg.norm(to_prev))
        len_next = float(np.linalg.norm(to_next))
        if len_prev < 1e-9 or len_next < 1e-9:
            continue

        to_prev /= len_prev
        to_next /= len_next
        # Deflexao: 0 = reta, pi = inversao completa de sentido.
        turn = np.pi - np.arccos(np.clip(np.dot(to_prev, to_next), -1.0, 1.0))
        if turn < min_turn:
            out.append(vertex)
            continue

        r = min(radius, 0.4 * len_prev, 0.4 * len_next)
        if r < 1e-3:
            out.append(vertex)
            continue

        start = vertex + to_prev * r
        end = vertex + to_next * r
        # Mais segmentos quanto mais fechado o canto.
        n = max(2, min(int(segments * turn / (np.pi / 3.0)) + 1, segments * 3))
        t = np.linspace(0.0, 1.0, n + 1)[:, None]
        arc = (1 - t) ** 2 * start + 2 * (1 - t) * t * vertex + t**2 * end
        out.extend(arc)

    out.append(coords[-1])
    return _dedupe(np.asarray(out, dtype=np.float64))


def offset_polyline(coords: np.ndarray, distance: float, miter_limit: float = 3.0) -> np.ndarray:
    """Desloca a polilinha lateralmente mantendo o numero de vertices.

    Usa normal media nos vertices com correcao de esquadria (miter), de modo que
    as duas bordas de uma faixa continuem paralelas mesmo nas curvas. Serve para
    sinalizacao de solo, onde o buffer do Shapely traria vertices demais.
    """
    coords = _dedupe(coords)
    if len(coords) < 2:
        return coords

    segments = np.diff(coords, axis=0)
    lengths = np.linalg.norm(segments, axis=1)[:, None]
    directions = segments / np.maximum(lengths, 1e-12)
    normals = np.column_stack([directions[:, 1], -directions[:, 0]])

    # Normal de cada vertice: media das arestas vizinhas (pontas usam so a sua).
    vertex_normals = np.empty_like(coords)
    vertex_normals[0] = normals[0]
    vertex_normals[-1] = normals[-1]
    if len(coords) > 2:
        summed = normals[:-1] + normals[1:]
        norms = np.linalg.norm(summed, axis=1)[:, None]
        vertex_normals[1:-1] = np.where(norms > 1e-9, summed / np.maximum(norms, 1e-12), normals[:-1])

    # Correcao de esquadria: alonga a normal para compensar o angulo interno.
    scale = np.ones((len(coords), 1))
    if len(coords) > 2:
        cos_half = np.sum(vertex_normals[1:-1] * normals[:-1], axis=1)[:, None]
        scale[1:-1] = np.clip(1.0 / np.maximum(np.abs(cos_half), 1e-6), 1.0, miter_limit)

    return coords + vertex_normals * scale * distance


def smooth_road(line: LineString, half_width: float, detail: str = "medium") -> LineString:
    """Arredonda os cantos de uma via com raio proporcional a sua largura."""
    segments = {"low": 0, "medium": 2, "high": 3}.get(detail, 2)
    if segments == 0:
        return line
    coords = _coords(line)
    if len(coords) < 3:
        return line
    # Uma esquina real tem raio da ordem da propria largura da pista.
    radius = min(max(half_width * 1.8, 3.0), 14.0)
    rounded = round_corners(coords, radius, segments=segments)
    return LineString(rounded) if len(rounded) >= 2 else line


def smooth_river(line: LineString, detail: str = "medium") -> LineString:
    """Transforma o traçado quebrado de um curso d'agua em meandro."""
    iterations = {"low": 1, "medium": 2, "high": 3}.get(detail, 2)
    coords = _coords(line)
    if len(coords) < 3:
        return line
    smoothed = chaikin(coords, iterations)
    return LineString(smoothed) if len(smoothed) >= 2 else line
