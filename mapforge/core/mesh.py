"""Construcao de malhas.

Sistema de coordenadas interno: X = leste, Y = norte, Z = altura (Z-up, metros).
A conversao para a convencao do glTF (Y-up) acontece somente no exportador.

O MeshBuilder acumula triangulos agrupados por material; cada grupo vira uma
geometria separada na cena exportada.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

try:  # pragma: no cover - dependencia opcional em tempo de import
    from trimesh.creation import triangulate_polygon as _tri_poly
except Exception:  # pragma: no cover
    _tri_poly = None


@dataclass(frozen=True)
class Material:
    """Material PBR simplificado, suficiente para o estilo low-poly/cartoon."""

    name: str
    color: tuple[float, float, float]
    roughness: float = 0.9
    metallic: float = 0.0
    opacity: float = 1.0
    emissive: tuple[float, float, float] | None = None
    # Imagem PIL opcional (textura de cor base). Fora de __eq__/__hash__ porque
    # imagem nao e hashavel e a identidade do material e o nome.
    texture: object | None = field(default=None, compare=False)

    def rgba8(self) -> tuple[int, int, int, int]:
        r, g, b = (int(round(max(0.0, min(1.0, c)) * 255)) for c in self.color)
        return (r, g, b, int(round(max(0.0, min(1.0, self.opacity)) * 255)))


@dataclass
class MeshGroup:
    """Malha final de um material."""

    material: Material
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray | None = None


@dataclass
class _Group:
    material: Material
    vertices: list[np.ndarray] = field(default_factory=list)
    faces: list[np.ndarray] = field(default_factory=list)
    uvs: list[np.ndarray] = field(default_factory=list)
    offset: int = 0
    has_uv: bool = False

    def append(self, verts: np.ndarray, faces: np.ndarray, uv: np.ndarray | None = None) -> None:
        if len(verts) == 0 or len(faces) == 0:
            return
        self.vertices.append(verts)
        self.faces.append(faces + self.offset)
        if uv is not None:
            self.has_uv = True
        # Guarda um UV por vertice sempre: se um lote vier sem, entra zerado,
        # senao os indices desalinham quando o material mistura os dois casos.
        self.uvs.append(uv if uv is not None else np.zeros((len(verts), 2), dtype=np.float64))
        self.offset += len(verts)


def _as_polygons(geom) -> list[Polygon]:
    """Normaliza qualquer geometria em uma lista de poligonos validos e nao vazios."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        polys = [geom]
    elif isinstance(geom, MultiPolygon):
        polys = list(geom.geoms)
    elif hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if isinstance(g, Polygon)]
    else:
        return []

    out: list[Polygon] = []
    for poly in polys:
        if poly.is_empty:
            continue
        # O reparo vem antes do filtro de area: um poligono auto-intersectante
        # pode informar area 0 e seria descartado sem chance de ser consertado.
        if not poly.is_valid:
            out.extend(_as_polygons(poly.buffer(0)))
            continue
        if poly.area <= 1e-9:
            continue
        out.append(orient(poly, sign=1.0))
    return out


def _triangulate(poly: Polygon) -> tuple[np.ndarray, np.ndarray]:
    """Triangula um poligono 2D (com buracos). Retorna (vertices Nx2, faces Mx3 CCW)."""
    if _tri_poly is None:  # pragma: no cover
        raise RuntimeError("trimesh nao esta instalado: nao e possivel triangular poligonos")
    try:
        verts, faces = _tri_poly(poly, engine="earcut")
    except Exception:
        verts, faces = _tri_poly(poly)
    verts = np.asarray(verts, dtype=np.float64).reshape(-1, 2)
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) == 0:
        return verts, faces
    # Forca winding anti-horario para que a normal aponte para +Z.
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    signed = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    flip = signed < 0
    if flip.any():
        faces[flip] = faces[flip][:, [0, 2, 1]]
    return verts, faces


