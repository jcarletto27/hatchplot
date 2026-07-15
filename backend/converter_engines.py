"""Optional raster-to-SVG converter engines.

The base HatchPlot image uses only the built-in linedraw-inspired engine.  The
Potrace, Pixels2SVG, and exact Inkscape adapters are activated by the explicit
GPL deployment profile (see ``requirements-gpl.txt`` and ``compose.gpl.yml``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from html import escape
import io
import math
import os
import re
import shutil
import subprocess
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
class InkscapeTraceSettings:
    scans: int = 8
    smooth: bool = True
    stack: bool = True
    remove_background: bool = True
    speckles: int = 2
    smooth_corners: float = 1.0
    optimize: float = 0.2
    maximum_svg_bytes: int = 25 * 1024 * 1024
    maximum_paths: int = 50000
    timeout_seconds: int = 120


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


@lru_cache(maxsize=1)
def _inkscape_runtime_status() -> dict[str, Any]:
    """Validate the optional Inkscape 1.4+ command-line trace runtime once."""
    executable_name = os.getenv("INKSCAPE_BIN", "inkscape").strip() or "inkscape"
    executable = shutil.which(executable_name)
    if executable is None:
        return {
            "available": False,
            "reason": f"{executable_name} was not found on PATH.",
            "version": "",
            "command": "",
        }

    xvfb_name = os.getenv("XVFB_RUN_BIN", "xvfb-run").strip() or "xvfb-run"
    xvfb = shutil.which(xvfb_name)
    if xvfb is None:
        return {
            "available": False,
            "reason": f"{xvfb_name} was not found; Inkscape tracing requires an isolated X server.",
            "version": "",
            "command": executable,
        }

    environment = os.environ.copy()
    environment.setdefault("LC_ALL", "C.UTF-8")
    try:
        version_result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "reason": f"Unable to execute Inkscape: {exc}",
            "version": "",
            "command": executable,
        }

    version_text = (version_result.stdout or version_result.stderr or "").strip()
    match = re.search(r"Inkscape\s+(\d+)\.(\d+)", version_text)
    if version_result.returncode != 0 or match is None:
        return {
            "available": False,
            "reason": f"Unable to determine a supported Inkscape version ({version_text or 'no version output'}).",
            "version": version_text,
            "command": executable,
        }
    version = (int(match.group(1)), int(match.group(2)))
    if version < (1, 4):
        return {
            "available": False,
            "reason": f"Inkscape 1.4 or newer is required for object-trace; found {version_text}.",
            "version": version_text,
            "command": executable,
        }

    try:
        action_result = subprocess.run(
            [executable, "--action-list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "reason": f"Unable to inspect Inkscape actions: {exc}",
            "version": version_text,
            "command": executable,
        }
    actions = action_result.stdout or ""
    if action_result.returncode != 0 or not re.search(r"(?m)^object-trace\s*:", actions):
        return {
            "available": False,
            "reason": "This Inkscape build does not expose the object-trace command-line action.",
            "version": version_text,
            "command": executable,
        }

    return {
        "available": True,
        "reason": "",
        "version": version_text,
        "command": executable,
        "xvfb": xvfb,
    }


def converter_engine_status() -> dict[str, dict[str, Any]]:
    """Return runtime availability and licensing metadata for each engine."""
    inkscape = _inkscape_runtime_status()
    return {
        "linedraw": {
            "available": True,
            "label": "Linedraw",
            "license": "MIT-derived implementation",
            "description": "Plotter-oriented open contours and tonal hatches.",
        },
        "inkscape": {
            "available": bool(inkscape.get("available")),
            "label": "Inkscape Trace Bitmap",
            "license": "GPL-2.0-or-later",
            "description": "Exact Inkscape 1.4+ color multi-scan bitmap tracing through its object-trace action.",
            "reason": str(inkscape.get("reason", "")),
            "version": str(inkscape.get("version", "")),
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


def _prepare_color(image: Image.Image, settings: CommonRasterSettings) -> Image.Image:
    """Apply HatchPlot's explicit raster preprocessing while preserving alpha."""
    rgba = _prepare_rgba(image, settings)
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")
    cutoff = _clamp(settings.auto_contrast_cutoff, 0.0, 20.0)
    if cutoff > 0:
        rgb = ImageOps.autocontrast(rgb, cutoff=cutoff)
    if settings.invert:
        rgb = ImageOps.invert(rgb)
    result = rgb.convert("RGBA")
    result.putalpha(alpha)
    return result


def _inkscape_bool(value: bool) -> str:
    return "true" if value else "false"


def _inkscape_trace_action(settings: InkscapeTraceSettings) -> str:
    scans = max(2, min(256, int(settings.scans)))
    speckles = max(0, min(100000, int(settings.speckles)))
    smooth_corners = _clamp(settings.smooth_corners, 0.0, 1.334)
    optimize = _clamp(settings.optimize, 0.0, 5.0)
    arguments = ",".join([
        str(scans),
        _inkscape_bool(settings.smooth),
        _inkscape_bool(settings.stack),
        _inkscape_bool(settings.remove_background),
        str(speckles),
        _number(smooth_corners),
        _number(optimize),
    ])
    return f"select-by-id:source-image;object-trace:{arguments};export-do"


