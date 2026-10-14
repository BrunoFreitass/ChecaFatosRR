"""Gera src/lib/mapaUf.ts (contorno das 27 UFs em SVG) a partir do Natural Earth.

Fonte: Natural Earth, "Admin 1 – States, Provinces", escala 1:50m
(ne_50m_admin_1_states_provinces). Licença: domínio público — "No permission is needed to use
Natural Earth" (https://www.naturalearthdata.com/about/terms-of-use/). Rodado uma vez à mão;
o resultado vai para o repositório (o build não baixa nada).

Uso (sem dependências além do Python):
    python frontend/scripts/mapa_uf.py ne_50m_admin_1_states_provinces.geojson \
        > frontend/src/lib/mapaUf.ts
"""

import json
import math
import sys
from typing import Any

WIDTH = 600  # largura do viewBox; a altura sai da proporção do Brasil
TOLERANCE = 0.9  # Douglas–Peucker, em unidades do viewBox (~1,5 px num celular)
MIN_RING_POINTS = 4  # anel menor que isso some (ilhotas)
LAT_REF = -15.0  # equiretangular com o cosseno da latitude média do Brasil


def project(lon: float, lat: float) -> tuple[float, float]:
    return lon * math.cos(math.radians(LAT_REF)), -lat


def simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy) or 1e-12
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        dist = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
        if dist > worst:
            worst, index = dist, i
    if worst <= tol:
        return [points[0], points[-1]]
    return simplify(points[: index + 1], tol)[:-1] + simplify(points[index:], tol)


def rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    if geometry["type"] == "Polygon":
        return geometry["coordinates"]
    return [ring for polygon in geometry["coordinates"] for ring in polygon]


def main(path: str) -> None:
    data = json.load(open(path, encoding="utf-8"))
    states = [f for f in data["features"] if f["properties"].get("adm0_a3") == "BRA"]
    projected = {
        f["properties"]["iso_3166_2"][3:].lower(): [
            [project(lon, lat) for lon, lat, *_ in ring] for ring in rings(f["geometry"])
        ]
        for f in states
    }
    xs = [x for rs in projected.values() for r in rs for x, _ in r]
    ys = [y for rs in projected.values() for r in rs for _, y in r]
    scale = WIDTH / (max(xs) - min(xs))
    height = round((max(ys) - min(ys)) * scale)
    paths: dict[str, str] = {}
    for uf, rs in sorted(projected.items()):
        parts = []
        for ring in rs:
            pts = [((x - min(xs)) * scale, (y - min(ys)) * scale) for x, y in ring]
            # Anel fechado (início = fim): simplifica em duas metades, senão a "reta" de
            # referência tem comprimento zero e o Douglas–Peucker descarta tudo.
            mid = len(pts) // 2
            pts = simplify(pts[: mid + 1], TOLERANCE)[:-1] + simplify(pts[mid:], TOLERANCE)
            if len(pts) < MIN_RING_POINTS:
                continue
            coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts[:-1])
            parts.append(f"M{coords}Z")
        paths[uf] = "".join(parts)
    print("// Gerado por frontend/scripts/mapa_uf.py. Não edite à mão.")
    print("// Fonte: Natural Earth, Admin 1 – States, Provinces, 1:50m.")
    print("// Licença: domínio público (https://www.naturalearthdata.com/about/terms-of-use/).")
    print(f'export const MAPA_VIEWBOX = "0 0 {WIDTH} {height}";')
    print("export const MAPA_UF: Record<string, string> = {")
    for uf, d in paths.items():
        print(f'  {uf}: "{d}",')
    print("};")


if __name__ == "__main__":
    main(sys.argv[1])
