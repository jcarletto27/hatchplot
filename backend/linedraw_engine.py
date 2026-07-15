"""Plotter-oriented raster-to-polyline conversion.

The contour/hatch strategy and nearest-endpoint stroke ordering are adapted from
Lingdong Huang's ``linedraw`` project:
https://github.com/LingDong-/linedraw

Original project copyright (c) 2017 Lingdong Huang, used under the MIT License.
HatchPlot's implementation is a Python 3/OpenCV rewrite designed to emit clean,
non-filled SVG polylines and deterministic output suitable for toolpath import.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import math
from typing import Iterable, Sequence

import cv2
import numpy as np
from PIL import Image, ImageOps

Point = tuple[float, float]
Polyline = list[Point]


@dataclass(frozen=True)
class LineDrawSettings:
    mode: str = "contour-hatch"
    output_width_mm: float = 150.0
    max_dimension: int = 1024
    auto_contrast_cutoff: float = 2.0
    blur_radius: int = 1
    alpha_threshold: int = 16
    invert: bool = False
    white_background: bool = True
    contour_low_threshold: int = 70
    contour_high_threshold: int = 180
    contour_simplify: float = 1.5
    minimum_contour_length: float = 10.0
    hatch_size: int = 16
    hatch_light_threshold: int = 160
    hatch_mid_threshold: int = 96
    hatch_dark_threshold: int = 40
    sort_strokes: bool = True
    stroke_width_mm: float = 0.35
    maximum_strokes: int = 30000
    maximum_points: int = 500000


@dataclass(frozen=True)
class PreparedRaster:
    gray: np.ndarray
    width: int
    height: int
    source_width: int
    source_height: int


@dataclass(frozen=True)
class LineDrawResult:
    svg: str
    contours: tuple[Polyline, ...]
    hatches: tuple[Polyline, ...]
    width: int
    height: int
    output_width_mm: float
    output_height_mm: float
    point_count: int
    travel_distance_px: float

    @property
    def stroke_count(self) -> int:
        return len(self.contours) + len(self.hatches)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _polyline_length(line: Sequence[Point]) -> float:
    if len(line) < 2:
        return 0.0
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(line, line[1:]))


def _prepare_raster(image: Image.Image, settings: LineDrawSettings) -> PreparedRaster:
    rgba = image.convert("RGBA")
    source_width, source_height = rgba.size
    if source_width < 1 or source_height < 1:
        raise ValueError("The source image has invalid dimensions.")

    maximum_dimension = int(_clamp(settings.max_dimension, 128, 4096))
    scale = min(1.0, maximum_dimension / max(source_width, source_height))
    width = max(1, int(round(source_width * scale)))
    height = max(1, int(round(source_height * scale)))
    if rgba.size != (width, height):
        rgba = rgba.resize((width, height), Image.Resampling.LANCZOS)

    pixels = np.asarray(rgba, dtype=np.uint8)
    alpha = pixels[:, :, 3]
    rgb = pixels[:, :, :3].astype(np.float32)
    if settings.white_background:
        alpha_fraction = (alpha.astype(np.float32) / 255.0)[:, :, None]
        rgb = (rgb * alpha_fraction) + (255.0 * (1.0 - alpha_fraction))

    gray = cv2.cvtColor(np.clip(rgb, 0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    transparent = alpha < int(_clamp(settings.alpha_threshold, 0, 255))
    if settings.invert:
        gray = 255 - gray
    # Transparent pixels always remain empty, even when luminance is inverted.
    gray[transparent] = 255

    cutoff = float(_clamp(settings.auto_contrast_cutoff, 0.0, 20.0))
    if cutoff > 0:
        gray = np.asarray(ImageOps.autocontrast(Image.fromarray(gray), cutoff=cutoff), dtype=np.uint8)

    blur_radius = int(_clamp(settings.blur_radius, 0, 12))
    if blur_radius > 0:
        kernel = (blur_radius * 2) + 1
        gray = cv2.GaussianBlur(gray, (kernel, kernel), 0)

    return PreparedRaster(
        gray=gray,
        width=width,
        height=height,
        source_width=source_width,
        source_height=source_height,
    )


def _neighbors(point: tuple[int, int], pixels: set[tuple[int, int]]) -> list[tuple[int, int]]:
    x, y = point
    result: list[tuple[int, int]] = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neighbor = (x + dx, y + dy)
            if neighbor in pixels:
                result.append(neighbor)
    return result


def _edge_key(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    return (a, b) if a <= b else (b, a)


def _trace_binary_lines(binary: np.ndarray) -> list[Polyline]:
    ys, xs = np.nonzero(binary)
    pixels = set(zip(xs.tolist(), ys.tolist()))
    if not pixels:
        return []

    adjacency = {point: _neighbors(point, pixels) for point in pixels}
    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    paths: list[Polyline] = []

    def walk(start: tuple[int, int], next_point: tuple[int, int]) -> Polyline:
        path: Polyline = [(float(start[0]), float(start[1]))]
        previous = start
        current = next_point
        visited.add(_edge_key(previous, current))
        path.append((float(current[0]), float(current[1])))

        while True:
            candidates = [
                point for point in adjacency[current]
                if point != previous and _edge_key(current, point) not in visited
            ]
            if not candidates:
                break
            if len(adjacency[current]) != 2:
                break
            following = candidates[0]
            previous, current = current, following
            visited.add(_edge_key(previous, current))
            path.append((float(current[0]), float(current[1])))
        return path

    endpoints = sorted(point for point, linked in adjacency.items() if len(linked) != 2)
    for start in endpoints:
        for linked in adjacency[start]:
            if _edge_key(start, linked) in visited:
                continue
            path = walk(start, linked)
            if len(path) >= 2:
                paths.append(path)

    # Remaining unvisited edges are closed cycles.
    for start in sorted(pixels):
        for linked in adjacency[start]:
            if _edge_key(start, linked) in visited:
                continue
            path = walk(start, linked)
            if len(path) >= 3:
                if path[0] != path[-1]:
                    path.append(path[0])
                paths.append(path)

    return paths


def _simplify_line(line: Polyline, tolerance: float, closed: bool = False) -> Polyline:
    if len(line) < 3 or tolerance <= 0:
        return line
    array = np.asarray(line, dtype=np.float32).reshape(-1, 1, 2)
    simplified = cv2.approxPolyDP(array, float(tolerance), bool(closed))
    if simplified is None or len(simplified) < 2:
        return line
    result = [(float(point[0][0]), float(point[0][1])) for point in simplified]
    if closed and result[0] != result[-1]:
        result.append(result[0])
    return result


def trace_contours(gray: np.ndarray, settings: LineDrawSettings) -> list[Polyline]:
    low = int(_clamp(settings.contour_low_threshold, 1, 254))
    high = int(_clamp(settings.contour_high_threshold, low + 1, 255))
    edges = cv2.Canny(gray, low, high, L2gradient=True)
    lines = _trace_binary_lines(edges > 0)

    minimum_length = float(_clamp(settings.minimum_contour_length, 0.0, 100000.0))
    tolerance = float(_clamp(settings.contour_simplify, 0.0, 20.0))
    result: list[Polyline] = []
    for line in lines:
        closed = len(line) > 3 and line[0] == line[-1]
        simplified = _simplify_line(line, tolerance, closed=closed)
        if _polyline_length(simplified) >= minimum_length:
            result.append(simplified)
    return result


def _trace_segment_graph(segments: Iterable[tuple[Point, Point]]) -> list[Polyline]:
    adjacency: dict[Point, list[Point]] = {}
    for start, end in segments:
        if start == end:
            continue
        adjacency.setdefault(start, []).append(end)
        adjacency.setdefault(end, []).append(start)
    if not adjacency:
        return []

    visited: set[tuple[Point, Point]] = set()

    def key(a: Point, b: Point) -> tuple[Point, Point]:
        return (a, b) if a <= b else (b, a)

    def walk(start: Point, linked: Point) -> Polyline:
        path = [start, linked]
        previous = start
        current = linked
        visited.add(key(previous, current))
        while True:
            choices = [candidate for candidate in adjacency[current] if candidate != previous and key(current, candidate) not in visited]
            if not choices or len(adjacency[current]) != 2:
                break
            following = choices[0]
            previous, current = current, following
            visited.add(key(previous, current))
            path.append(current)
        return path

    paths: list[Polyline] = []
    starts = sorted(point for point, linked in adjacency.items() if len(linked) != 2)
    for start in starts:
        for linked in adjacency[start]:
            if key(start, linked) in visited:
                continue
            paths.append(walk(start, linked))
    for start in sorted(adjacency):
        for linked in adjacency[start]:
            if key(start, linked) in visited:
                continue
            path = walk(start, linked)
            if path[0] != path[-1]:
                path.append(path[0])
            paths.append(path)
    return paths


def generate_hatches(gray: np.ndarray, settings: LineDrawSettings) -> list[Polyline]:
    height, width = gray.shape[:2]
    cell = int(_clamp(settings.hatch_size, 4, 128))
    columns = max(1, math.ceil(width / cell))
    rows = max(1, math.ceil(height / cell))
    sampled = cv2.resize(gray, (columns, rows), interpolation=cv2.INTER_AREA)

    light = int(_clamp(settings.hatch_light_threshold, 1, 254))
    mid = int(_clamp(settings.hatch_mid_threshold, 0, light))
    dark = int(_clamp(settings.hatch_dark_threshold, 0, mid))

    primary: list[tuple[Point, Point]] = []
    secondary: list[tuple[Point, Point]] = []
    diagonal: list[tuple[Point, Point]] = []

    for row in range(rows):
        y0 = float(row * cell)
        y1 = float(min(height - 1, (row + 1) * cell))
        if y1 <= y0:
            continue
        for column in range(columns):
            tone = int(sampled[row, column])
            if tone > light:
                continue
            x0 = float(column * cell)
            x1 = float(min(width - 1, (column + 1) * cell))
            if x1 <= x0:
                continue

            primary_y = min(float(height - 1), y0 + ((y1 - y0) * 0.25))
            primary.append(((x0, primary_y), (x1, primary_y)))

            if tone <= mid:
                diagonal.append(((x1, y0), (x0, y1)))
            if tone <= dark:
                secondary_y = min(float(height - 1), y0 + ((y1 - y0) * 0.75))
                secondary.append(((x0, secondary_y), (x1, secondary_y)))

    return _trace_segment_graph(primary) + _trace_segment_graph(secondary) + _trace_segment_graph(diagonal)


def sort_strokes(lines: Sequence[Polyline]) -> list[Polyline]:
    """Nearest-endpoint ordering adapted from linedraw/strokesort.py."""
    if len(lines) < 2:
        return [list(line) for line in lines]

    remaining = [list(line) for line in lines if len(line) >= 2]
    ordered = [remaining.pop(0)]
    while remaining:
        current = np.asarray(ordered[-1][-1], dtype=np.float64)
        starts = np.asarray([line[0] for line in remaining], dtype=np.float64)
        ends = np.asarray([line[-1] for line in remaining], dtype=np.float64)
        start_distance = np.sum((starts - current) ** 2, axis=1)
        end_distance = np.sum((ends - current) ** 2, axis=1)
        start_index = int(np.argmin(start_distance))
        end_index = int(np.argmin(end_distance))
        if end_distance[end_index] < start_distance[start_index]:
            line = remaining.pop(end_index)
            ordered.append(list(reversed(line)))
        else:
            ordered.append(remaining.pop(start_index))
    return ordered


def _travel_distance(lines: Sequence[Polyline]) -> float:
    distance = 0.0
    previous: Point | None = None
    for line in lines:
        if not line:
            continue
        if previous is not None:
            distance += math.hypot(line[0][0] - previous[0], line[0][1] - previous[1])
        previous = line[-1]
    return distance


def _number(value: float) -> str:
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _polyline_element(line: Sequence[Point]) -> str:
    points = " ".join(f"{_number(x)},{_number(y)}" for x, y in line)
    return f'<polyline points="{points}"/>'


def _group_svg(group_id: str, lines: Sequence[Polyline]) -> str:
    body = "".join(_polyline_element(line) for line in lines if len(line) >= 2)
    return f'<g id="{escape(group_id)}" data-hatchplot-pass="{escape(group_id)}">{body}</g>' if body else ""


def convert_image(image: Image.Image, filename: str, settings: LineDrawSettings) -> LineDrawResult:
    mode = settings.mode
    aliases = {
        "edges": "contour",
        "posterize": "contour-hatch",
        "silhouette": "contour",
    }
    mode = aliases.get(mode, mode)
    if mode not in {"contour", "hatch", "contour-hatch"}:
        raise ValueError("Conversion mode must be contour, hatch, or contour-hatch.")

    prepared = _prepare_raster(image, settings)
    contours = trace_contours(prepared.gray, settings) if mode in {"contour", "contour-hatch"} else []
    hatches = generate_hatches(prepared.gray, settings) if mode in {"hatch", "contour-hatch"} else []

    stroke_count = len(contours) + len(hatches)
    point_count = sum(len(line) for line in contours) + sum(len(line) for line in hatches)
    if stroke_count == 0:
        raise ValueError("No plotter strokes survived the current settings.")
    if stroke_count > settings.maximum_strokes:
        raise ValueError(f"The conversion produced {stroke_count:,} strokes; increase simplification or hatch size.")
    if point_count > settings.maximum_points:
        raise ValueError(f"The conversion produced {point_count:,} points; reduce resolution or increase simplification.")

    if settings.sort_strokes:
        # Keep contours before hatches so the workspace can preserve an
        # outline-first plotting sequence while minimizing travel in each pass.
        contours = sort_strokes(contours)
        hatches = sort_strokes(hatches)

    output_width = float(_clamp(settings.output_width_mm, 1.0, 5000.0))
    output_height = output_width * (prepared.height / prepared.width)
    stroke_width_px = max(0.1, settings.stroke_width_mm * prepared.width / output_width)
    groups = [
        _group_svg("contours", contours),
        _group_svg("hatches", hatches),
    ]
    groups = [group for group in groups if group]
    metadata = (
        f"engine=linedraw-inspired; mode={mode}; trace={prepared.width}x{prepared.height}; "
        f"hatch-size={settings.hatch_size}; contour-simplify={settings.contour_simplify}"
    )
    svg = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{output_width:.4f}mm" height="{output_height:.4f}mm" viewBox="0 0 {prepared.width} {prepared.height}">',
        f'<title>{escape(filename or "converted-image")}</title>',
        f'<metadata>{escape(metadata)}</metadata>',
        f'<g fill="none" stroke="#000000" stroke-width="{_number(stroke_width_px)}" stroke-linecap="round" stroke-linejoin="round">',
        *groups,
        '</g>',
        '</svg>',
    ])

    return LineDrawResult(
        svg=svg,
        contours=tuple(contours),
        hatches=tuple(hatches),
        width=prepared.width,
        height=prepared.height,
        output_width_mm=output_width,
        output_height_mm=output_height,
        point_count=point_count,
        travel_distance_px=_travel_distance(contours + hatches),
    )
