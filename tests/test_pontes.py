"""Rio escavado e ponte sobre ele.

Dois defeitos concretos deram origem a estes testes, e cada um tem aqui o seu
invariante:

1. **Ilha de terra dentro do rio.** A rampa de escavacao dava peso 0,5 na borda
   interna do poligono, entao um vertice logo dentro da margem descia so metade
   do caminho e parava acima da lamina. Medido em Araioses, num rio so: 245
   vertices de terra acima da agua.
2. **A rua mergulhava no leito.** Com relevo, a pista e assentada no terreno - e
   o terreno debaixo do rio acabara de ser escavado. Medido em Ouro Preto, a
   pista sobre a agua ficava 0,37 m abaixo da lamina na mediana e 4,16 m no pior
   caso. Nao era ponte; era vau.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString, box

from mapforge.core.features import FeatureKind, MapData, River, Road, RoadClass
from mapforge.core.geo import BBox
from mapforge.generation import GenerationContext, GenerationSettings, build_scene
from mapforge.generation import layers
from mapforge.generation.bridges import MIN_SPAN_M2, _pillar_spots
from mapforge.generation.terrain import TerrainField
from mapforge.generation.water import _reach_levels, _slice_body
from mapforge.imagery.elevation import ElevationGrid

BBOX = BBox(-20.3900, -43.5080, -20.3814, -43.4992)


def _grid(heights: np.ndarray) -> ElevationGrid:
    return ElevationGrid(heights=heights, bbox=BBOX, zoom=15)


def _vale(inclinacao: float = 0.12, step: float = 10.0) -> TerrainField:
    """Vale em V correndo leste-oeste: o fundo em y = 0, encostas dos dois lados."""
    rows = cols = 90
    half_w, half_h = BBOX.width_m / 2, BBOX.height_m / 2
    xs = np.linspace(-half_w, half_w, cols)
    ys = np.linspace(half_h, -half_h, rows)  # linha 0 = norte
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    # Encosta em V mais um caimento suave para leste, para o rio ter jusante.
    heights = np.abs(mesh_y) * inclinacao - mesh_x * 0.01
    return TerrainField.from_grid(BBOX, _grid(heights), step=step)


def _cena(**kwargs) -> MapData:
    data = MapData(bbox=BBOX)
    data.rivers.append(
        River(
            osm_id=1,
            kind=FeatureKind.RIVER,
            tags={"waterway": "river"},
            centerline=LineString([(-400, 0), (400, 0)]),
            width=kwargs.get("largura", 40.0),
        )
    )
    data.roads.append(
        Road(
            osm_id=2,
            kind=FeatureKind.ROAD,
            tags={},
            centerline=LineString([(0, -300), (0, 300)]),
            road_class=RoadClass.SECONDARY,
        )
    )
    return data


def _contexto(field, **kwargs) -> GenerationContext:
    base = dict(seed=1, buildings=False, vegetation=False, structures=False, street_lamps=False)
    base.update(kwargs)
    return GenerationContext(BBOX, GenerationSettings(**base), terrain=field)


def _centroides(scene, *nomes) -> np.ndarray:
    """Centro de cada triangulo dos grupos pedidos.

    O tabuleiro e a lamina sao quadrilateros grandes: os vertices deles ficam so
    nos cantos, fora do rio. Medir por vertice nao encontraria nenhum ponto
    **sobre** a agua - e foi o que fez a primeira versao destes testes achar que
    a ponte nao existia.
    """
    partes = [
        scene.groups[n].vertices[scene.groups[n].faces].mean(axis=1)
        for n in nomes
        if n in scene.groups
    ]
    return np.concatenate(partes) if partes else np.zeros((0, 3))


def _lamina_em(scene, pontos: np.ndarray) -> np.ndarray:
    """Altura da superficie da agua em cada ponto XY (NaN onde nao ha agua).

    Resolve triangulo a triangulo porque o rio e fatiado em trechos de niveis
    diferentes: uma unica altura media nao descreve a lamina.
    """
    from shapely import STRtree
    from shapely.geometry import Point, Polygon

    grupo = scene.groups["water"]
    triangulos = grupo.vertices[grupo.faces]
    faces = [Polygon(t[:, :2]) for t in triangulos]
    alturas = triangulos[:, :, 2].mean(axis=1)
    arvore = STRtree(faces)

    saida = np.full(len(pontos), np.nan)
    for i, (x, y) in enumerate(pontos[:, :2]):
        ponto = Point(float(x), float(y))
        for j in arvore.query(ponto):
            if faces[j].covers(ponto):
                saida[i] = alturas[j]
                break
    return saida


# ------------------------------------------------------- escavacao sem ilhas


def test_o_leito_inteiro_fica_abaixo_da_lamina():
    """Regressao das ilhas de terra.

    Nenhum vertice de grade debaixo da agua pode sobrar acima dela. A comparacao
    e feita contra a lamina **daquele ponto**, e nao contra uma media: o rio e
    fatiado em trechos de niveis diferentes.
    """
    field = _vale()
    scene = build_scene(_cena(), _contexto(field, roads=False))

    mesh_x, mesh_y = np.meshgrid(field.xs, field.ys)
    pontos = np.column_stack([mesh_x.ravel(), mesh_y.ravel()])
    lamina = _lamina_em(scene, pontos)

    sob_agua = ~np.isnan(lamina)
    assert sob_agua.sum() > 20, "o teste so vale se ha grade debaixo do rio"

    terreno = field.z.ravel()[sob_agua]
    acima = terreno > lamina[sob_agua]
    assert not acima.any(), (
        f"{int(acima.sum())} vertices de terra acima da lamina, "
        f"o pior por {(terreno - lamina[sob_agua]).max():.2f} m"
    )


def test_a_margem_desce_em_rampa_e_nao_em_parede():
    """Dentro o peso e 1 (leito); fora ele decai (talude); longe, nada muda."""
    field = _vale()
    antes = field.z.copy()
    field.carve(box(-300, -20, 300, 20), depth=1.05, level=0.0)

    def em(x: float, y: float):
        j = int(np.abs(field.ys - y).argmin())
        i = int(np.abs(field.xs - x).argmin())
        return field.z[j, i], antes[j, i]

    leito, _ = em(0.0, 0.0)
    talude, talude_antes = em(0.0, 30.0)
    longe, longe_antes = em(0.0, 200.0)

    assert leito == pytest.approx(-1.05, abs=1e-6)  # desceu inteiro
    assert talude < talude_antes  # a margem acompanha
    assert talude > leito  # mas nao ate o fundo
    assert longe == pytest.approx(longe_antes, abs=1e-9)  # longe fica intacto


def test_o_nivel_sai_de_dentro_do_poligono_e_nao_da_caixa():
    """Um rio diagonal preenche pouco da propria caixa envolvente."""
    field = _vale(inclinacao=0.3)
    diagonal = LineString([(-300, -300), (300, 300)]).buffer(12.0)

    dentro = field.level_over(diagonal, percentile=15.0)
    caixa = float(np.percentile(field.height(*np.meshgrid(
        np.linspace(-300, 300, 24), np.linspace(-300, 300, 24)
    )), 15.0))
    # A caixa pega as duas encostas do vale; o corpo, so o fundo.
    assert dentro < caixa


# ------------------------------------------------------------ nivel por trecho


def test_rio_comprido_e_fatiado_em_trechos():
    rio = box(-400, -10, 400, 10)  # 800 x 20 m
    partes = _slice_body(rio, 140.0)
    assert len(partes) >= 5
    assert sum(p.area for p in partes) == pytest.approx(rio.area, rel=1e-6)


def test_lago_nao_e_fatiado():
    """Agua parada tem um nivel so: fatiar inventaria desnivel."""
    assert len(_slice_body(box(-150, -150, 150, 150), 140.0)) == 1


def test_o_nivel_nunca_sobe_para_jusante():
    class TerrenoRuidoso:
        """DEM com um pico no meio do rio - represa que nao existe."""

        def level_over(self, geom, percentile=15.0):
            x = geom.centroid.x
            return 10.0 - x * 0.01 + (6.0 if -30 < x < 30 else 0.0)

    partes = _slice_body(box(-400, -10, 400, 10), 140.0)
    niveis = _reach_levels(partes, TerrenoRuidoso())

    assert len(niveis) == len(partes)
    # Jusante e o lado leste (nivel cai com x). Nenhum trecho pode subir.
    assert all(b <= a + 1e-9 for a, b in zip(niveis, niveis[1:])), niveis


def test_trecho_unico_dispensa_a_suavizacao():
    class Plano:
        def level_over(self, geom, percentile=15.0):
            return 3.0

    assert _reach_levels([box(0, 0, 10, 10)], Plano()) == [3.0]


# -------------------------------------------------------------------- ponte


def _pista_sobre_o_rio(scene):
    """Centros dos triangulos de pavimento que caem sobre a agua."""
    centros = _centroides(scene, "road", "major", "cobble", "dirt")
    lamina = _lamina_em(scene, centros)
    sobre = ~np.isnan(lamina)
    return centros[sobre], lamina[sobre]


def test_a_pista_sobre_o_rio_fica_acima_da_agua():
    """O invariante que separa ponte de vau."""
    field = _vale()
    scene = build_scene(_cena(), _contexto(field))

    pista, lamina = _pista_sobre_o_rio(scene)
    assert len(pista) > 0, "a pista nem chegou ao rio"

    afundado = pista[:, 2] < lamina
    assert not afundado.any(), (
        f"{int(afundado.sum())} pedacos de pista abaixo da agua, "
        f"o pior por {(lamina - pista[:, 2]).max():.2f} m"
    )


def test_o_tabuleiro_e_plano():
    """A ponte nao acompanha o fundo escavado: ela vence o vao."""
    field = _vale()
    scene = build_scene(_cena(), _contexto(field))

    pista, _ = _pista_sobre_o_rio(scene)
    assert len(pista) > 0
    assert pista[:, 2].max() - pista[:, 2].min() < 0.05, "o tabuleiro esta inclinado"


def test_o_tabuleiro_fica_na_altura_da_margem_e_nao_na_do_rio():
    """O encontro e medido alem do talude escavado.

    Medir dentro do talude - que a propria agua rebaixou - fazia a ponte nascer
    quase no nivel do rio, e o pilar era entao descartado por nao caber embaixo
    dela.
    """
    field = _vale()
    scene = build_scene(_cena(), _contexto(field))

    pista, lamina = _pista_sobre_o_rio(scene)
    # A margem do vale, a 20 m do eixo, esta bem acima da agua: a ponte tem de
    # estar na altura dela, nao rente a lamina.
    assert float(np.median(pista[:, 2] - lamina)) > 1.0


def test_a_ponte_tem_guarda_corpo():
    field = _vale()
    com_rio = build_scene(_cena(), _contexto(field))
    sem_rio = build_scene(_cena(), _contexto(_vale(), water=False))

    # O guarda-corpo e a laje entram no material de meio-fio.
    assert "curb" in com_rio.groups
    antes = len(sem_rio.groups.get("curb").faces) if "curb" in sem_rio.groups else 0
    assert len(com_rio.groups["curb"].faces) > antes


def test_o_guarda_corpo_sobe_acima_da_pista():
    """A mureta lateral existe e passa do nivel do tabuleiro.

    Sem ela a ponte le como uma tira de asfalto flutuando sobre o rio.
    """
    field = _vale()
    scene = build_scene(_cena(), _contexto(field))

    pista, _ = _pista_sobre_o_rio(scene)
    topo_pista = float(pista[:, 2].max())

    # Nesta cena o material de meio-fio so e usado pela ponte, entao o ponto
    # mais alto dele e o topo do guarda-corpo.
    assert scene.groups["curb"].vertices[:, 2].max() > topo_pista + 0.5


def test_o_guarda_corpo_deixa_as_entradas_abertas():
    """O anel e cortado onde a ponte encontra o resto da pista.

    Sem esse corte a mureta fecharia a passagem e a ponte viraria uma caixa.
    """
    import shapely

    field = _vale()
    scene = build_scene(_cena(), _contexto(field))

    pista, _ = _pista_sobre_o_rio(scene)
    topo_pista = float(pista[:, 2].max())

    curb = _centroides(scene, "curb")
    mureta = curb[curb[:, 2] > topo_pista + 0.1]
    assert len(mureta) > 0, "nao ha guarda-corpo"

    # A mureta corre ao longo do rio (dois lados), nao atravessada nele.
    eixo = shapely.LineString([(-400.0, 0.0), (400.0, 0.0)])
    lados = np.abs(mureta[:, 0])
    assert lados.min() > 1.0, "ha guarda-corpo no meio da pista"
    assert eixo.distance(shapely.Point(float(mureta[0, 0]), float(mureta[0, 1]))) < 200.0


def test_o_tabuleiro_nao_sobe_muito_acima_da_propria_travessia():
    """Trava de segurança: ponte no ar é pior que ponte baixa.

    A primeira versão media a altura do encontro num anel de pista de 14 m em
    volta da travessia. Em cidade de montanha o anel pega a ladeira que passa
    perto do córrego, e o tabuleiro sobe com ela: medido em Ouro Preto, **19 de
    27 tabuleiros mais de 6 m acima do próprio vão**, dois deles a 99 e 121 m no
    ar. A altura passou a sair da entrada do próprio tabuleiro, e este teto
    limita o estrago quando nem isso existe.
    """
    from mapforge.generation.bridges import MAX_RISE_M, _sample_heights

    field = _vale()
    ctx = _contexto(field)
    scene = build_scene(_cena(), ctx)

    pista, _ = _pista_sobre_o_rio(scene)
    rio = LineString([(-400, 0), (400, 0)]).buffer(20.0)
    margem = float(_sample_heights(ctx, rio).max())

    assert pista[:, 2].max() <= margem + MAX_RISE_M + layers.Z_ROAD + 0.01


def test_vao_largo_recebe_pilar():
    largo = box(-60, -12, 60, 12)  # 120 m de vao
    assert len(_pillar_spots(largo, 24.0)) >= 3


def test_vao_curto_nao_recebe_pilar():
    curto = box(-6, -5, 6, 5)  # 12 m: vence de uma vez
    assert _pillar_spots(curto, 24.0) == []


def test_travessia_minuscula_nao_vira_ponte():
    """Limiar: uma lasca de rua roçando um corrego nao merece tabuleiro."""
    assert MIN_SPAN_M2 > 0


def test_sem_agua_nao_se_constroi_ponte():
    field = _vale()
    scene = build_scene(_cena(), _contexto(field, water=False))
    assert "water" not in scene.groups


def test_a_sinalizacao_nao_desce_dentro_do_rio():
    """A pintura e assentada no relevo, e o relevo ali e o leito escavado."""
    import shapely

    field = _vale()
    scene = build_scene(_cena(), _contexto(field, detail="high"))
    if "road_line" not in scene.groups:
        pytest.skip("esta via nao recebeu sinalizacao")

    linha = scene.groups["road_line"].vertices
    rio = LineString([(-400, 0), (400, 0)]).buffer(16.0)
    dentro = shapely.contains_xy(rio, linha[:, 0], linha[:, 1])
    assert not dentro.any(), f"{int(dentro.sum())} vertices de pintura dentro do rio"
