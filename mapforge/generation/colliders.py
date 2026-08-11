"""Malha de fisica: formas simples para a cena virar mundo jogavel.

A malha bonita nao serve como colisor. Um predio tem parede, telhado de quatro
aguas, beiral, janela e platibanda - centenas de triangulos que a fisica teria
de testar a cada quadro para responder uma pergunta simples: "bati na casa?".
Uma engine responde isso com **uma caixa**.

Entao a fisica sai em paralelo, com as formas que ela realmente quer:

- **caixa** por edificio, na orientacao do proprio predio;
- **cilindro** por arvore, no tronco - copa nao colide, e ninguem esbarra nela;
- **campo de altura** para o chao, que e o formato nativo de terreno em toda
  engine e o mais barato que existe;
- **volume** por corpo d'agua, para servir de gatilho (nadar, afogar, frear).

Duas saidas, porque as engines nao concordam:

1. **Malha no proprio GLB**, em nos separados com sufixo configuravel. O Godot
   entende `-col` (colisor trimesh) e `-colonly` (colisor sem desenho); o
   Unreal usa `UCX_`. Serve para importar e sair jogando.
2. **JSON ao lado**, com as formas *analiticas* - centro, tamanho, raio,
   rotacao. E o que permite criar colisor **primitivo** na engine em vez de
   trimesh, que e onde esta a diferenca de desempenho de verdade: uma caixa
   custa quase nada, uma malha de mil triangulos custa mil vezes mais.

Nada disso e gerado por padrao: o mapa continua sendo so o visual ate alguem
pedir a fisica.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Sufixos que cada engine reconhece no nome do no.
NAMING = {
    "godot": "-colonly",
    "unreal": "UCX_",
    "plain": "",
}

# Prefixo comum a todo no de colisao. Existe para o resto do programa poder
# reconhece-los sem saber qual convencao de engine foi escolhida - e assim o
# viewport e o renderizador conseguem **nao desenhar** a fisica.
COLLIDER_PREFIX = "colisor_"


def is_collider(material_name: str) -> bool:
    """O material pertence a malha de fisica?"""
    return COLLIDER_PREFIX in (material_name or "")

# Passo do campo de altura do chao, em metros. Bem mais grosso que a malha
# visual: a fisica nao precisa do detalhe que o olho quer, e cada celula a mais
# custa em toda consulta de colisao.
GROUND_STEP_M = 12.0

# Abaixo desta area o edificio nao ganha colisor proprio - galpao de fundo de
# quintal e telheiro nao mudam a navegacao e so somariam formas.
MIN_BUILDING_M2 = 8.0


@dataclass
class BoxShape:
    """Caixa orientada: centro, tamanho e giro em torno do eixo vertical."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    yaw: float

    def to_dict(self) -> dict:
        return {
            "tipo": "caixa",
            "centro": [round(c, 3) for c in self.center],
            "tamanho": [round(s, 3) for s in self.size],
            "giro": round(self.yaw, 5),
        }


@dataclass
class CylinderShape:
    """Cilindro em pe: base no centro informado."""

    center: tuple[float, float, float]
    radius: float
    height: float

    def to_dict(self) -> dict:
        return {
            "tipo": "cilindro",
            "centro": [round(c, 3) for c in self.center],
            "raio": round(self.radius, 3),
            "altura": round(self.height, 3),
        }


@dataclass
class HeightField:
    """Chao como grade de alturas - o formato nativo de terreno nas engines."""

    origin: tuple[float, float]
    step: float
    columns: int
    rows: int
    heights: np.ndarray  # (rows, columns), em metros

    def to_dict(self) -> dict:
        return {
            "tipo": "campo_de_altura",
            "origem": [round(c, 3) for c in self.origin],
            "passo": round(self.step, 3),
            "colunas": self.columns,
            "linhas": self.rows,
            # Uma linha por vetor, para o arquivo continuar legivel.
            "alturas": [[round(float(v), 2) for v in linha] for linha in self.heights],
        }


@dataclass
class ColliderSet:
    boxes: list[BoxShape] = field(default_factory=list)
    cylinders: list[CylinderShape] = field(default_factory=list)
    ground: Optional[HeightField] = None
    water: list[BoxShape] = field(default_factory=list)

    def summary(self) -> str:
        partes = [f"{len(self.boxes)} caixas", f"{len(self.cylinders)} cilindros"]
        if self.ground is not None:
            partes.append(f"chao {self.ground.columns}x{self.ground.rows}")
        if self.water:
            partes.append(f"{len(self.water)} volumes de agua")
        return ", ".join(partes)

    def to_dict(self) -> dict:
        return {
            "formato": "mapforge-colisores/1",
            "resumo": self.summary(),
            "edificios": [b.to_dict() for b in self.boxes],
            "vegetacao": [c.to_dict() for c in self.cylinders],
            "chao": self.ground.to_dict() if self.ground else None,
            "agua": [b.to_dict() for b in self.water],
        }