class MeshBuilder:
    """Acumula geometria por material.

    `terrain` e um campo de altura opcional com `.height(x, y)` vetorizado.
    Quando presente, as superficies marcadas com `drape=True` acompanham o
    relevo em vez de ficarem no plano z=0.
    """

    def __init__(self, terrain=None, clip=None) -> None:
        self._groups: dict[str, _Group] = {}
        self.terrain = terrain
        # Contorno da regiao, quando ela foi desenhada a mao. As superficies sao
        # recortadas por ele na hora de virar malha: o parser recorta o *eixo*
        # da via, mas a largura e aplicada depois, entao sem isto a calcada
        # avanca alguns metros no vazio.
        self.clip = clip
        # Aresta maxima ao assentar no relevo. Precisa ser bem menor que o passo
        # da grade do terreno: uma aresta longa vira uma corda reta por cima de
        # um terreno curvo, e a rua atravessa o morro.
        step = getattr(terrain, "step", None)
        self.drape_edge = max(float(step) / 3.0, 0.5) if step else 0.0
        # Folga sobre o terreno quando ha relevo. Com o recorte por triangulo o
        # erro geometrico e zero, entao isto e so margem de seguranca visual.
        self.drape_bias = 0.08 if terrain is not None else 0.0
        # Triangulos do terreno e o indice espacial deles, construidos uma vez
        # so: sao milhares, e o recorte acontece a cada camada assentada.
        self._cells = None
        self._cell_tree = None

    def _drape(self, points_xy: np.ndarray) -> np.ndarray:
        """Altura do terreno nos pontos dados (zero quando nao ha relevo)."""
        if self.terrain is None:
            return np.zeros(len(points_xy))
        return np.asarray(self.terrain.height(points_xy[:, 0], points_xy[:, 1]), dtype=np.float64)

    def clipped(self, geom):
        """Recorta pelo contorno da regiao, quando ha um desenhado."""
        if self.clip is None or geom is None or geom.is_empty:
            return geom
        try:
            cut = geom.intersection(self.clip)
        except Exception:  # noqa: BLE001 - topologia ruim: deixa passar
            return geom
        return cut

    def densify(self, geom):
        """Insere vertices ao longo das bordas para acompanhar o relevo."""
        if self.terrain is None or self.drape_edge <= 0 or geom is None or geom.is_empty:
            return geom
        try:
            from shapely import segmentize

            return segmentize(geom, self.drape_edge)
        except Exception:  # noqa: BLE001 - segue com a geometria original
            return geom

    def grid_split(self, geom) -> list:
        """Recorta a geometria pelas celulas da grade do terreno.

        Densificar a borda nao basta: o earcut liga vertices distantes, e num
        corredor de rua isso produz triangulos com arestas de centenas de
        metros que passam reto por cima do morro (medido: a rua afundava 6,5 m,
        com arestas de ate 410 m).

        O recorte usa a mesma grade da malha do terreno, entao cada peca cai
        dentro de uma celula, onde o relevo e praticamente plano - e a
        superficie assentada passa a acompanhar o terreno de perto.
        """
        if self.terrain is None or geom is None or geom.is_empty:
            return [geom] if geom is not None else []

        import shapely
        from shapely.ops import polygonize, unary_union

        xs = getattr(self.terrain, "xs", None)
        ys = getattr(self.terrain, "ys", None)
        if xs is None or ys is None or len(xs) < 2 or len(ys) < 2:
            return [geom]

        minx, miny, maxx, maxy = geom.bounds
        # So as linhas de grade que cruzam a geometria.
        cut_x = xs[(xs > minx) & (xs < maxx)]
        cut_y = ys[(ys > miny) & (ys < maxy)]
        if len(cut_x) + len(cut_y) == 0:
            return [geom]
        if len(cut_x) * len(cut_y) > 250_000:  # protecao contra area absurda
            return [geom]

        from shapely.geometry import LineString

        grade = [LineString([(x, miny), (x, maxy)]) for x in cut_x]
        grade += [LineString([(minx, y), (maxx, y)]) for y in cut_y]

        # Sobreposicao planar de uma vez so: noda o contorno da geometria com as
        # linhas de grade e remonta as faces. A versao anterior intersectava
        # cada celula contra a geometria inteira - com a uniao das vias de uma
        # cidade isso custava minutos, porque cada uma das milhares de operacoes
        # percorria a geometria toda.
        try:
            noded = unary_union([geom.boundary, *grade])
            faces = list(polygonize(noded))
        except Exception:  # noqa: BLE001 - topologia ruim: segue sem recortar
            return [geom]
        if not faces:
            return [geom]

        # `polygonize` devolve tambem as faces de fora; o ponto interno decide.
        shapely.prepare(geom)
        pontos = shapely.point_on_surface(np.array(faces, dtype=object))
        dentro = shapely.contains(geom, pontos)
        return [face for face, ok in zip(faces, dentro) if ok]

    def _terrain_cells(self):
        """Celulas da grade do terreno + STRtree, construidas uma vez so.

        Sao as celulas quadradas, nao os dois triangulos de cada uma. Dentro de
        uma celula o terreno tem uma dobra na diagonal, mas a diferenca que ela
        causa e uma fracao da torcao da celula - bem abaixo da folga de 8 cm com
        que as superficies sao assentadas. Usar quadrados corta pela metade o
        numero de geometrias, e esse numero e o que dita o custo.
        """
        if self._cells is not None:
            return self._cells, self._cell_tree

        xs = getattr(self.terrain, "xs", None)
        ys = getattr(self.terrain, "ys", None)
        if xs is None or ys is None or len(xs) < 2 or len(ys) < 2:
            return None, None
        if (len(xs) - 1) * (len(ys) - 1) > 250_000:  # protecao contra area absurda
            return None, None

        import shapely
        from shapely import STRtree

        # Vetorizado: `shapely.box` sobre arrays cria todas as celulas de uma
        # vez. Num laco Python isto sozinho levava dezenas de segundos.
        x0, y0 = np.meshgrid(xs[:-1], ys[:-1], indexing="ij")
        x1, y1 = np.meshgrid(xs[1:], ys[1:], indexing="ij")
        self._cells = shapely.box(x0.ravel(), y0.ravel(), x1.ravel(), y1.ravel())
        self._cell_tree = STRtree(self._cells)
        return self._cells, self._cell_tree

    # ------------------------------------------------------------------ base

    def add_mesh(
        self,
        material: Material,
        vertices: np.ndarray,
        faces: np.ndarray,
        uv: np.ndarray | None = None,
    ) -> None:
        """Adiciona triangulos ja prontos (vertices Nx3 em Z-up)."""
        vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
        faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
        if len(faces) == 0:
            return
        group = self._groups.get(material.name)
        if group is None:
            group = self._groups[material.name] = _Group(material)
        if uv is not None:
            uv = np.asarray(uv, dtype=np.float64).reshape(-1, 2)
        group.append(vertices, faces, uv)

    def add_quad(
        self,
        material: Material,
        p0: np.ndarray,
        p1: np.ndarray,
        p2: np.ndarray,
        p3: np.ndarray,
    ) -> None:
        """Quadrilatero com vertices em ordem anti-horaria vistos pela face frontal."""
        verts = np.array([p0, p1, p2, p3], dtype=np.float64)
        faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
        self.add_mesh(material, verts, faces)

    # -------------------------------------------------------------- superficies

    def add_flat(
        self,
        material: Material,
        geom,
        z: float = 0.0,
        flip: bool = False,
        uv_bounds: tuple[float, float, float, float] | None = None,
        drape: bool = False,
    ) -> None:
        """Superficie horizontal (chao, asfalto, agua) na altura z.

        `uv_bounds` = (minx, miny, maxx, maxy) em metros locais liga o mapeamento
        planar de textura: a imagem cobre esse retangulo, com V invertido porque
        no glTF a linha 0 fica no topo (norte).

        `drape` faz a superficie acompanhar o relevo: z passa a ser a folga
        acima do terreno, nao a altura absoluta.
        """
        geom = self.clipped(geom)
        if geom is None or geom.is_empty:
            return
        if drape and self.terrain is not None:
            # Recorta pelos triangulos do terreno antes de triangular, senao o
            # earcut cria triangulos que atravessam varios morros. O recorte ja
            # insere os vertices nas bordas das celulas, entao nao ha o que
            # densificar aqui.
            for piece in self.grid_split(geom):
                self._add_flat_piece(material, piece, z, flip, uv_bounds, drape=True)
            return
        self._add_flat_piece(material, geom, z, flip, uv_bounds, drape=False)

    def _add_flat_piece(
        self,
        material: Material,
        geom,
        z: float,
        flip: bool,
        uv_bounds: tuple[float, float, float, float] | None,
        drape: bool,
    ) -> None:
        for poly in _as_polygons(geom):
            verts2d, faces = _triangulate(poly)
            if len(faces) == 0:
                continue
            heights = np.full(len(verts2d), z)
            if drape:
                heights = heights + self._drape(verts2d) + self.drape_bias
            verts = np.column_stack([verts2d, heights])
            if flip:
                faces = faces[:, [0, 2, 1]]

            uv = None
            if uv_bounds is not None:
                minx, miny, maxx, maxy = uv_bounds
                span_x = max(maxx - minx, 1e-9)
                span_y = max(maxy - miny, 1e-9)
                uv = np.column_stack(
                    [
                        (verts2d[:, 0] - minx) / span_x,
                        (maxy - verts2d[:, 1]) / span_y,
                    ]
                )
            self.add_mesh(material, verts, faces, uv)

    def add_walls(
        self,
        material: Material,
        geom,
        base_z: float,
        top_z: float,
        drape: bool = False,
    ) -> None:
        """Paredes verticais ao longo de todos os aneis do poligono.

        Com `drape`, base e topo sobem juntos com o terreno: a parede mantem a
        espessura e acompanha a encosta (util no degrau da calcada).
        """
        geom = self.clipped(geom)
        if geom is None or geom.is_empty:
            return
        if drape:
            geom = self.densify(geom)
        for poly in _as_polygons(geom):
            rings = [poly.exterior] + list(poly.interiors)
            for ring in rings:
                coords = np.asarray(ring.coords, dtype=np.float64)
                if len(coords) < 3:
                    continue
                if np.allclose(coords[0], coords[-1]):
                    coords = coords[:-1]
                n = len(coords)
                if n < 3:
                    continue
                nxt = np.roll(coords, -1, axis=0)

                ground0 = (self._drape(coords) + self.drape_bias) if drape else np.zeros(n)
                ground1 = np.roll(ground0, -1) if drape else np.zeros(n)

                # Para cada aresta: base(p0), base(p1), topo(p1), topo(p0).
                bottom0 = np.column_stack([coords, ground0 + base_z])
                bottom1 = np.column_stack([nxt, ground1 + base_z])
                top1 = np.column_stack([nxt, ground1 + top_z])
                top0 = np.column_stack([coords, ground0 + top_z])
                verts = np.concatenate([bottom0, bottom1, top1, top0], axis=0)
                idx = np.arange(n)
                faces = np.concatenate(
                    [
                        np.column_stack([idx, idx + n, idx + 2 * n]),
                        np.column_stack([idx, idx + 2 * n, idx + 3 * n]),
                    ],
                    axis=0,
                )
                self.add_mesh(material, verts, faces)

    def add_prism(
        self,
        material: Material,
        geom,
        base_z: float,
        top_z: float,
        cap_top: bool = True,
        cap_bottom: bool = False,
        top_material: Material | None = None,
        drape: bool = False,
    ) -> None:
        """Extrusao vertical de um poligono: paredes + tampas."""
        if top_z <= base_z:
            return
        self.add_walls(material, geom, base_z, top_z, drape=drape)
        if cap_top:
            self.add_flat(top_material or material, geom, top_z, drape=drape)
        if cap_bottom:
            self.add_flat(material, geom, base_z, flip=True, drape=drape)

    def add_slope(
        self,
        material: Material,
        ring_geom,
        inner_geom,
        outer_z: float,
        inner_z: float,
        width: float,
    ) -> None:
        """Superficie inclinada entre dois aneis concentricos (talude de margem).

        Triangula o anel em 2D e define a altura de cada vertice pela distancia
        ate o contorno interno: quem esta sobre ele fica em inner_z, quem esta a
        `width` de distancia fica em outer_z.
        """
        if width <= 1e-6 or inner_geom is None or inner_geom.is_empty:
            return
        boundary = inner_geom.boundary
        if boundary.is_empty:
            return

        from shapely import distance as shapely_distance
        from shapely.geometry import Point

        for poly in _as_polygons(ring_geom):
            verts2d, faces = _triangulate(poly)
            if len(faces) == 0:
                continue
            points = [Point(x, y) for x, y in verts2d]
            dist = np.asarray(shapely_distance(points, boundary), dtype=np.float64)
            t = np.clip(dist / width, 0.0, 1.0)
            z = inner_z + t * (outer_z - inner_z)
            self.add_mesh(material, np.column_stack([verts2d, z]), faces)

    # ---------------------------------------------------------------- instancias

    def add_instances(
        self,
        material: Material,
        prototype: tuple[np.ndarray, np.ndarray],
        transforms: np.ndarray,
    ) -> None:
        """Replica um prototipo (verts, faces) em varias posicoes/escalas/rotacoes.

        `transforms` e um array (n, 5): x, y, z, escala, rotacao_z (radianos).
        A geometria e assada na malha final (sem instancing de GPU) para manter o
        GLB portavel entre engines.
        """
        proto_v, proto_f = prototype
        proto_v = np.asarray(proto_v, dtype=np.float64).reshape(-1, 3)
        proto_f = np.asarray(proto_f, dtype=np.int64).reshape(-1, 3)
        transforms = np.asarray(transforms, dtype=np.float64).reshape(-1, 5)
        if len(transforms) == 0 or len(proto_f) == 0:
            return

        n_v = len(proto_v)
        cos = np.cos(transforms[:, 4])
        sin = np.sin(transforms[:, 4])
        scale = transforms[:, 3][:, None]

        base = proto_v[None, :, :] * scale[:, :, None]  # (n, n_v, 3)
        x = base[:, :, 0] * cos[:, None] - base[:, :, 1] * sin[:, None]
        y = base[:, :, 0] * sin[:, None] + base[:, :, 1] * cos[:, None]
        z = base[:, :, 2]
        verts = np.stack([x, y, z], axis=-1) + transforms[:, None, 0:3]

        offsets = (np.arange(len(transforms)) * n_v)[:, None, None]
        faces = proto_f[None, :, :] + offsets

        self.add_mesh(material, verts.reshape(-1, 3), faces.reshape(-1, 3))

    # -------------------------------------------------------------------- saida

    def is_empty(self) -> bool:
        return not any(g.faces for g in self._groups.values())

    def triangle_count(self) -> int:
        return sum(sum(len(f) for f in g.faces) for g in self._groups.values())

    def build(self) -> dict[str, MeshGroup]:
        out: dict[str, MeshGroup] = {}
        for name, group in self._groups.items():
            if not group.faces:
                continue
            out[name] = MeshGroup(
                material=group.material,
                vertices=np.concatenate(group.vertices, axis=0),
                faces=np.concatenate(group.faces, axis=0),
                uv=np.concatenate(group.uvs, axis=0) if group.has_uv else None,
            )
        return out


@dataclass
class Scene:
    """Resultado da geracao: malhas por material + metadados para o projeto."""

    name: str
    groups: dict[str, MeshGroup]
    metadata: dict = field(default_factory=dict)

    @property
    def triangle_count(self) -> int:
        return sum(len(g.faces) for g in self.groups.values())

    @property
    def vertex_count(self) -> int:
        return sum(len(g.vertices) for g in self.groups.values())

    def bounds(self) -> np.ndarray:
        if not self.groups:
            return np.zeros((2, 3))
        mins = np.min([g.vertices.min(axis=0) for g in self.groups.values()], axis=0)
        maxs = np.max([g.vertices.max(axis=0) for g in self.groups.values()], axis=0)
        return np.array([mins, maxs])
