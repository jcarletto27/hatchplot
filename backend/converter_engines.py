"""Optional raster-to-SVG converter engines.

The base HatchPlot image uses only the built-in linedraw-inspired engine.  The
Potrace and Pixels2SVG adapters are activated when their GPL-licensed Python
packages are installed (see ``requirements-gpl.txt`` and ``compose.gpl.yml``).
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import io
import math
import os
import tempfile
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:  # Optional GPLv2+ dependency.
    import potrace as _potrace
except Exception as exc:  # pragma: no cover - depends on optional install.
    _potrace = None
    _potrace_import_error = str(exc)
else:
    _potrace_import_error = ""

try:  # Optional GPLv3+ dependency.
    from pixels2svg import pixels2svg as _pixels2svg_convert
except Exception as exc:  # pragma: no cover - depends on optional install.
    _pixels2svg_convert = None
    _pixels2svg_import_error = str(exc)
else:
    _pixels2svg_import_error = ""


@dataclass(frozen=True)
class ConverterEngineResult:
    svg: str
    width: int
    height: int
    output_width_mm: float
    output_height_mm: float
    path_count: int
    point_count: int
    engine: str
    engine_detail: str


@dataclass(frozen=True)
class CommonRasterSettings:
    output_width_mm: float = 150.0
    max_dimension: int = 1024
    auto_contrast_cutoff: float = 2.0
    blur_radius: int = 1
    alpha_threshold: int = 16
    invert: bool = False
    white_background: bool = True


@dataclass(frozen=True)
class PotraceSettings:
    threshold: int = 128
    turd_size: int = 2
    turn_policy: str = "minority"
    alpha_max: float = 1.0
    optimize_curves: bool = True
    optimize_tolerance: float = 0.2


@dataclass(frozen=True)
class Pixels2SvgSettings:
    max_colors: int = 16
    color_tolerance: int = 64
    remove_background: bool = True
    background_tolerance: float = 1.0
    maximum_artifact_percent: float = 0.1
    group_by_color: bool = True
    maximum_svg_bytes: int = 25 * 1024 * 1024
    maximum_paths: int = 50000


def converter_engine_status() -> dict[str, dict[str, Any]]:
    """Return runtime availability and licensing metadata for each engine."""
    return {
        "linedraw": {
            "available": True,
            "label": "Linedraw",
            "license": "MIT-derived implementation",
            "description": "Plotter-oriented open contours and tonal hatches.",
        },
        "potrace": {
            "available": _potrace is not None,
            "label": "Potrace",
            "license": "GPL-2.0-or-later",
            "description": "Smooth closed monochrome contours for logos, line art, and silhouettes.",
            "reason": _potrace_import_error if _potrace is None else "",
        },
        "pixels2svg": {
            "available": _pixels2svg_convert is not None,
            "label": "Pixels2SVG",
            "license": "GPL-3.0-or-later",
            "description": "Color-region polygons that preserve pixel-art and segmentation boundaries.",
            "reason": _pixels2svg_import_error if _pixels2svg_convert is None else "",
        },
    }


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _prepare_rgba(image: Image.Image, settings: CommonRasterSettings) -> Image.Image:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width < 1 or height < 1:
        raise ValueError("The source image has invalid dimensions.")

    maximum = int(_clamp(settings.max_dimension, 64, 4096))
    scale = min(1.0, maximum / max(width, height))
    target = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    if target != rgba.size:
        rgba = rgba.resize(target, Image.Resampling.LANCZOS)

    pixels = np.asarray(rgba, dtype=np.uint8).copy()
    alpha = pixels[:, :, 3]
    alpha[alpha < int(_clamp(settings.alpha_threshold, 0, 255))] = 0
    pixels[:, :, 3] = alpha
    rgba = Image.fromarray(pixels, "RGBA")

    background = (255, 255, 255, 255) if settings.white_background else (0, 0, 0, 0)
    if settings.white_background:
        canvas = Image.new("RGBA", rgba.size, background)
        canvas.alpha_composite(rgba)
        rgba = canvas

    if settings.blur_radius > 0:
        rgba = rgba.filter(ImageFilter.GaussianBlur(radius=int(_clamp(settings.blur_radius, 0, 12))))
    return rgba


def _prepare_gray(image: Image.Image, settings: CommonRasterSettings) -> Image.Image:
    rgba = _prepare_rgba(image, settings)
    if not settings.white_background:
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        canvas.alpha_composite(rgba)
        rgba = canvas
    gray = rgba.convert("L")
    cutoff = _clamp(settings.auto_contrast_cutoff, 0.0, 20.0)
    if cutoff > 0:
        gray = ImageOps.autocontrast(gray, cutoff=cutoff)
    if settings.invert:
        gray = ImageOps.invert(gray)
    return gray


def _number(value: float) -> str:
    rounded = round(float(value), 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _point_xy(point: Any) -> tuple[float, float]:
    return float(point.x), float(point.y)


def convert_potrace(
    image: Image.Image,
    filename: str,
    common: CommonRasterSettings,
    settings: PotraceSettings,
) -> ConverterEngineResult:
    if _potrace is None:
        raise ValueError("Potrace is not installed. Rebuild HatchPlot with the GPL converter profile.")

    gray = _prepare_gray(image, common)
    width, height = gray.size
    threshold = int(_clamp(settings.threshold, 0, 255)) / 255.0
    policies = {
        "black": _potrace.POTRACE_TURNPOLICY_BLACK,
        "white": _potrace.POTRACE_TURNPOLICY_WHITE,
        "left": _potrace.POTRACE_TURNPOLICY_LEFT,
        "right": _potrace.POTRACE_TURNPOLICY_RIGHT,
        "minority": _potrace.POTRACE_TURNPOLICY_MINORITY,
        "majority": _potrace.POTRACE_TURNPOLICY_MAJORITY,
        "random": _potrace.POTRACE_TURNPOLICY_RANDOM,
    }
    policy = policies.get(settings.turn_policy, policies["minority"])
    bitmap = _potrace.Bitmap(gray, blacklevel=threshold)
    curves = bitmap.trace(
        turdsize=max(0, int(settings.turd_size)),
        turnpolicy=policy,
        alphamax=_clamp(settings.alpha_max, 0.0, 1.334),
        opticurve=bool(settings.optimize_curves),
        opttolerance=_clamp(settings.optimize_tolerance, 0.0, 5.0),
    )

    path_parts: list[str] = []
    segment_count = 0
    curve_count = 0
    for curve in curves:
        if curve.start_point is None:
            continue
        curve_count += 1
        x, y = _point_xy(curve.start_point)
        path_parts.append(f"M{_number(x)},{_number(y)}")
        for segment in curve.segments:
            segment_count += 1
            if segment.is_corner:
                ax, ay = _point_xy(segment.c)
                bx, by = _point_xy(segment.end_point)
                path_parts.append(
                    f"L{_number(ax)},{_number(ay)}L{_number(bx)},{_number(by)}"
                )
            else:
                a1x, a1y = _point_xy(segment.c1)
                a2x, a2y = _point_xy(segment.c2)
                bx, by = _point_xy(segment.end_point)
                path_parts.append(
                    "C"
                    f"{_number(a1x)},{_number(a1y)} "
                    f"{_number(a2x)},{_number(a2y)} "
                    f"{_number(bx)},{_number(by)}"
                )
        path_parts.append("Z")

    if curve_count == 0:
        raise ValueError("Potrace found no closed regions at the current threshold.")

    output_width = _clamp(common.output_width_mm, 1.0, 5000.0)
    output_height = output_width * height / width
    metadata = (
        f"engine=potrace; threshold={settings.threshold}; turd-size={settings.turd_size}; "
        f"turn-policy={settings.turn_policy}; alpha-max={settings.alpha_max}; "
        f"opticurve={bool(settings.optimize_curves)}; opttolerance={settings.optimize_tolerance}"
    )
    svg = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{output_width:.4f}mm" height="{output_height:.4f}mm" viewBox="0 0 {width} {height}">',
        f'<title>{escape(filename or "converted-image")}</title>',
        f'<metadata>{escape(metadata)}</metadata>',
        f'<path id="potrace-contours" fill="none" stroke="#000000" stroke-width="1" vector-effect="non-scaling-stroke" d="{"".join(path_parts)}"/>',
        '</svg>',
    ])
    return ConverterEngineResult(
        svg=svg,
        width=width,
        height=height,
        output_width_mm=output_width,
        output_height_mm=output_height,
        path_count=curve_count,
        point_count=max(curve_count, segment_count * 3),
        engine="potrace",
        engine_detail="Potrace smooth monochrome regions",
    )


def _quantize_rgba(image: Image.Image, max_colors: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")
    colors = max(2, min(256, int(max_colors)))
    quantized = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGBA")
    quantized.putalpha(alpha)
    return quantized


def _rewrite_pixels_svg(
    svg_text: str,
    filename: str,
    width: int,
    height: int,
    output_width: float,
    output_height: float,
    metadata: str,
) -> tuple[str, int, int]:
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        raise ValueError("Pixels2SVG returned malformed SVG data.") from exc

    root.set("width", f"{output_width:.4f}mm")
    root.set("height", f"{output_height:.4f}mm")
    root.set("viewBox", f"0 0 {width} {height}")
    root.set("preserveAspectRatio", "xMidYMid meet")

    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    title = ET.Element(f"{{{namespace}}}title")
    title.text = filename or "converted-image"
    meta = ET.Element(f"{{{namespace}}}metadata")
    meta.text = metadata
    root.insert(0, meta)
    root.insert(0, title)

    path_count = 0
    point_count = 0
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path":
            path_count += 1
            data = element.attrib.get("d", "")
            point_count += sum(data.count(command) for command in "MmLlHhVvCcSsQqTtAa")
        elif tag in {"polygon", "polyline"}:
            path_count += 1
            points = element.attrib.get("points", "").replace(",", " ").split()
            point_count += len(points) // 2
        elif tag == "rect":
            path_count += 1
            point_count += 4

    return ET.tostring(root, encoding="unicode"), path_count, point_count


def convert_pixels2svg(
    image: Image.Image,
    filename: str,
    common: CommonRasterSettings,
    settings: Pixels2SvgSettings,
) -> ConverterEngineResult:
    if _pixels2svg_convert is None:
        raise ValueError("Pixels2SVG is not installed. Rebuild HatchPlot with the GPL converter profile.")

    rgba = _prepare_rgba(image, common)
    if common.invert:
        alpha = rgba.getchannel("A")
        inverted = ImageOps.invert(rgba.convert("RGB")).convert("RGBA")
        inverted.putalpha(alpha)
        rgba = inverted
    if common.auto_contrast_cutoff > 0:
        alpha = rgba.getchannel("A")
        rgb = ImageOps.autocontrast(rgba.convert("RGB"), cutoff=_clamp(common.auto_contrast_cutoff, 0.0, 20.0))
        rgba = rgb.convert("RGBA")
        rgba.putalpha(alpha)

    rgba = _quantize_rgba(rgba, settings.max_colors)
    width, height = rgba.size
    output_width = _clamp(common.output_width_mm, 1.0, 5000.0)
    output_height = output_width * height / width

    input_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary:
            input_path = temporary.name
        rgba.save(input_path, format="PNG", optimize=False)
        svg_text = _pixels2svg_convert(
            input_path=input_path,
            output_path=None,
            group_by_color=bool(settings.group_by_color),
            color_tolerance=max(0, min(765, int(settings.color_tolerance))),
            remove_background=bool(settings.remove_background),
            background_tolerance=_clamp(settings.background_tolerance, 0.0, 50.0),
            maximal_non_bg_artifact_size=_clamp(settings.maximum_artifact_percent, 0.0, 100.0),
            as_string=True,
            pretty=False,
        )
    finally:
        if input_path:
            try:
                os.unlink(input_path)
            except OSError:
                pass

    if not isinstance(svg_text, str) or not svg_text.strip():
        raise ValueError("Pixels2SVG returned no SVG data.")
    if len(svg_text.encode("utf-8")) > settings.maximum_svg_bytes:
        raise ValueError("Pixels2SVG output is too large; lower trace resolution, reduce colors, or increase color tolerance.")

    metadata = (
        f"engine=pixels2svg; max-colors={settings.max_colors}; color-tolerance={settings.color_tolerance}; "
        f"remove-background={bool(settings.remove_background)}; group-by-color={bool(settings.group_by_color)}"
    )
    svg, path_count, point_count = _rewrite_pixels_svg(
        svg_text,
        filename,
        width,
        height,
        output_width,
        output_height,
        metadata,
    )
    if path_count == 0:
        raise ValueError("Pixels2SVG produced no vector regions.")
    if path_count > settings.maximum_paths:
        raise ValueError(
            f"Pixels2SVG produced {path_count:,} regions; lower trace resolution, reduce colors, or increase color tolerance."
        )

    return ConverterEngineResult(
        svg=svg,
        width=width,
        height=height,
        output_width_mm=output_width,
        output_height_mm=output_height,
        path_count=path_count,
        point_count=max(path_count, point_count),
        engine="pixels2svg",
        engine_detail="Pixels2SVG color-region polygons",
    )
