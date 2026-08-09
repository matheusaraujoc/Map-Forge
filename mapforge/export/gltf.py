"""Exportador: Scene interna -> GLB/GLTF/OBJ/PLY via trimesh.

A cena e construida em Z-up (metros). O glTF usa Y-up, entao aplicamos uma
rotacao de -90 graus em X na saida - uma rotacao propria, que preserva o
sentido de giro dos triangulos.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import trimesh
from trimesh.transformations import rotation_matrix

from ..core.mesh import Material, Scene

log = logging.getLogger(__name__)

SUPPORTED = {".glb", ".gltf", ".obj", ".ply", ".stl"}

# Z-up (X leste, Y norte, Z cima) -> Y-up do glTF.
ZUP_TO_YUP = rotation_matrix(-np.pi / 2.0, [1.0, 0.0, 0.0])


def _pbr(material: Material) -> trimesh.visual.material.PBRMaterial:
    r, g, b, a = material.rgba8()
    kwargs = {
        "name": material.name,
        "baseColorFactor": [r, g, b, a],
        "metallicFactor": float(material.metallic),
        "roughnessFactor": float(material.roughness),
        "doubleSided": True,
    }
    if material.alpha_cutoff is not None:
        # MASK vem antes de BLEND: recorte nao precisa de ordenacao por
        # profundidade, e vegetacao transparente ordenada e um pesadelo.
        kwargs["alphaMode"] = "MASK"
        kwargs["alphaCutoff"] = float(material.alpha_cutoff)
    elif material.opacity < 1.0:
        kwargs["alphaMode"] = "BLEND"
    if material.emissive:
        kwargs["emissiveFactor"] = [float(c) for c in material.emissive]
    if material.texture is not None:
        # Com textura, a cor base vira multiplicador: branco preserva a imagem.
        kwargs["baseColorTexture"] = material.texture
        kwargs["baseColorFactor"] = [255, 255, 255, a]
    return trimesh.visual.material.PBRMaterial(**kwargs)


def to_trimesh_scene(scene: Scene, yup: bool = True) -> trimesh.Scene:
    """Converte a cena interna numa trimesh.Scene, uma geometria por material."""
    out = trimesh.Scene()
    transform = ZUP_TO_YUP if yup else np.eye(4)

    for name, group in scene.groups.items():
        if len(group.faces) == 0:
            continue
        mesh = trimesh.Trimesh(
            vertices=group.vertices, faces=group.faces, process=False, validate=False
        )
        try:
            mesh.visual = trimesh.visual.TextureVisuals(
                uv=group.uv, material=_pbr(group.material)
            )
        except Exception:  # noqa: BLE001 - fallback para cor por vertice
            log.debug("PBR indisponivel para %s, usando cor por vertice", name)
            mesh.visual.vertex_colors = np.tile(group.material.rgba8(), (len(mesh.vertices), 1))
        out.add_geometry(mesh, geom_name=name, node_name=name, transform=transform)

    out.metadata.update(scene.metadata)
    return out


def export_scene(scene: Scene, path: str | Path, yup: bool = True) -> Path:
    """Grava a cena no formato deduzido pela extensao. Retorna o caminho final."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(
            f"formato nao suportado: {suffix!r}. Use um de: {', '.join(sorted(SUPPORTED))}"
        )
    if scene.triangle_count == 0:
        raise ValueError("cena vazia: nada para exportar")

    path.parent.mkdir(parents=True, exist_ok=True)
    tri_scene = to_trimesh_scene(scene, yup=yup)

    if suffix in (".glb", ".gltf"):
        # include_normals=False mantem o sombreamento facetado do low-poly e
        # reduz o arquivo; se a versao do trimesh nao aceitar, exporta normal.
        try:
            data = tri_scene.export(file_type=suffix.lstrip("."), include_normals=False)
        except TypeError:
            data = tri_scene.export(file_type=suffix.lstrip("."))
        mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
        with open(path, mode) as handle:
            handle.write(data)
    else:
        merged = tri_scene.dump(concatenate=True)
        merged.export(path)

    sidecar = path.with_suffix(path.suffix + ".json")
    sidecar.write_text(json.dumps(scene.metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("Exportado %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path
