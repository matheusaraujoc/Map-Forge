"""Procedencia da geometria: quem criou cada triangulo, e o que se ve de cima.

Este modulo existe porque uma pergunta simples nao tinha resposta no projeto:
**"de onde saiu essa chapa que apareceu no mapa?"**

A cena e um dicionario de material -> malha. O material diz *como* a superficie
e pintada, e nao *quem* a construiu: `curb` pode ser meio-fio, embasamento de
predio, laje de ponte ou pilar de viaduto; `sidewalk` pode ser calcada ou topo
de platibanda. Sem saber a origem, investigar um defeito visual vira adivinhacao
- e foi exatamente o que aconteceu ao caçar umas chapas claras que apareciam
sobre as ruas: tres hipoteses plausiveis, tres medicoes, tres becos sem saida.

Duas medidas, e a juncao delas e o que responde de verdade:

- **procedencia** (`GeometryLog`): cada lote de triangulos que entra no
  MeshBuilder anota qual funcao de qual modulo o produziu. O gancho fica em
  `add_mesh`, que e o funil por onde tudo passa - uma linha cobre a cena
  inteira.
- **visibilidade** (`top_down`): rasteriza a cena de cima e guarda, por celula,
  qual **face** ficou por cima. Area no arquivo nao e area vista: um telhado de
  300 m2 debaixo de outra coisa nao aparece, e uma faixa de 2 m ao longo de toda
  rua aparece muito.

Juntando as duas, a pergunta vira respondivel: *"o que se ve naquele ponto, de
que material, criado por qual gerador"*.

Nada disto roda por padrao. Instrumentar custa uma chamada de pilha por lote.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

# Modulos que sao "encanamento": a origem interessante esta acima deles na pilha.
_ENCANAMENTO = {"mesh.py", "diagnostics.py"}

# Cosseno minimo com o eixo vertical para uma face contar como horizontal.
HORIZONTAL = 0.985


def _origem(pulos: int = 2) -> str:
    """Modulo e funcao que pediram a geometria, ignorando o encanamento."""
    quadro = sys._getframe(pulos)
    while quadro is not None:
        nome = Path(quadro.f_code.co_filename).name
        if nome not in _ENCANAMENTO:
            return f"{Path(nome).stem}.{quadro.f_code.co_name}"
        quadro = quadro.f_back
    return "desconhecido"


@dataclass
class Lote:
    """Um lote de triangulos com a mesma origem, dentro de um material."""

    material: str
    origem: str
    primeira_face: int  # indice da primeira face dentro do grupo do material
    faces: int
    horizontais: int
    area_h: float  # area das faces horizontais, em m2
    z_min: float
    z_max: float
    bounds: tuple[float, float, float, float]

    @property
    def ultima_face(self) -> int:
        return self.primeira_face + self.faces


@dataclass
class GeometryLog:
    """Registro de procedencia. Ligado, o MeshBuilder anota cada lote aqui."""

    lotes: list[Lote] = field(default_factory=list)
    _proxima_face: dict[str, int] = field(default_factory=dict)

    def record(self, material: str, vertices: np.ndarray, faces: np.ndarray) -> None:
        if len(faces) == 0:
            return
        tri = vertices[faces]
        normais = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        norma = np.linalg.norm(normais, axis=1)
        valida = norma > 1e-9
        horiz = np.zeros(len(faces), dtype=bool)
        horiz[valida] = np.abs(normais[valida, 2]) / norma[valida] > HORIZONTAL

        inicio = self._proxima_face.get(material, 0)
        self._proxima_face[material] = inicio + len(faces)
        self.lotes.append(
            Lote(
                material=material,
                origem=_origem(3),
                primeira_face=inicio,
                faces=len(faces),
                horizontais=int(horiz.sum()),
                area_h=float((0.5 * norma[horiz]).sum()),
                z_min=float(vertices[:, 2].min()),
                z_max=float(vertices[:, 2].max()),
                bounds=(
                    float(vertices[:, 0].min()),
                    float(vertices[:, 1].min()),
                    float(vertices[:, 0].max()),
                    float(vertices[:, 1].max()),
                ),
            )
        )

    # ------------------------------------------------------------------ consulta

    def dono(self, material: str, face: int) -> Optional[Lote]:
        """Qual lote criou esta face do material."""
        for lote in self.lotes:
            if lote.material == material and lote.primeira_face <= face < lote.ultima_face:
                return lote
        return None

    def por_origem(self) -> dict[tuple[str, str], dict]:
        """Agrega por (origem, material)."""
        saida: dict[tuple[str, str], dict] = {}
        for lote in self.lotes:
            chave = (lote.origem, lote.material)
            alvo = saida.setdefault(
                chave, {"lotes": 0, "faces": 0, "horizontais": 0, "area_h": 0.0}
            )
            alvo["lotes"] += 1
            alvo["faces"] += lote.faces
            alvo["horizontais"] += lote.horizontais
            alvo["area_h"] += lote.area_h
        return saida

    def summary(self) -> str:
        origens = {lote.origem for lote in self.lotes}
        return (
            f"{len(self.lotes)} lotes de {len(origens)} origens, "
            f"{sum(l.faces for l in self.lotes):,} triangulos"
        )


# ----------------------------------------------------------------- visibilidade


@dataclass
class Visao:
    """Resultado de uma rasterizacao de cima. Caro de produzir: reaproveite."""

    nomes: list
    material: np.ndarray  # indice em `nomes`, -1 onde nao ha nada
    face: np.ndarray  # indice da face dentro do grupo daquele material
    xs: np.ndarray
    ys: np.ndarray
    topo: np.ndarray
    passo: float


# Teto de celulas da rasterizacao.
#
# Sem ele o diagnostico **trava a geracao**: numa regiao de 11 km2 a 0,6 m a
# grade tem 8,9 milhoes de celulas, e cada triangulo do terreno pinta umas
# duzentas delas. Com 800 mil triangulos isso passa de minutos - e como o
# retrato roda dentro da thread de geracao, a interface fica "carregando" sem
# fim. O passo afrouxa ate caber; o diagnostico quer identificar chapas de
# metros, nao centimetros.
MAX_CELULAS = 2_500_000


def top_down(scene, passo: float = 0.5, janela=None, ignorar=()) -> Optional[Visao]:
    """Rasteriza a cena de cima, guardando material e face por celula.

    Guarda a **face**, e nao so o material: e o que permite perguntar ao log de
    procedencia quem construiu aquilo. A projecao e ortografica de cima porque a
    pergunta e "o que cobre este ponto do chao".
    """
    limites = janela
    if limites is None:
        cantos = [g.vertices for g in scene.groups.values() if len(g.faces)]
        if not cantos:
            return None
        todos = np.concatenate(cantos)
        limites = (
            float(todos[:, 0].min()), float(todos[:, 1].min()),
            float(todos[:, 0].max()), float(todos[:, 1].max()),
        )

    minx, miny, maxx, maxy = limites
    celulas = ((maxx - minx) / passo) * ((maxy - miny) / passo)
    if celulas > MAX_CELULAS:
        passo *= float(np.sqrt(celulas / MAX_CELULAS))
        logging.getLogger(__name__).info(
            "Diagnostico: passo afrouxado para %.2f m (a regiao nao cabe na grade)",
            passo,
        )

    largura = max(int((maxx - minx) / passo), 1)
    altura = max(int((maxy - miny) / passo), 1)
    xs = minx + (np.arange(largura) + 0.5) * passo
    ys = miny + (np.arange(altura) + 0.5) * passo
    gx, gy = np.meshgrid(xs, ys)

    topo = np.full(gx.shape, -np.inf, dtype=np.float32)
    qual_material = np.full(gx.shape, -1, dtype=np.int32)
    qual_face = np.full(gx.shape, -1, dtype=np.int64)

    nomes = [n for n in scene.groups if n not in ignorar]
    for indice, nome in enumerate(nomes):
        grupo = scene.groups[nome]
        if len(grupo.faces) == 0:
            continue
        tri = grupo.vertices[grupo.faces]
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        cx0 = np.minimum.reduce([a[:, 0], b[:, 0], c[:, 0]])
        cx1 = np.maximum.reduce([a[:, 0], b[:, 0], c[:, 0]])
        cy0 = np.minimum.reduce([a[:, 1], b[:, 1], c[:, 1]])
        cy1 = np.maximum.reduce([a[:, 1], b[:, 1], c[:, 1]])
        # Triangulo menor que meia celula nao muda o resultado e domina o custo.
        interessa = np.nonzero((cx1 - cx0) * (cy1 - cy0) > passo * passo)[0]

        for f in interessa:
            i0 = max(int((cx0[f] - minx) / passo), 0)
            i1 = min(int((cx1[f] - minx) / passo) + 2, largura)
            j0 = max(int((cy0[f] - miny) / passo), 0)
            j1 = min(int((cy1[f] - miny) / passo) + 2, altura)
            if i1 <= i0 or j1 <= j0:
                continue
            px, py = gx[j0:j1, i0:i1], gy[j0:j1, i0:i1]
            A, B, C = a[f], b[f], c[f]
            den = (B[1] - C[1]) * (A[0] - C[0]) + (C[0] - B[0]) * (A[1] - C[1])
            if abs(den) < 1e-12:
                continue
            w0 = ((B[1] - C[1]) * (px - C[0]) + (C[0] - B[0]) * (py - C[1])) / den
            w1 = ((C[1] - A[1]) * (px - C[0]) + (A[0] - C[0]) * (py - C[1])) / den
            w2 = 1.0 - w0 - w1
            dentro = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
            if not dentro.any():
                continue
            z = (w0 * A[2] + w1 * B[2] + w2 * C[2]).astype(np.float32)
            troca = dentro & (z > topo[j0:j1, i0:i1])
            if not troca.any():
                continue
            np.copyto(topo[j0:j1, i0:i1], z, where=troca)
            np.copyto(qual_material[j0:j1, i0:i1], np.int32(indice), where=troca)
            np.copyto(qual_face[j0:j1, i0:i1], np.int64(f), where=troca)

    return Visao(
        nomes=nomes, material=qual_material, face=qual_face,
        xs=xs, ys=ys, topo=topo, passo=passo,
    )


def visivel_por_origem(scene, log: Optional[GeometryLog], passo: float = 0.5,
                       janela=None, ignorar=(), visao: Optional[Visao] = None):
    """Area vista de cima, agrupada por (origem, material). Em m2.

    `visao` reaproveita uma rasterizacao ja feita. Rasterizar e a parte cara -
    a primeira versao deste modulo chamava `top_down` uma vez no relatorio e
    outra nas manchas, dobrando o custo a toa.
    """
    resultado = visao or top_down(scene, passo=passo, janela=janela, ignorar=ignorar)
    if resultado is None:
        return {}, None
    nomes, qual_material, qual_face = resultado.nomes, resultado.material, resultado.face
    passo = resultado.passo

    celula = passo * passo
    saida: dict[tuple[str, str], float] = {}
    for indice, nome in enumerate(nomes):
        mascara = qual_material == indice
        if not mascara.any():
            continue
        if log is None:
            saida[("?", nome)] = saida.get(("?", nome), 0.0) + float(mascara.sum()) * celula
            continue
        faces = qual_face[mascara]
        # Mapeia cada face ao lote que a criou, de uma vez.
        cortes, origens = [], []
        for lote in log.lotes:
            if lote.material == nome:
                cortes.append(lote.primeira_face)
                origens.append(lote.origem)
        if not cortes:
            saida[("?", nome)] = saida.get(("?", nome), 0.0) + float(mascara.sum()) * celula
            continue
        ordem = np.argsort(cortes)
        cortes = np.asarray(cortes)[ordem]
        origens = [origens[i] for i in ordem]
        quais = np.searchsorted(cortes, faces, side="right") - 1
        for k, origem in enumerate(origens):
            n = int((quais == k).sum())
            if n:
                chave = (origem, nome)
                saida[chave] = saida.get(chave, 0.0) + n * celula
    return saida, resultado


def relatorio(scene, log: Optional[GeometryLog], passo: float = 0.5,
              janela=None, ignorar=(), limite: int = 26,
              visao: Optional[Visao] = None) -> str:
    """Texto pronto: o que se ve de cima, de quem, e com que cor."""
    visivel, _ = visivel_por_origem(
        scene, log, passo=passo, janela=janela, ignorar=ignorar, visao=visao
    )
    if not visivel:
        return "nada visivel"

    total = sum(visivel.values())
    linhas = [
        f"visivel de cima: {total:,.0f} m2 em {len(visivel)} pares (origem, material)",
        "",
        f"{'origem':<28}{'material':<20}{'m2':>10}{'%':>7}{'brilho':>8}  rgb",
    ]
    for (origem, material), area in sorted(visivel.items(), key=lambda kv: -kv[1])[:limite]:
        grupo = scene.groups.get(material)
        cor = grupo.material.color if grupo else (0.0, 0.0, 0.0)
        brilho = sum(cor) / 3
        rgb = tuple(int(round(c * 255)) for c in cor)
        marca = "  <== CLARO" if brilho > 0.62 else ""
        linhas.append(
            f"{origem:<28}{material:<20}{area:>10.0f}{100 * area / total:>6.1f}%"
            f"{brilho:>8.2f}  {rgb}{marca}"
        )
    return "\n".join(linhas)


@dataclass
class Mancha:
    """Uma regiao contigua de um mesmo material, vista de cima."""

    material: str
    origem: str
    area: float
    espessura: float  # maior raio que cabe dentro dela, em metros
    x: float
    y: float
    passo: float = 0.5  # resolucao com que ela foi medida

    @property
    def e_chapa(self) -> bool:
        """Faixa ou chapa?

        A distincao que importa ao investigar "quadrados no mapa": uma calcada
        de 2 m de largura e uma chapa de 30 x 30 m podem ter a **mesma area**.
        O que as separa e a espessura - o maior circulo que cabe dentro. Faixa
        tem espessura de metade da largura dela; chapa tem metade do lado.

        O limiar acompanha a resolucao da rasterizacao. Numa regiao grande o
        passo afrouxa (numa cena de 11 km2 ele vai a 2,1 m), e a espessura passa
        a ter essa incerteza; manter 4 m fixos ali faria uma calcada de duas
        celulas passar por chapa. Pedir tres celulas de margem custa perder
        alguma chapa pequena numa regiao grande - e o lado certo de errar, ja
        que a lista existe para investigar, nao para contar.
        """
        return self.espessura >= max(4.0, 3.0 * self.passo)


# Origens que nunca sao "um quadrado no mapa": elas **sao** o mapa. Sem este
# corte, o proprio chao domina a lista - o verde-oliva do estilo tem brilho 0,72
# e o terreno inteiro vira uma mancha de dezenas de milhares de m2.
FUNDO = ("terrain.generate_terrain", "terrain.generate_landuse")


def manchas(scene, log: Optional[GeometryLog], passo: float = 0.5,
            brilho_min: float = 0.62, area_min: float = 40.0,
            janela=None, ignorar=(), ignorar_origens=FUNDO,
            visao: Optional[Visao] = None) -> list[Mancha]:
    """Regioes claras contiguas vistas de cima, com area, espessura e origem."""
    from scipy import ndimage

    resultado = visao or top_down(scene, passo=passo, janela=janela, ignorar=ignorar)
    if resultado is None:
        return []
    nomes, qual_material, qual_face = resultado.nomes, resultado.material, resultado.face
    xs, ys, passo = resultado.xs, resultado.ys, resultado.passo

    saida: list[Mancha] = []
    for indice, nome in enumerate(nomes):
        grupo = scene.groups[nome]
        # Material texturado nao tem "brilho": a cor base so multiplica a imagem.
        if grupo.material.texture is not None:
            continue
        if sum(grupo.material.color) / 3 <= brilho_min:
            continue
        mascara = qual_material == indice
        if not mascara.any():
            continue
        rotulos, quantas = ndimage.label(mascara)
        if quantas == 0:
            continue
        areas = ndimage.sum(mascara, rotulos, range(1, quantas + 1)) * passo * passo
        recortes = ndimage.find_objects(rotulos)
        for comp in np.nonzero(areas > area_min)[0]:
            # A transformada de distancia roda **so na caixa da mancha**, com uma
            # borda de zeros em volta para ela enxergar o lado de fora. Rodar na
            # grade inteira a cada componente era o gargalo que travava o
            # diagnostico: dezenas de manchas x milhoes de celulas cada.
            recorte = recortes[comp]
            if recorte is None:
                continue
            j0, i0 = recorte[0].start, recorte[1].start
            local = np.zeros(
                (recorte[0].stop - j0 + 2, recorte[1].stop - i0 + 2), dtype=bool
            )
            local[1:-1, 1:-1] = rotulos[recorte] == comp + 1
            distancia = ndimage.distance_transform_edt(local) * passo
            lj, li = np.unravel_index(int(np.argmax(distancia)), distancia.shape)
            j, i = j0 + lj - 1, i0 + li - 1
            face = int(qual_face[j, i])
            lote = log.dono(nome, face) if (log and face >= 0) else None
            origem = lote.origem if lote else "?"
            if origem in ignorar_origens:
                continue
            saida.append(
                Mancha(
                    material=nome,
                    origem=origem,
                    area=float(areas[comp]),
                    espessura=float(distancia[lj, li]),
                    x=float(xs[i]),
                    y=float(ys[j]),
                    passo=passo,
                )
            )
    saida.sort(key=lambda m: -m.area)
    return saida


def mapa_anotado(scene, destino, passo: float = 0.5, marcar: list = (),
                 janela=None, ignorar=(), visao: Optional[Visao] = None) -> Path:
    """Desenha a cena de cima com as manchas numeradas.

    Serve para conversar sobre um defeito visual sem ambiguidade: em vez de
    "aquele quadrado ali", vira "a mancha 3", que o relatorio descreve com
    material, origem e tamanho.
    """
    from PIL import Image, ImageDraw

    resultado = visao or top_down(scene, passo=passo, janela=janela, ignorar=ignorar)
    if resultado is None:
        raise ValueError("cena vazia")
    nomes, qual_material = resultado.nomes, resultado.material
    xs, ys, passo = resultado.xs, resultado.ys, resultado.passo

    imagem = np.full((*qual_material.shape, 3), 40, dtype=np.uint8)
    for indice, nome in enumerate(nomes):
        onde = qual_material == indice
        if not onde.any():
            continue
        cor = scene.groups[nome].material.color
        if scene.groups[nome].material.texture is not None:
            imagem[onde] = (96, 96, 88)  # chao texturado: tom neutro
        else:
            imagem[onde] = tuple(int(round(c * 255)) for c in cor)

    # Norte para cima.
    imagem = np.flipud(imagem)
    figura = Image.fromarray(imagem)
    desenho = ImageDraw.Draw(figura)
    altura = figura.height

    for n, mancha in enumerate(marcar, start=1):
        i = int((mancha.x - xs[0]) / passo)
        j = altura - 1 - int((mancha.y - ys[0]) / passo)
        raio = max(int(np.sqrt(mancha.area) / passo / 2) + 6, 10)
        desenho.ellipse([i - raio, j - raio, i + raio, j + raio], outline=(255, 0, 0), width=3)
        desenho.text((i + raio + 3, j - 7), str(n), fill=(255, 0, 0))

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    figura.save(destino)
    return destino


# ------------------------------------------------------------- sessao ao vivo


_SESSAO: Optional[logging.Handler] = None


def iniciar_sessao(destino=None, nivel: int = logging.DEBUG) -> Path:
    """Comeca a gravar tudo que os geradores dizem, enquanto rodam.

    Escreve com `flush` a cada linha de proposito: o arquivo serve para ser lido
    **durante** a execucao, e um buffer de 8 KB esconderia justamente a etapa em
    que a geracao travou ou produziu o defeito.
    """
    global _SESSAO

    from . import config

    if destino is None:
        pasta = config.OUTPUT_DIR / "logs"
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / f"sessao_{datetime.now():%Y%m%d-%H%M%S}.log"
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    encerrar_sessao()

    class _NaHora(logging.FileHandler):
        def emit(self, registro):
            super().emit(registro)
            self.flush()

    manipulador = _NaHora(destino, mode="a", encoding="utf-8")
    manipulador.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-34s %(message)s",
                          datefmt="%H:%M:%S")
    )
    manipulador.setLevel(nivel)

    raiz = logging.getLogger("mapforge")
    raiz.setLevel(min(raiz.level or nivel, nivel))
    raiz.addHandler(manipulador)
    _SESSAO = manipulador

    raiz.info("=== sessao de diagnostico aberta em %s ===", destino)
    return destino


def encerrar_sessao() -> None:
    global _SESSAO

    if _SESSAO is not None:
        logging.getLogger("mapforge").removeHandler(_SESSAO)
        _SESSAO.close()
        _SESSAO = None


def registrar_geracao(destino, bbox, settings, result, passo: float = 0.6) -> None:
    """Anexa ao log o retrato de uma geracao: configuracao e procedencia.

    E o que fecha o ciclo: o log corrido conta *o que aconteceu*, e este bloco
    conta *o que sobrou na tela e de quem e*.
    """
    registrador = logging.getLogger("mapforge.diagnostico")
    scene = result.scene
    log_geo = getattr(result, "geometry_log", None)

    linhas = [
        "",
        "=" * 78,
        f"GERACAO  {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"  regiao   : {bbox.key()}  ({bbox.width_m:.0f} x {bbox.height_m:.0f} m, "
        f"{bbox.area_km2:.2f} km2)",
        f"  malha    : {scene.triangle_count:,} triangulos, {len(scene.groups)} materiais",
    ]
    ligadas = [
        nome for nome, valor in sorted(settings.to_dict().items())
        if valor is True
    ]
    linhas.append(f"  ligadas  : {', '.join(ligadas) or 'nenhuma'}")
    linhas.append(f"  detalhe  : {settings.detail} | estilo {settings.style} "
                  f"| seed {settings.seed}")

    if log_geo is None:
        linhas.append("  (procedencia desligada: marque o diagnostico antes de gerar)")
    else:
        linhas.append(f"  procedencia: {log_geo.summary()}")
        linhas.append("")
        # Uma rasterizacao so, compartilhada: e a parte cara.
        visao = top_down(scene, passo=passo)
        linhas.append(f"  (rasterizado a {visao.passo:.2f} m por celula)")
        linhas.append("")
        linhas.append(relatorio(scene, log_geo, limite=20, visao=visao))
        achadas = manchas(scene, log_geo, area_min=40.0, visao=visao)
        chapas = [m for m in achadas if m.e_chapa]
        linhas.append("")
        linhas.append(f"  chapas claras (espessura >= 4 m): {len(chapas)}")
        for n, m in enumerate(chapas[:20], start=1):
            linhas.append(
                f"   {n:>3}  {m.origem:<28}{m.material:<16}{m.area:>8.0f} m2"
                f"{m.espessura:>7.1f} m  em {m.x:.0f},{m.y:.0f}"
            )
    linhas.append("=" * 78)

    texto = "\n".join(linhas)
    registrador.info(texto)
    if destino is not None:
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
        with open(destino, "a", encoding="utf-8") as arquivo:
            arquivo.write(texto + "\n")


def o_que_ha_em(scene, log: Optional[GeometryLog], x: float, y: float,
                raio: float = 4.0) -> str:
    """Pilha completa de geometria sobre um ponto, do topo para baixo."""
    import shapely

    alvo = shapely.Point(x, y).buffer(raio)
    achados = []
    for nome, grupo in scene.groups.items():
        if len(grupo.faces) == 0:
            continue
        tri = grupo.vertices[grupo.faces]
        centro = tri.mean(axis=1)
        perto = (
            (np.abs(centro[:, 0] - x) < raio * 2) & (np.abs(centro[:, 1] - y) < raio * 2)
        )
        if not perto.any():
            continue
        dentro = shapely.contains_xy(alvo, centro[perto, 0], centro[perto, 1])
        if not dentro.any():
            continue
        indices = np.nonzero(perto)[0][dentro]
        for f in indices:
            lote = log.dono(nome, int(f)) if log else None
            achados.append(
                (float(centro[f, 2]), nome, lote.origem if lote else "?")
            )

    if not achados:
        return f"nada em {x:.0f},{y:.0f}"
    achados.sort(reverse=True)
    linhas = [f"em {x:.0f},{y:.0f} (raio {raio:.0f} m), de cima para baixo:"]
    visto = set()
    for z, material, origem in achados:
        chave = (material, origem, round(z, 1))
        if chave in visto:
            continue
        visto.add(chave)
        linhas.append(f"  z={z:8.2f}  {material:<20} <- {origem}")
        if len(linhas) > 24:
            linhas.append("  ...")
            break
    return "\n".join(linhas)