def _make_inkscape_input_svg(width: int, height: int) -> str:
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<image id="source-image" x="0" y="0" width="{width}" height="{height}" xlink:href="source.png"/>',
        '</svg>',
    ])


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _remove_elements_by_tag(root: ET.Element, tag_name: str) -> int:
    removed = 0
    for parent in root.iter():
        for child in list(parent):
            if _local_name(child.tag) == tag_name:
                parent.remove(child)
                removed += 1
    return removed


def _rewrite_inkscape_svg(
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
        raise ValueError("Inkscape returned malformed SVG data.") from exc
    if _local_name(root.tag) != "svg":
        raise ValueError("Inkscape did not return an SVG document.")

    # Inkscape keeps the selected source bitmap beside the generated vectors.
    # HatchPlot exports vectors only, so remove all embedded raster nodes.
    _remove_elements_by_tag(root, "image")
    root.set("width", f"{output_width:.4f}mm")
    root.set("height", f"{output_height:.4f}mm")
    root.set("viewBox", f"0 0 {width} {height}")
    root.set("preserveAspectRatio", "xMidYMid meet")

    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    title = ET.Element(f"{{{namespace}}}title")
    title.text = filename or "converted-image"
    meta = ET.Element(f"{{{namespace}}}metadata")
    meta.text = metadata
    root.insert(0, meta)
    root.insert(0, title)

    path_count = 0
    point_count = 0
    for element in root.iter():
        tag = _local_name(element.tag)
        if tag == "path":
            path_count += 1
            data = element.attrib.get("d", "")
            point_count += sum(data.count(command) for command in "MmLlHhVvCcSsQqTtAa")
        elif tag in {"polygon", "polyline"}:
            path_count += 1
            points = element.attrib.get("points", "").replace(",", " ").split()
            point_count += len(points) // 2
        elif tag in {"rect", "circle", "ellipse"}:
            path_count += 1
            point_count += 4

    return ET.tostring(root, encoding="unicode"), path_count, point_count


def convert_inkscape(
    image: Image.Image,
    filename: str,
    common: CommonRasterSettings,
    settings: InkscapeTraceSettings,
) -> ConverterEngineResult:
    runtime = _inkscape_runtime_status()
    if not runtime.get("available"):
        raise ValueError(
            "Inkscape Trace Bitmap is unavailable: "
            f"{runtime.get('reason') or 'install the GPL converter profile.'}"
        )

    rgba = _prepare_color(image, common)
    width, height = rgba.size
    output_width = _clamp(common.output_width_mm, 1.0, 5000.0)
    output_height = output_width * height / width
    timeout = max(10, min(600, int(settings.timeout_seconds)))

    with tempfile.TemporaryDirectory(prefix="hatchplot-inkscape-") as temporary_directory:
        input_path = os.path.join(temporary_directory, "input.svg")
        source_path = os.path.join(temporary_directory, "source.png")
        output_path = os.path.join(temporary_directory, "traced.svg")
        home_path = os.path.join(temporary_directory, "home")
        os.makedirs(home_path, mode=0o700, exist_ok=True)
        rgba.save(source_path, format="PNG", optimize=False)
        with open(input_path, "w", encoding="utf-8") as handle:
            handle.write(_make_inkscape_input_svg(width, height))

        environment = os.environ.copy()
        environment.update({
            "HOME": home_path,
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
        })
        command = [
            str(runtime["xvfb"]),
            "-a",
            str(runtime["command"]),
            input_path,
            f"--export-filename={output_path}",
            "--export-plain-svg",
            f"--actions={_inkscape_trace_action(settings)}",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Inkscape tracing exceeded the {timeout}-second timeout.") from exc
        except OSError as exc:
            raise ValueError(f"Unable to execute Inkscape: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            message = detail[-1][:500] if detail else f"process exited with status {completed.returncode}"
            raise ValueError(f"Inkscape tracing failed: {message}")
        try:
            output_size = os.path.getsize(output_path)
        except OSError as exc:
            raise ValueError("Inkscape completed without producing an SVG trace.") from exc
        if output_size > settings.maximum_svg_bytes:
            raise ValueError(
                "Inkscape output is too large; lower trace resolution or reduce the number of scans."
            )
        with open(output_path, "r", encoding="utf-8") as handle:
            svg_text = handle.read(settings.maximum_svg_bytes + 1)

    metadata = (
        f"engine=inkscape; version={runtime.get('version', '')}; mode=color-multiscan; "
        f"scans={settings.scans}; smooth={bool(settings.smooth)}; stack={bool(settings.stack)}; "
        f"remove-background={bool(settings.remove_background)}; speckles={settings.speckles}; "
        f"smooth-corners={settings.smooth_corners}; optimize={settings.optimize}"
    )
    svg, path_count, point_count = _rewrite_inkscape_svg(
        svg_text,
        filename,
        width,
        height,
        output_width,
        output_height,
        metadata,
    )
    if path_count == 0:
        raise ValueError("Inkscape produced no vector paths at the current settings.")
    if path_count > settings.maximum_paths:
        raise ValueError(
            f"Inkscape produced {path_count:,} paths; lower trace resolution or reduce the number of scans."
        )

    version_text = str(runtime.get("version", "Inkscape 1.4+"))
    return ConverterEngineResult(
        svg=svg,
        width=width,
        height=height,
        output_width_mm=output_width,
        output_height_mm=output_height,
        path_count=path_count,
        point_count=max(path_count, point_count),
        engine="inkscape",
        engine_detail=f"{version_text} color multi-scan Trace Bitmap",
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

    rgba = _prepare_color(image, common)
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