def _oriented_box(poly, base_z: float, top_z: float) -> Optional[BoxShape]:
    """Caixa alinhada ao proprio predio, e nao aos eixos do mundo.

    Casa raramente e paralela ao norte. Uma caixa alinhada ao eixo sobre um
    predio a 30 graus fica larga demais e o jogador esbarra no nada; a caixa
    orientada acompanha a fachada.
    """
    try:
        obb = poly.minimum_rotated_rectangle
        cantos = np.asarray(obb.exterior.coords, dtype=np.float64)[:-1]
    except Exception:  # noqa: BLE001 - contorno degenerado
        return None
    if len(cantos) != 4:
        return None

    lado_a = cantos[1] - cantos[0]
    lado_b = cantos[2] - cantos[1]
    comp_a = float(np.linalg.norm(lado_a))
    comp_b = float(np.linalg.norm(lado_b))
    if comp_a < 1e-6 or comp_b < 1e-6:
        return None

    # O giro sai do lado mais longo, para a caixa "olhar" no sentido da fachada.
    principal = lado_a if comp_a >= comp_b else lado_b
    yaw = math.atan2(float(principal[1]), float(principal[0]))

    centro = cantos.mean(axis=0)
    altura = max(top_z - base_z, 0.5)
    return BoxShape(
        center=(float(centro[0]), float(centro[1]), base_z + altura / 2.0),
        size=(max(comp_a, comp_b), min(comp_a, comp_b), altura),
        yaw=yaw,
    )


def build_colliders(ctx, map_data, water_union=None) -> ColliderSet:
    """Monta o conjunto de formas a partir do que a cena de fato gerou."""
    conjunto = ColliderSet()

    # --- edificios: uma caixa cada, com a altura que o gerador usou ---
    for poly, base_z, top_z in getattr(ctx, "collider_boxes", []):
        if poly is None or poly.is_empty or poly.area < MIN_BUILDING_M2:
            continue
        caixa = _oriented_box(poly, base_z, top_z)
        if caixa is not None:
            conjunto.boxes.append(caixa)

    # --- vegetacao: cilindro no tronco ---
    for x, y, z, altura, raio in getattr(ctx, "collider_cylinders", []):
        if altura <= 0.2 or raio <= 0.02:
            continue
        conjunto.cylinders.append(
            CylinderShape(center=(float(x), float(y), float(z)), radius=float(raio),
                          height=float(altura))
        )

    # --- chao ---
    conjunto.ground = _ground_field(ctx)

    # --- agua: uma caixa rasa por corpo, para servir de gatilho ---
    if water_union is not None and not water_union.is_empty:
        for corpo in getattr(water_union, "geoms", [water_union]):
            if corpo.is_empty or corpo.area < 25.0:
                continue
            minx, miny, maxx, maxy = corpo.bounds
            nivel = ctx.terrain.level_over(corpo) if ctx.draped else 0.0
            conjunto.water.append(
                BoxShape(
                    center=((minx + maxx) / 2.0, (miny + maxy) / 2.0, nivel - 1.0),
                    size=(maxx - minx, maxy - miny, 2.0),
                    yaw=0.0,
                )
            )

    log.info("Colisores: %s", conjunto.summary())
    return conjunto


def _ground_field(ctx) -> HeightField:
    """Chao em grade grossa, seguindo o relevo quando ele existe."""
    half_w, half_h = ctx.half_size
    passo = GROUND_STEP_M
    colunas = max(int(round(2 * half_w / passo)) + 1, 2)
    linhas = max(int(round(2 * half_h / passo)) + 1, 2)

    xs = np.linspace(-half_w, half_w, colunas)
    ys = np.linspace(-half_h, half_h, linhas)
    grade_x, grade_y = np.meshgrid(xs, ys)

    if ctx.draped:
        alturas = ctx.terrain.height(grade_x.ravel(), grade_y.ravel()).reshape(grade_y.shape)
    else:
        alturas = np.zeros(grade_y.shape)

    return HeightField(
        origin=(float(-half_w), float(-half_h)),
        step=float(passo),
        columns=colunas,
        rows=linhas,
        heights=np.asarray(alturas, dtype=np.float64),
    )


