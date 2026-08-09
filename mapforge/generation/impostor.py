"""Impostor de arvore: a copa vira imagem, a arvore vira dois triangulos.

Uma arvore de mata custa hoje 33 triangulos. Multiplicado por milhares, a
vegetacao vira metade da cena. A alternativa e trocar a geometria por uma
**imagem da propria arvore** num quad - 2 triangulos, 16 vezes menos.

Por que **de angulo fixo**, e nao o impostor octaedrico que as engines usam: o
octaedrico escolhe, a cada quadro, qual vista do atlas mostrar conforme o angulo
da camera. Isso exige shader, e glTF nao expressa shader. Um GLB estatico nao
consegue. Ja o quad com recorte por alfa e `alphaMode: MASK` - que e **nucleo do
glTF 2.0, nao extensao** - abre em qualquer visualizador. Foi o criterio: um
modelo, importa em qualquer lugar, funciona.

O atlas e assado a partir dos **proprios prototipos low-poly**, com o mesmo
renderizador e o mesmo sombreamento chapado da cena. O estilo bate por
construcao: nao e foto de arvore, e a nossa arvore vista de cima.

Limite honesto: quad horizontal deita conforme a camera baixa. O impostor e
LOD de longe, nao substituto universal - e nao serve para impressao 3D nem para
colisao por malha, onde alfa nao significa nada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Lado de cada quadro do atlas, em pixels. 128 sustenta a copa a media
# distancia; acima disso o ganho para no limite da propria silhueta low-poly.
TILE_PX = 128

# Abaixo deste alfa o pixel some. Alto de proposito: borda semitransparente em
# vegetacao produz franja clara quando muitas arvores se sobrepoem.
ALPHA_CUTOFF = 0.55

# Inclinacao da camera ao assar. 90 graus e de cima em cheio; um pouco menos
# mostra alguma silhueta lateral e disfarca o quad deitado em vista obliqua.
BAKE_ELEVATION = 68.0


@dataclass
class ImpostorAtlas:
    """Atlas assado e o retangulo UV de cada (especie, variante)."""

    image: object  # PIL.Image RGBA
    tiles: dict[tuple[str, int], tuple[float, float, float, float]] = field(
        default_factory=dict
    )
    # Proporcao largura/altura do desenho dentro do quadro, por peca. O quad usa
    # isso para nao esticar uma palmeira estreita na largura de uma mangueira.
    aspect: dict[tuple[str, int], float] = field(default_factory=dict)

    def uv_for(self, species: str, variant: int) -> Optional[tuple]:
        return self.tiles.get((species, variant))


def _render_tile(vertices, faces, color, size: int) -> np.ndarray:
    """Rasteriza um prototipo de cima, devolve RGBA com fundo transparente.

    Reaproveita o renderizador de software do projeto: e ele que garante que o
    impostor tenha exatamente o mesmo sombreamento chapado do resto da cena.
    """
    from ..core.mesh import Material, MeshGroup, Scene
    from ..render.software import Camera, render_scene

    grupo = MeshGroup(
        material=Material(name="impostor", color=color),
        vertices=np.asarray(vertices, dtype=np.float64),
        faces=np.asarray(faces, dtype=np.int64),
    )
    cena = Scene(name="impostor", groups={"impostor": grupo})

    camera = Camera(azimuth=225.0, elevation=BAKE_ELEVATION, perspective=False)

    def desenhar(fundo):
        img = render_scene(
            cena, path=None, width=size, height=size, camera=camera,
            supersample=2, background=fundo, horizon=fundo,
        )
        return np.asarray(img, dtype=np.float64) / 255.0

    # Extracao exata de alfa por dois fundos.
    #
    # Tentar deduzir o alfa da distancia a uma cor-chave nao funciona: a folha
    # de palmeira ocupa uma fracao de pixel, mas a distancia dela ao magenta ja
    # satura o limiar, entao o pixel recebia alfa 1 com magenta misturado dentro
    # - e a interpolacao da GPU devolvia isso como franja rosa.
    #
    # Renderizando o mesmo prototipo sobre preto e sobre branco:
    #     Ip = arvore*a          Ib = arvore*a + (1-a)
    # logo `a = 1 - (Ib - Ip)` e `arvore = Ip / a`. Sem limiar, sem chute.
    sobre_preto = desenhar((0.0, 0.0, 0.0))
    sobre_branco = desenhar((1.0, 1.0, 1.0))

    alfa = np.clip(1.0 - (sobre_branco - sobre_preto).mean(axis=2), 0.0, 1.0)
    seguro = np.maximum(alfa, 1e-3)[:, :, None]
    cor = np.clip(sobre_preto / seguro, 0.0, 1.0)

    rgba = np.zeros((*alfa.shape, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(cor * 255.0, 0, 255).astype(np.uint8)
    rgba[:, :, 3] = (alfa * 255).astype(np.uint8)
    return _defringe(rgba)


def _defringe(rgba: np.ndarray) -> np.ndarray:
    """Espalha a cor opaca para dentro da area transparente.

    O pixel de borda e uma mistura entre a arvore e o fundo croma, entao guarda
    um resto de magenta. Ele nao aparece enquanto o alfa e zero - mas a
    interpolacao bilinear da GPU mistura os vizinhos *antes* do recorte, e o
    magenta ressurge como franja rosa no contorno de cada folha. Foi visivel nas
    palmeiras, que sao finas.

    A correcao e classica: onde nao e opaco, copiar a cor do opaco mais proximo.
    O alfa nao muda - so a cor que estava escondida debaixo dele.
    """
    from scipy import ndimage

    opaco = rgba[:, :, 3] > 200
    if not opaco.any() or opaco.all():
        return rgba
    _, (linhas, colunas) = ndimage.distance_transform_edt(~opaco, return_indices=True)
    saida = rgba.copy()
    for canal in range(3):
        plano = saida[:, :, canal]
        plano[~opaco] = plano[linhas[~opaco], colunas[~opaco]]
    return saida


def _trim(rgba: np.ndarray) -> tuple[np.ndarray, float]:
    """Corta a moldura vazia. Devolve (imagem, proporcao largura/altura)."""
    visivel = rgba[:, :, 3] > 8
    if not visivel.any():
        return rgba, 1.0
    linhas = np.nonzero(visivel.any(axis=1))[0]
    colunas = np.nonzero(visivel.any(axis=0))[0]
    recorte = rgba[linhas[0] : linhas[-1] + 1, colunas[0] : colunas[-1] + 1]
    altura, largura = recorte.shape[:2]
    return recorte, largura / max(altura, 1)


def bake_atlas(prototypes, palette, tile_px: int = TILE_PX) -> ImpostorAtlas:
    """Assa o atlas com todas as especies e variantes de copa.

    A copa e o tronco entram juntos no mesmo desenho: no impostor nao ha custo
    em manter o tronco, porque o quadro ja esta pago.
    """
    from PIL import Image

    from .vegetation import SPECIES_HEIGHT

    pecas: list[tuple[str, int, np.ndarray, float]] = []
    n_cores = max(palette.canopy_count(), 1)

    for indice, species in enumerate(SPECIES_HEIGHT):
        tronco = prototypes.trunk(species)
        for variante, copa in enumerate(prototypes.variants(species)):
            verts = np.concatenate([tronco[0], copa[0]])
            faces = np.concatenate([tronco[1], copa[1] + len(tronco[0])])
            cor = palette.canopy((indice + variante) % n_cores).color
            rgba, proporcao = _trim(_render_tile(verts, faces, cor, tile_px))
            pecas.append((species, variante, rgba, proporcao))

    if not pecas:
        raise ValueError("nenhum prototipo para assar")

    colunas = int(np.ceil(np.sqrt(len(pecas))))
    linhas = int(np.ceil(len(pecas) / colunas))
    atlas = Image.new("RGBA", (colunas * tile_px, linhas * tile_px), (0, 0, 0, 0))

    resultado = ImpostorAtlas(image=atlas)
    for n, (species, variante, rgba, proporcao) in enumerate(pecas):
        col, lin = n % colunas, n // colunas
        peca = Image.fromarray(rgba, mode="RGBA")
        # Encaixa mantendo a proporcao, centralizado no quadro.
        if proporcao >= 1.0:
            larg, alt = tile_px, max(int(round(tile_px / proporcao)), 1)
        else:
            larg, alt = max(int(round(tile_px * proporcao)), 1), tile_px
        peca = peca.resize((larg, alt), Image.LANCZOS)
        atlas.paste(
            peca,
            (col * tile_px + (tile_px - larg) // 2, lin * tile_px + (tile_px - alt) // 2),
        )

        # UV com meio pixel de recuo: sem isso a interpolacao bilinear puxa o
        # quadro vizinho e aparece uma franja de outra arvore na borda.
        recuo = 0.5 / tile_px
        u0 = (col + recuo) / colunas
        v0 = (lin + recuo) / linhas
        u1 = (col + 1 - recuo) / colunas
        v1 = (lin + 1 - recuo) / linhas
        resultado.tiles[(species, variante)] = (u0, v0, u1, v1)
        resultado.aspect[(species, variante)] = proporcao

    log.info(
        "Atlas de impostor: %d pecas em %dx%d px",
        len(pecas), atlas.width, atlas.height,
    )
    return resultado


def add_impostors(
    builder,
    material,
    atlas: ImpostorAtlas,
    species: str,
    variant: int,
    rows: np.ndarray,
) -> int:
    """Emite um quad por arvore. Devolve os triangulos gerados.

    O quad e **horizontal**, na altura da copa: e a orientacao correta para uma
    camera de cima, que e como este mapa e olhado. Ele deita conforme a camera
    baixa - por isso o impostor e opcao, nao padrao.
    """
    uv = atlas.uv_for(species, variant)
    if uv is None or len(rows) == 0:
        return 0
    u0, v0, u1, v1 = uv

    x, y, z = rows[:, 0], rows[:, 1], rows[:, 2]
    altura = rows[:, 3]
    giro = rows[:, 4]

    # Lado do quad: a copa ocupa cerca de 60% da altura da arvore em largura.
    meio = altura * 0.30
    cos, sin = np.cos(giro), np.sin(giro)

    # Quatro cantos girados no plano, na altura do centro da copa.
    cantos = np.array([(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)])
    n = len(rows)
    verts = np.empty((n, 4, 3))
    for i, (dx, dy) in enumerate(cantos):
        verts[:, i, 0] = x + (dx * cos - dy * sin) * meio
        verts[:, i, 1] = y + (dx * sin + dy * cos) * meio
        verts[:, i, 2] = z + altura * 0.62

    uvs = np.tile(
        np.array([[u0, v1], [u1, v1], [u1, v0], [u0, v0]]), (n, 1)
    )

    base = (np.arange(n) * 4)[:, None]
    faces = np.concatenate(
        [
            np.column_stack([base[:, 0], base[:, 0] + 1, base[:, 0] + 2]),
            np.column_stack([base[:, 0], base[:, 0] + 2, base[:, 0] + 3]),
        ]
    )
    builder.add_mesh(material, verts.reshape(-1, 3), faces, uvs)
    return len(faces)