# ------------------------------------------------------------------- geometria


def _box_mesh(shape: BoxShape):
    """Oito vertices e doze triangulos, girados no plano."""
    sx, sy, sz = (s / 2.0 for s in shape.size)
    cantos = np.array(
        [
            [-sx, -sy, -sz], [sx, -sy, -sz], [sx, sy, -sz], [-sx, sy, -sz],
            [-sx, -sy, sz], [sx, -sy, sz], [sx, sy, sz], [-sx, sy, sz],
        ]
    )
    cos, sin = math.cos(shape.yaw), math.sin(shape.yaw)
    girado = np.column_stack(
        [
            cantos[:, 0] * cos - cantos[:, 1] * sin,
            cantos[:, 0] * sin + cantos[:, 1] * cos,
            cantos[:, 2],
        ]
    )
    faces = np.array(
        [
            [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
            [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
        ],
        dtype=np.int64,
    )
    return girado + np.array(shape.center), faces


def _cylinder_mesh(shape: CylinderShape, lados: int = 6):
    """Prisma de seis lados: mais que isso a fisica nao aproveita."""
    angulos = np.linspace(0.0, 2 * np.pi, lados, endpoint=False)
    anel = np.column_stack([np.cos(angulos), np.sin(angulos)]) * shape.radius
    baixo = np.column_stack([anel, np.zeros(lados)])
    alto = np.column_stack([anel, np.full(lados, shape.height)])
    verts = np.concatenate([baixo, alto]) + np.array(shape.center)

    i = np.arange(lados)
    j = (i + 1) % lados
    lado = np.concatenate(
        [np.column_stack([i, j, j + lados]), np.column_stack([i, j + lados, i + lados])]
    )
    tampa = np.column_stack(
        [np.full(lados - 2, lados), np.arange(1, lados - 1) + lados, np.arange(2, lados) + lados]
    )
    return verts, np.concatenate([lado, tampa]).astype(np.int64)


def _ground_mesh(campo: HeightField):
    xs = campo.origin[0] + np.arange(campo.columns) * campo.step
    ys = campo.origin[1] + np.arange(campo.rows) * campo.step
    grade_x, grade_y = np.meshgrid(xs, ys)
    verts = np.column_stack([grade_x.ravel(), grade_y.ravel(), campo.heights.ravel()])

    i, j = np.meshgrid(np.arange(campo.columns - 1), np.arange(campo.rows - 1))
    i, j = i.ravel(), j.ravel()
    v00 = j * campo.columns + i
    v10, v01 = v00 + 1, v00 + campo.columns
    v11 = v01 + 1
    faces = np.concatenate(
        [np.column_stack([v00, v10, v11]), np.column_stack([v00, v11, v01])]
    ).astype(np.int64)
    return verts, faces


def add_collider_meshes(builder, conjunto: ColliderSet, naming: str = "godot") -> int:
    """Acrescenta a geometria de colisao a cena, em nos proprios.

    O sufixo do nome e o que faz a engine reconhecer o no como colisor na
    importacao. glTF nao tem fisica no padrao, entao convencao de nome e o unico
    canal que existe - e cada engine escolheu a sua.
    """
    from ..core.mesh import Material

    sufixo = NAMING.get(naming, NAMING["godot"])

    def material_de(nome: str) -> Material:
        # Totalmente transparente.
        #
        # A primeira versao usava vermelho semitransparente "para identificar" -
        # e o resultado foi a fisica aparecer por cima do mapa inteiro, que e
        # exatamente o que um colisor nao deve fazer. Quem quer ve-la abre a
        # camada na engine; no modelo ela e invisivel, e o viewport e o
        # renderizador a ignoram pelo nome.
        return Material(
            name=(f"UCX_{nome}" if sufixo == "UCX_" else f"{nome}{sufixo}"),
            color=(0.85, 0.25, 0.35),
            opacity=0.0,
        )

    total = 0
    for nome, formas, malha in (
        ("colisor_predios", conjunto.boxes, _box_mesh),
        ("colisor_vegetacao", conjunto.cylinders, _cylinder_mesh),
        ("colisor_agua", conjunto.water, _box_mesh),
    ):
        if not formas:
            continue
        material = material_de(nome)
        for forma in formas:
            verts, faces = malha(forma)
            builder.add_mesh(material, verts, faces)
            total += len(faces)

    if conjunto.ground is not None:
        verts, faces = _ground_mesh(conjunto.ground)
        builder.add_mesh(material_de("colisor_chao"), verts, faces)
        total += len(faces)

    log.info("Colisores: %d triangulos de fisica", total)
    return total
