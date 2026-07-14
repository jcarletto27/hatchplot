from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import threading
import time
from multiprocessing import Manager
import traceback
import uuid
from concurrent.futures import CancelledError, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from typing import Any, Iterable, MutableMapping

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from shapely import affinity
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    box,
)
from shapely.validation import make_valid
from svgelements import Color, Path, SVG, Shape

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(processName)s %(name)s: %(message)s",
)
logger = logging.getLogger("hatchplot")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_BRIGHTNESS_MAP_BYTES = int(os.getenv("MAX_BRIGHTNESS_MAP_BYTES", str(20 * 1024 * 1024)))
MAX_BRIGHTNESS_MAP_PIXELS = int(os.getenv("MAX_BRIGHTNESS_MAP_PIXELS", "16777216"))
MAX_HATCH_PATHS = int(os.getenv("MAX_HATCH_PATHS", "50000"))
MAX_TOOLPATH_POINTS = int(os.getenv("MAX_TOOLPATH_POINTS", "500000"))
MAX_LIVE_PREVIEW_POINTS = int(os.getenv("MAX_LIVE_PREVIEW_POINTS", "20000"))
LIVE_PREVIEW_CHUNK_POINTS = int(os.getenv("LIVE_PREVIEW_CHUNK_POINTS", "1600"))
DEFAULT_BRIGHTNESS_CUTOFF = float(os.getenv("DEFAULT_BRIGHTNESS_CUTOFF", "0.025"))
MAX_RETAINED_RESULTS = max(1, int(os.getenv("MAX_RETAINED_RESULTS", "4")))
MAX_PENDING_JOBS = int(os.getenv("MAX_PENDING_JOBS", "4"))
JOB_WORKERS = max(1, int(os.getenv("JOB_WORKERS", "1")))
JOB_TTL_SECONDS = max(60, int(os.getenv("JOB_TTL_SECONDS", "1800")))
ACCELERATION_BACKEND = os.getenv("ACCELERATION_BACKEND", "auto").strip().lower()
if ACCELERATION_BACKEND not in {"auto", "cpu", "cuda"}:
    logger.warning("Unknown ACCELERATION_BACKEND=%s; falling back to auto", ACCELERATION_BACKEND)
    ACCELERATION_BACKEND = "auto"

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


class GenerationError(ValueError):
    """An SVG or parameter problem that can be shown safely to the user."""


class GenerationLimitError(GenerationError):
    """The requested toolpath is too large to generate safely."""


def _new_executor() -> ProcessPoolExecutor:
    logger.info("Starting geometry worker pool with %d worker(s)", JOB_WORKERS)
    return ProcessPoolExecutor(max_workers=JOB_WORKERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.progress_manager = Manager()
    app.state.executor = _new_executor()
    try:
        yield
    finally:
        app.state.executor.shutdown(wait=False, cancel_futures=True)
        app.state.progress_manager.shutdown()


app = FastAPI(title="Hatch Plotter API", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_id=%s method=%s path=%s unhandled_exception",
            request_id,
            request.method,
            request.url.path,
        )
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "Invalid request",
            "detail": exc.errors(),
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", None)
    logger.exception("request_id=%s internal_server_error", request_id)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "detail": "The backend failed while processing the request. Check the API log using the request ID.",
            "request_id": request_id,
        },
    )


def get_luminance(color: Color | None) -> float:
    if color is None or color == "none":
        return 1.0
    red = float(getattr(color, "red", 0) or 0)
    green = float(getattr(color, "green", 0) or 0)
    blue = float(getattr(color, "blue", 0) or 0)
    return max(0.0, min(1.0, ((0.299 * red) + (0.587 * green) + (0.114 * blue)) / 255.0))


def create_hatch_lines(bounds: tuple[float, float, float, float], spacing: float, angle_deg: float = 45.0) -> MultiLineString:
    minx, miny, maxx, maxy = bounds
    width = maxx - minx
    height = maxy - miny
    if width <= 0 or height <= 0:
        return MultiLineString([])

    spacing = max(0.05, float(spacing))
    center_x = (minx + maxx) / 2.0
    center_y = (miny + maxy) / 2.0
    radius = math.hypot(width, height) + spacing
    line_count = int(math.ceil((2.0 * radius) / spacing)) + 1

    lines = [
        LineString(
            [
                (center_x - radius, center_y - radius + index * spacing),
                (center_x + radius, center_y - radius + index * spacing),
            ]
        )
        for index in range(line_count)
    ]
    grid = MultiLineString(lines)
    return affinity.rotate(grid, angle_deg, origin=(center_x, center_y), use_radians=False)


def _iter_polygons(geometry: Any) -> Iterable[Polygon]:
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from _iter_polygons(item)


def _iter_lines(geometry: Any) -> Iterable[LineString]:
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from _iter_lines(item)


def path_to_shapely(element: Path) -> Polygon | MultiPolygon | None:
    polygons: list[Polygon] = []
    for raw_subpath in element.as_subpaths():
        subpath = Path(raw_subpath)
        length = float(subpath.length())
        if not math.isfinite(length) or length < 1.0:
            continue

        point_count = max(10, min(300, int(math.ceil(length / 1.5))))
        points = []
        for index in range(point_count + 1):
            point = subpath.point(index / point_count)
            if math.isfinite(point.x) and math.isfinite(point.y):
                points.append((float(point.x), float(point.y)))

        if len(points) < 3:
            continue

        polygon: Any = Polygon(points)
        if not polygon.is_valid:
            polygon = make_valid(polygon)
        polygons.extend(_iter_polygons(polygon))

    if not polygons:
        return None

    polygons.sort(key=lambda item: item.area, reverse=True)
    result: Any = polygons[0]
    for polygon in polygons[1:]:
        # Preserve the original SVG subpath behavior: nested paths become holes;
        # disjoint paths are combined.
        if result.contains(polygon):
            result = result.difference(polygon)
        else:
            result = result.union(polygon)

    if not result.is_valid:
        result = make_valid(result)
    polygon_parts = list(_iter_polygons(result))
    if not polygon_parts:
        return None
    if len(polygon_parts) == 1:
        return polygon_parts[0]
    return MultiPolygon(polygon_parts)


def validate_generation_params(params: dict[str, Any]) -> None:
    for key in ("bedX", "bedY"):
        value = float(params[key])
        if not math.isfinite(value) or not 1.0 <= value <= 5000.0:
            raise GenerationError(f"{key} must be between 1 and 5000 mm.")

    scale = float(params["svgScale"])
    if not math.isfinite(scale) or not 0.1 <= scale <= 1000.0:
        raise GenerationError("svgScale must be between 0.1 and 1000 percent.")

    if params.get("svgScaleMode", "fit-relative") not in {"absolute", "fit-relative"}:
        raise GenerationError("svgScaleMode must be either 'absolute' or 'fit-relative'.")

    pen_thickness = float(params.get("penThickness", 0.5))
    if not math.isfinite(pen_thickness) or not 0.05 <= pen_thickness <= 10.0:
        raise GenerationError("penThickness must be between 0.05 and 10 mm.")

    density_fudge = float(params.get("densityFudge", 0.0))
    if not math.isfinite(density_fudge) or not -0.5 <= density_fudge <= 0.5:
        raise GenerationError("densityFudge must be between -0.5 and 0.5.")

    brightness_cutoff = float(params.get("brightnessCutoff", DEFAULT_BRIGHTNESS_CUTOFF))
    if not math.isfinite(brightness_cutoff) or not 0.0 <= brightness_cutoff <= 1.0:
        raise GenerationError("brightnessCutoff must be between 0.0 and 1.0.")

    if params.get("patternLayout", "linear") not in {"linear", "spiral", "concentric", "radial"}:
        raise GenerationError("patternLayout must be linear, spiral, concentric, or radial.")
    if params.get("waveform", "zigzag") not in {"zigzag", "sawtooth", "sine", "ekg", "straight"}:
        raise GenerationError("waveform must be zigzag, sawtooth, sine, ekg, or straight.")
    if params.get("brightnessModulation", "both") not in {"both", "amplitude", "frequency", "none"}:
        raise GenerationError("brightnessModulation must be both, amplitude, frequency, or none.")
    for key, minimum, maximum in (
        ("patternSpacing", 0.05, 500.0),
        ("waveAmplitude", 0.0, 500.0),
        ("waveLength", 0.05, 5000.0),
    ):
        value = float(params.get(key, minimum))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise GenerationError(f"{key} must be between {minimum} and {maximum} mm.")
    for key in ("patternCenterX", "patternCenterY", "patternAngle"):
        if not math.isfinite(float(params.get(key, 0.0))):
            raise GenerationError(f"{key} must be a finite number.")

    for key in ("svgRotate", "svgPosX", "svgPosY"):
        if not math.isfinite(float(params[key])):
            raise GenerationError(f"{key} must be a finite number.")

    for key in ("xyFeedRate", "zPlungeRate"):
        value = int(params[key])
        if not 1 <= value <= 1_000_000:
            raise GenerationError(f"{key} must be between 1 and 1,000,000.")

    if params["zMode"] not in {"stepper", "servo"}:
        raise GenerationError("zMode must be either 'stepper' or 'servo'.")

    for key in ("zUp", "zDown"):
        try:
            value = float(params[key])
        except (TypeError, ValueError) as exc:
            raise GenerationError(f"{key} must be numeric.") from exc
        if not math.isfinite(value):
            raise GenerationError(f"{key} must be a finite number.")


def _set_progress(
    progress: MutableMapping[str, Any] | None,
    *,
    phase: str,
    percent: float,
    completed: int | None = None,
    total: int | None = None,
    detail: str | None = None,
    compute_backend: str | None = None,
    force_eta_zero: bool = False,
) -> None:
    if progress is None:
        return
    try:
        now = time.time()
        started_at = float(progress.get("started_at") or now)
        bounded_percent = max(0.0, min(100.0, float(percent)))
        elapsed = max(0.0, now - started_at)
        eta_seconds: float | None = None
        if force_eta_zero or bounded_percent >= 100.0:
            eta_seconds = 0.0
        elif bounded_percent >= 1.0 and elapsed >= 0.25:
            eta_seconds = max(0.0, (elapsed / (bounded_percent / 100.0)) - elapsed)

        update: dict[str, Any] = {
            "phase": phase,
            "percent": round(bounded_percent, 1),
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": None if eta_seconds is None else round(eta_seconds, 1),
            "updated_at": now,
        }
        if completed is not None:
            update["completed"] = int(completed)
        if total is not None:
            update["total"] = int(total)
        if detail is not None:
            update["detail"] = detail
        if compute_backend is not None:
            update["compute_backend"] = compute_backend
        progress.update(update)
    except Exception:
        # Progress reporting must never be allowed to fail generation.
        logger.debug("Unable to update generation progress", exc_info=True)


def _check_cancel_requested(progress: MutableMapping[str, Any] | None) -> None:
    if progress is None:
        return
    try:
        if bool(progress.get("cancel_requested")):
            _set_progress(
                progress,
                phase="cancelled",
                percent=float(progress.get("percent") or 0.0),
                detail="Generation was cancelled.",
            )
            raise CancelledError("Generation was cancelled.")
    except CancelledError:
        raise
    except Exception:
        logger.debug("Unable to read generation cancellation state", exc_info=True)


def _build_darkness_grid(
    rgba: Image.Image,
    x_values: list[float],
    row_positions: list[float],
    bed_x: float,
    bed_y: float,
    progress: MutableMapping[str, Any] | None,
) -> tuple[np.ndarray, str]:
    """Sample the raster into the exact machine-coordinate grid.

    CUDA accelerates the dense pixel indexing and luminance math. Path chaining and
    G-code compilation remain CPU tasks because they are branch-heavy and sequential.
    """
    _check_cancel_requested(progress)
    source = np.asarray(rgba, dtype=np.uint8)
    x_indices = np.rint((np.asarray(x_values, dtype=np.float64) / bed_x) * (rgba.width - 1)).astype(np.int32)
    y_indices = np.rint((np.asarray(row_positions, dtype=np.float64) / bed_y) * (rgba.height - 1)).astype(np.int32)
    x_indices = np.clip(x_indices, 0, rgba.width - 1)
    y_indices = np.clip(y_indices, 0, rgba.height - 1)

    requested = ACCELERATION_BACKEND
    if requested in {"auto", "cuda"}:
        try:
            import cupy as cp  # type: ignore

            if cp.cuda.runtime.getDeviceCount() < 1:
                raise RuntimeError("no CUDA devices were detected")
            _set_progress(
                progress,
                phase="gpu-sampling",
                percent=7.0,
                detail="Sampling image brightness on the CUDA GPU...",
                compute_backend="cuda",
            )
            gpu_source = cp.asarray(source)
            gpu_x = cp.asarray(x_indices)
            gpu_y = cp.asarray(y_indices)
            samples = gpu_source[gpu_y[:, None], gpu_x[None, :]]
            samples_f = samples.astype(cp.float64)
            luminance = (0.299 * samples_f[..., 0] + 0.587 * samples_f[..., 1] + 0.114 * samples_f[..., 2]) / 255.0
            darkness = (1.0 - luminance) * (samples_f[..., 3] / 255.0)
            result = cp.asnumpy(cp.clip(darkness, 0.0, 1.0)).astype(np.float64, copy=False)
            _check_cancel_requested(progress)
            # Release large allocations before the sequential path-building phase.
            del samples, samples_f, luminance, darkness, gpu_source, gpu_x, gpu_y
            cp.get_default_memory_pool().free_all_blocks()
            return result, "cuda"
        except Exception as exc:
            if requested == "cuda":
                raise GenerationError(
                    "CUDA acceleration was requested but is unavailable. "
                    f"Install the GPU image and NVIDIA Container Toolkit, or set ACCELERATION_BACKEND=auto/cpu. ({exc})"
                ) from exc
            logger.info("CUDA acceleration unavailable; using NumPy CPU path: %s", exc)

    _set_progress(
        progress,
        phase="cpu-sampling",
        percent=7.0,
        detail="Sampling image brightness with NumPy...",
        compute_backend="numpy-cpu",
    )
    _check_cancel_requested(progress)
    samples = source[y_indices[:, None], x_indices[None, :]].astype(np.float64)
    luminance = (0.299 * samples[..., 0] + 0.587 * samples[..., 1] + 0.114 * samples[..., 2]) / 255.0
    darkness = (1.0 - luminance) * (samples[..., 3] / 255.0)
    result = np.clip(darkness, 0.0, 1.0).astype(np.float64, copy=False)
    _check_cancel_requested(progress)
    return result, "numpy-cpu"


def _sample_darkness(pixels: Any, width: int, height: int, bed_x: float, bed_y: float, x: float, y: float) -> float:
    if x < 0.0 or y < 0.0 or x > bed_x or y > bed_y:
        return 0.0
    px = min(width - 1, max(0, int(round((x / bed_x) * (width - 1)))))
    py = min(height - 1, max(0, int(round((y / bed_y) * (height - 1)))))
    red, green, blue, alpha = pixels[px, py]
    luminance = ((0.299 * red) + (0.587 * green) + (0.114 * blue)) / 255.0
    return max(0.0, min(1.0, (1.0 - luminance) * (alpha / 255.0)))


def _triangle_wave(phase: float) -> float:
    # Continuous triangle wave in the range [-1, 1].
    wrapped = phase % 1.0
    return 1.0 - (4.0 * abs(wrapped - 0.5))


def _connector_is_drawable(
    pixels: Any,
    width: int,
    height: int,
    bed_x: float,
    bed_y: float,
    start: tuple[float, float],
    end: tuple[float, float],
    pen_thickness: float,
    brightness_cutoff: float,
) -> bool:
    distance = math.dist(start, end)
    if distance > pen_thickness * 3.5:
        return False
    samples = max(3, int(math.ceil(distance / max(pen_thickness * 0.35, 0.05))))
    visible = 0
    total_darkness = 0.0
    for index in range(samples + 1):
        ratio = index / samples
        x = start[0] + ((end[0] - start[0]) * ratio)
        y = start[1] + ((end[1] - start[1]) * ratio)
        darkness = _sample_darkness(pixels, width, height, bed_x, bed_y, x, y)
        total_darkness += darkness
        if darkness >= brightness_cutoff:
            visible += 1
    return visible / (samples + 1) >= 0.7 and total_darkness / (samples + 1) >= brightness_cutoff


def _decimate_preview_path(path: list[list[float]], maximum_points: int = 240) -> list[list[float]]:
    if len(path) <= maximum_points:
        return path
    step = max(1, int(math.ceil((len(path) - 1) / (maximum_points - 1))))
    reduced = path[::step]
    if reduced[-1] != path[-1]:
        reduced.append(path[-1])
    return reduced


def _publish_live_preview(
    preview_queue: Any | None,
    paths: list[list[list[float]]],
    preview_state: dict[str, int],
) -> None:
    if preview_queue is None or not paths:
        return

    remaining = MAX_LIVE_PREVIEW_POINTS - int(preview_state.get("points", 0))
    if remaining < 2:
        return

    chunk_paths: list[list[list[float]]] = []
    chunk_points = 0

    def flush_chunk() -> None:
        nonlocal chunk_paths, chunk_points
        if not chunk_paths:
            return
        preview_queue.append({
            "sequence": int(preview_state.get("chunks", 0)),
            "paths": chunk_paths,
            "points": chunk_points,
        })
        preview_state["points"] = int(preview_state.get("points", 0)) + chunk_points
        preview_state["chunks"] = int(preview_state.get("chunks", 0)) + 1
        chunk_paths = []
        chunk_points = 0

    for path in paths:
        remaining = MAX_LIVE_PREVIEW_POINTS - int(preview_state.get("points", 0)) - chunk_points
        if remaining < 2:
            break
        reduced = _decimate_preview_path(path)
        if len(reduced) < 2:
            continue
        if len(reduced) > remaining:
            reduced = _decimate_preview_path(reduced, remaining)
        if chunk_points and chunk_points + len(reduced) > LIVE_PREVIEW_CHUNK_POINTS:
            flush_chunk()
        chunk_paths.append(reduced)
        chunk_points += len(reduced)
        if chunk_points >= LIVE_PREVIEW_CHUNK_POINTS:
            flush_chunk()

    flush_chunk()



def _waveform_value(waveform: str, phase: float) -> float:
    wrapped = phase % 1.0
    if waveform == "straight":
        return 0.0
    if waveform == "sine":
        return math.sin(wrapped * math.tau)
    if waveform == "sawtooth":
        return (2.0 * wrapped) - 1.0
    if waveform == "ekg":
        # Stylized P-QRS-T cycle with a mostly flat baseline.
        keyframes = (
            (0.00, 0.00), (0.12, 0.00), (0.18, 0.18), (0.24, 0.00),
            (0.34, 0.00), (0.39, -0.22), (0.43, 1.00), (0.47, -0.52),
            (0.53, 0.00), (0.68, 0.00), (0.78, 0.32), (0.88, 0.00),
            (1.00, 0.00),
        )
        for index in range(len(keyframes) - 1):
            x0, y0 = keyframes[index]
            x1, y1 = keyframes[index + 1]
            if x0 <= wrapped <= x1:
                ratio = (wrapped - x0) / max(x1 - x0, 1e-9)
                return y0 + ((y1 - y0) * ratio)
        return 0.0
    return _triangle_wave(wrapped)


def _sample_segment(start: tuple[float, float], end: tuple[float, float], step: float) -> list[tuple[float, float]]:
    distance = math.dist(start, end)
    count = max(1, int(math.ceil(distance / max(step, 0.01))))
    return [
        (
            start[0] + ((end[0] - start[0]) * (index / count)),
            start[1] + ((end[1] - start[1]) * (index / count)),
        )
        for index in range(count + 1)
    ]


def _build_pattern_carriers(
    layout: str,
    bed_x: float,
    bed_y: float,
    center_x: float,
    center_y: float,
    spacing: float,
    sample_step: float,
    angle_degrees: float,
    clockwise: bool,
) -> list[list[tuple[float, float]]]:
    center = (center_x, center_y)
    corners = ((0.0, 0.0), (bed_x, 0.0), (bed_x, bed_y), (0.0, bed_y))
    maximum_radius = max(math.dist(center, corner) for corner in corners) + spacing
    carriers: list[list[tuple[float, float]]] = []

    if layout == "spiral":
        direction = -1.0 if clockwise else 1.0
        theta = 0.0
        points: list[tuple[float, float]] = []
        radial_per_radian = spacing / math.tau
        maximum_theta = maximum_radius / max(radial_per_radian, 1e-9)
        while theta <= maximum_theta:
            radius = radial_per_radian * theta
            x = center_x + (radius * math.cos(direction * theta))
            y = center_y + (radius * math.sin(direction * theta))
            points.append((x, y))
            arc_derivative = math.hypot(radial_per_radian, radius)
            theta += sample_step / max(arc_derivative, sample_step)
        if len(points) >= 2:
            carriers.append(points)
        return carriers

    if layout == "concentric":
        radius = max(spacing * 0.5, sample_step)
        ring_index = 0
        while radius <= maximum_radius:
            count = max(12, int(math.ceil((math.tau * radius) / sample_step)))
            direction = -1.0 if clockwise ^ (ring_index % 2 == 1) else 1.0
            points = [
                (
                    center_x + (radius * math.cos(direction * math.tau * index / count)),
                    center_y + (radius * math.sin(direction * math.tau * index / count)),
                )
                for index in range(count + 1)
            ]
            carriers.append(points)
            radius += spacing
            ring_index += 1
        return carriers

    if layout == "radial":
        circumference = max(math.tau * maximum_radius, spacing)
        spoke_count = max(3, int(math.ceil(circumference / spacing)))
        direction = -1.0 if clockwise else 1.0
        start_angle = math.radians(angle_degrees)
        for index in range(spoke_count):
            angle = start_angle + (direction * math.tau * index / spoke_count)
            outer = (
                center_x + (maximum_radius * math.cos(angle)),
                center_y + (maximum_radius * math.sin(angle)),
            )
            points = _sample_segment(center, outer, sample_step)
            if index % 2 == 1:
                points.reverse()
            carriers.append(points)
        return carriers

    # Linear parallel carriers. The selected center acts as the pattern origin.
    angle = math.radians(angle_degrees)
    tangent = (math.cos(angle), math.sin(angle))
    normal = (-tangent[1], tangent[0])
    diagonal = math.hypot(bed_x, bed_y) * 1.25
    offset = -diagonal
    line_index = 0
    while offset <= diagonal:
        line_center = (center_x + (normal[0] * offset), center_y + (normal[1] * offset))
        start = (line_center[0] - (tangent[0] * diagonal), line_center[1] - (tangent[1] * diagonal))
        end = (line_center[0] + (tangent[0] * diagonal), line_center[1] + (tangent[1] * diagonal))
        points = _sample_segment(start, end, sample_step)
        if line_index % 2 == 1:
            points.reverse()
        carriers.append(points)
        offset += spacing
        line_index += 1
    return carriers


def _sample_carrier_darkness(
    rgba: Image.Image,
    carriers: list[list[tuple[float, float]]],
    bed_x: float,
    bed_y: float,
    progress: MutableMapping[str, Any] | None,
) -> tuple[list[np.ndarray], str]:
    lengths = [len(carrier) for carrier in carriers]
    flat_points = [point for carrier in carriers for point in carrier]
    if not flat_points:
        return [], "numpy-cpu"

    source = np.asarray(rgba, dtype=np.uint8)
    coordinates = np.asarray(flat_points, dtype=np.float64)
    x_indices = np.rint((coordinates[:, 0] / bed_x) * (rgba.width - 1)).astype(np.int32)
    y_indices = np.rint((coordinates[:, 1] / bed_y) * (rgba.height - 1)).astype(np.int32)
    inside = (
        (coordinates[:, 0] >= 0.0) & (coordinates[:, 0] <= bed_x) &
        (coordinates[:, 1] >= 0.0) & (coordinates[:, 1] <= bed_y)
    )
    x_indices = np.clip(x_indices, 0, rgba.width - 1)
    y_indices = np.clip(y_indices, 0, rgba.height - 1)

    requested = ACCELERATION_BACKEND
    backend = "numpy-cpu"
    darkness: np.ndarray
    if requested in {"auto", "cuda"}:
        try:
            import cupy as cp  # type: ignore
            if cp.cuda.runtime.getDeviceCount() < 1:
                raise RuntimeError("no CUDA devices were detected")
            _set_progress(progress, phase="gpu-sampling", percent=7.0, detail="Sampling pattern brightness on the CUDA GPU...", compute_backend="cuda")
            gpu_source = cp.asarray(source)
            samples = gpu_source[cp.asarray(y_indices), cp.asarray(x_indices)].astype(cp.float64)
            luminance = (0.299 * samples[:, 0] + 0.587 * samples[:, 1] + 0.114 * samples[:, 2]) / 255.0
            gpu_darkness = (1.0 - luminance) * (samples[:, 3] / 255.0)
            darkness = cp.asnumpy(cp.clip(gpu_darkness, 0.0, 1.0))
            del gpu_source, samples, luminance, gpu_darkness
            cp.get_default_memory_pool().free_all_blocks()
            backend = "cuda"
        except Exception as exc:
            if requested == "cuda":
                raise GenerationError(f"CUDA acceleration was requested but is unavailable: {exc}") from exc
            logger.info("CUDA pattern sampling unavailable; using NumPy CPU path: %s", exc)
            samples = source[y_indices, x_indices].astype(np.float64)
            luminance = (0.299 * samples[:, 0] + 0.587 * samples[:, 1] + 0.114 * samples[:, 2]) / 255.0
            darkness = np.clip((1.0 - luminance) * (samples[:, 3] / 255.0), 0.0, 1.0)
    else:
        _set_progress(progress, phase="cpu-sampling", percent=7.0, detail="Sampling pattern brightness with NumPy...", compute_backend="numpy-cpu")
        samples = source[y_indices, x_indices].astype(np.float64)
        luminance = (0.299 * samples[:, 0] + 0.587 * samples[:, 1] + 0.114 * samples[:, 2]) / 255.0
        darkness = np.clip((1.0 - luminance) * (samples[:, 3] / 255.0), 0.0, 1.0)
    darkness[~inside] = 0.0

    result: list[np.ndarray] = []
    cursor = 0
    for length in lengths:
        result.append(darkness[cursor:cursor + length])
        cursor += length
    return result, backend


def _generate_brightness_paths(
    brightness_map: bytes,
    params: dict[str, Any],
    progress: MutableMapping[str, Any] | None = None,
    preview_queue: Any | None = None,
) -> tuple[list[list[list[float]]], dict[str, Any]]:
    bed_x = float(params["bedX"])
    bed_y = float(params["bedY"])
    pen_thickness = float(params.get("penThickness", 0.5))
    density_fudge = float(params.get("densityFudge", 0.0))
    brightness_cutoff = float(params.get("brightnessCutoff", DEFAULT_BRIGHTNESS_CUTOFF))
    layout = str(params.get("patternLayout", "linear"))
    waveform = str(params.get("waveform", "zigzag"))
    center_x = float(params.get("patternCenterX", bed_x / 2.0))
    center_y = float(params.get("patternCenterY", bed_y / 2.0))
    angle_degrees = float(params.get("patternAngle", 0.0))
    clockwise = bool(params.get("patternClockwise", True))
    brightness_mode = str(params.get("brightnessModulation", "both"))
    density_scale = 1.0 - (density_fudge * 0.8)
    layout_spacing = max(pen_thickness, float(params.get("patternSpacing", pen_thickness * 1.55)) * density_scale)
    wave_amplitude = max(0.0, float(params.get("waveAmplitude", layout_spacing * 0.42)))
    wave_length = max(pen_thickness * 2.0, float(params.get("waveLength", pen_thickness * 6.0)) * density_scale)

    _set_progress(progress, phase="decoding", percent=2.0, detail="Decoding the browser brightness map...")
    _check_cancel_requested(progress)
    try:
        image = Image.open(io.BytesIO(brightness_map))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise GenerationError(f"Unable to read the generated brightness map: {exc}") from exc
    if image.width * image.height > MAX_BRIGHTNESS_MAP_PIXELS:
        raise GenerationLimitError(f"The brightness map contains more than {MAX_BRIGHTNESS_MAP_PIXELS:,} pixels.")

    rgba = image.convert("RGBA")
    pixel_mm = max(bed_x / max(1, rgba.width - 1), bed_y / max(1, rgba.height - 1))
    sample_step = max(pen_thickness * 0.5, pixel_mm, 0.05)
    carriers = _build_pattern_carriers(
        layout, bed_x, bed_y, center_x, center_y, layout_spacing,
        sample_step, angle_degrees, clockwise,
    )
    estimated_points = sum(len(carrier) for carrier in carriers)
    if estimated_points > MAX_TOOLPATH_POINTS * 2:
        scale = estimated_points / max(MAX_TOOLPATH_POINTS * 1.5, 1)
        sample_step *= scale
        carriers = _build_pattern_carriers(
            layout, bed_x, bed_y, center_x, center_y, layout_spacing,
            sample_step, angle_degrees, clockwise,
        )

    darkness_sets, compute_backend = _sample_carrier_darkness(rgba, carriers, bed_x, bed_y, progress)
    total_carriers = len(carriers)
    _set_progress(
        progress, phase="path-building", percent=10.0, completed=0, total=total_carriers,
        detail=f"Building {layout} {waveform} paths...", compute_backend=compute_backend,
    )

    preview_state = {"points": 0, "chunks": 0}
    completed_paths: list[list[list[float]]] = []
    preview_pending: list[list[list[float]]] = []
    sampled_points = 0
    progress_stride = max(1, total_carriers // 150)

    for carrier_index, (carrier, darkness_values) in enumerate(zip(carriers, darkness_sets, strict=True)):
        if carrier_index % progress_stride == 0 or carrier_index + 1 == total_carriers:
            _check_cancel_requested(progress)
        current_run: list[list[float]] = []
        phase = 0.0
        previous_point: tuple[float, float] | None = None

        for point_index, (point, raw_darkness) in enumerate(zip(carrier, darkness_values, strict=True)):
            darkness = max(0.0, min(1.0, float(raw_darkness) * (1.0 + density_fudge)))
            sampled_points += 1
            if darkness < brightness_cutoff:
                if len(current_run) >= 2:
                    completed_paths.append(current_run)
                    preview_pending.append(current_run)
                current_run = []
                previous_point = None
                phase = 0.0
                continue

            if previous_point is not None:
                distance = math.dist(previous_point, point)
                local_wave_length = wave_length
                if brightness_mode in {"frequency", "both"}:
                    local_wave_length = max(pen_thickness * 2.0, wave_length * (1.5 - darkness))
                phase += distance / max(local_wave_length, 0.001)
            previous_point = point

            before = carrier[max(0, point_index - 1)]
            after = carrier[min(len(carrier) - 1, point_index + 1)]
            tangent_x = after[0] - before[0]
            tangent_y = after[1] - before[1]
            tangent_length = math.hypot(tangent_x, tangent_y) or 1.0
            normal_x = -tangent_y / tangent_length
            normal_y = tangent_x / tangent_length
            local_amplitude = wave_amplitude * (darkness if brightness_mode in {"amplitude", "both"} else 1.0)
            offset = _waveform_value(waveform, phase) * local_amplitude
            x = point[0] + (normal_x * offset)
            y = point[1] + (normal_y * offset)
            if not (0.0 <= x <= bed_x and 0.0 <= y <= bed_y):
                if len(current_run) >= 2:
                    completed_paths.append(current_run)
                    preview_pending.append(current_run)
                current_run = []
                continue
            current_run.append([round(x, 4), round(y, 4)])

        if len(current_run) >= 2:
            completed_paths.append(current_run)
            preview_pending.append(current_run)

        if sum(len(path) for path in preview_pending) >= LIVE_PREVIEW_CHUNK_POINTS:
            _publish_live_preview(preview_queue, preview_pending, preview_state)
            preview_pending = []
        if len(completed_paths) > MAX_HATCH_PATHS:
            raise GenerationLimitError(f"The pattern generated more than {MAX_HATCH_PATHS:,} disconnected paths.")
        if sum(len(path) for path in completed_paths) > MAX_TOOLPATH_POINTS:
            raise GenerationLimitError(f"The pattern exceeded {MAX_TOOLPATH_POINTS:,} toolpath points.")

        if carrier_index % progress_stride == 0 or carrier_index + 1 == total_carriers:
            fraction = (carrier_index + 1) / max(total_carriers, 1)
            _set_progress(
                progress, phase="path-building", percent=10.0 + (fraction * 80.0),
                completed=carrier_index + 1, total=total_carriers,
                detail=f"Building carrier {carrier_index + 1:,} of {total_carriers:,}...",
                compute_backend=compute_backend,
            )

    _publish_live_preview(preview_queue, preview_pending, preview_state)
    completed_paths = [path for path in completed_paths if len(path) >= 2]
    if not completed_paths:
        raise GenerationError("The transformed SVG contains no pixels above the brightness cutoff.")

    _set_progress(progress, phase="path-building", percent=92.0, completed=total_carriers, total=total_carriers, detail="Pattern paths are ready; compiling G-code...", compute_backend=compute_backend)
    return completed_paths, {
        "source": "browser-brightness-map",
        "map_width": rgba.width,
        "map_height": rgba.height,
        "carrier_count": total_carriers,
        "sample_step_mm": round(sample_step, 4),
        "pattern_spacing_mm": round(layout_spacing, 4),
        "sampled_points": sampled_points,
        "compute_backend": compute_backend,
        "gpu_accelerated": compute_backend == "cuda",
        "density_fudge": density_fudge,
        "brightness_cutoff": brightness_cutoff,
        "pattern_layout": layout,
        "waveform": waveform,
        "pattern_center_x": center_x,
        "pattern_center_y": center_y,
        "pattern_angle": angle_degrees,
        "pattern_clockwise": clockwise,
        "wave_amplitude_mm": wave_amplitude,
        "wave_length_mm": wave_length,
        "brightness_modulation": brightness_mode,
        "live_preview_points": preview_state["points"],
    }

def _compile_gcode(paths: list[list[list[float]]], params: dict[str, Any]) -> list[str]:
    z_mode = str(params["zMode"])
    z_up = str(params["zUp"])
    z_down = str(params["zDown"])
    xy_feed_rate = int(params["xyFeedRate"])
    z_plunge_rate = int(params["zPlungeRate"])

    gcode = ["G21", "G90", f"G0 Z{z_up}" if z_mode == "stepper" else f"M3 S{z_up}"]
    for path in paths:
        first = path[0]
        gcode.append(f"G0 X{first[0]:.2f} Y{first[1]:.2f}")
        gcode.append(f"G1 Z{z_down} F{z_plunge_rate}" if z_mode == "stepper" else f"M3 S{z_down}")
        for point_index, (x, y) in enumerate(path[1:]):
            feed = f" F{xy_feed_rate}" if point_index == 0 else ""
            gcode.append(f"G1 X{x:.2f} Y{y:.2f}{feed}")
        gcode.append(f"G0 Z{z_up}" if z_mode == "stepper" else f"M3 S{z_up}")
    gcode.append("G0 X0 Y0")
    return gcode

def generate_toolpath(
    svg_content: bytes,
    params: dict[str, Any],
    brightness_map: bytes | None = None,
    progress: MutableMapping[str, Any] | None = None,
    preview_queue: Any | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    _set_progress(progress, phase="validating", percent=1.0, detail="Validating generation settings...")
    _check_cancel_requested(progress)
    validate_generation_params(params)

    if brightness_map is not None:
        compiled_paths, raster_stats = _generate_brightness_paths(brightness_map, params, progress, preview_queue)
        _set_progress(
            progress,
            phase="gcode",
            percent=94.0,
            completed=0,
            total=len(compiled_paths),
            detail="Compiling continuous paths into G-code...",
            compute_backend=raster_stats.get("compute_backend"),
        )
        _check_cancel_requested(progress)
        gcode = _compile_gcode(compiled_paths, params)
        duration = time.perf_counter() - started
        toolpath_points = sum(len(path) for path in compiled_paths)
        result = {
            "gcode": "\n".join(gcode),
            "paths": compiled_paths,
            "stats": {
                "duration_seconds": round(duration, 3),
                "drawable_elements": None,
                "hatch_paths": len(compiled_paths),
                "continuous_paths": len(compiled_paths),
                "toolpath_points": toolpath_points,
                "gcode_lines": len(gcode),
                "pen_thickness_mm": float(params.get("penThickness", 0.5)),
                **raster_stats,
            },
        }
        _set_progress(
            progress,
            phase="completed",
            percent=100.0,
            completed=len(compiled_paths),
            total=len(compiled_paths),
            detail="Toolpath generation completed.",
            compute_backend=raster_stats.get("compute_backend"),
            force_eta_zero=True,
        )
        return result

    _set_progress(progress, phase="svg-parsing", percent=3.0, detail="Parsing SVG geometry...")
    try:
        parsed_svg = SVG.parse(io.BytesIO(svg_content))
    except Exception as exc:
        raise GenerationError(f"Unable to parse the uploaded SVG: {exc}") from exc

    bbox = parsed_svg.bbox()
    if bbox is None or len(bbox) != 4:
        raise GenerationError("The SVG does not contain drawable geometry.")
    if not all(math.isfinite(float(value)) for value in bbox):
        raise GenerationError("The SVG has invalid or non-finite bounds.")

    min_x, min_y, max_x, max_y = map(float, bbox)
    svg_width = max_x - min_x
    svg_height = max_y - min_y
    if svg_width <= 0 or svg_height <= 0:
        raise GenerationError("The SVG width and height must be greater than zero.")

    bed_x = float(params["bedX"])
    bed_y = float(params["bedY"])
    fit_scale = min((bed_x * 0.9) / svg_width, (bed_y * 0.9) / svg_height)
    requested_scale = float(params["svgScale"]) / 100.0
    scale_mode = str(params.get("svgScaleMode", "fit-relative"))
    # New browser clients use absolute percentage scaling: 100% preserves the
    # SVG's imported size. Older API clients retain the previous fit-relative
    # behavior unless they explicitly request absolute scaling.
    scale = requested_scale if scale_mode == "absolute" else fit_scale * requested_scale
    source_center = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    destination = (float(params["svgPosX"]), float(params["svgPosY"]))
    rotation = float(params["svgRotate"])
    machine_bed = box(0.0, 0.0, bed_x, bed_y)

    compiled_paths: list[list[list[float]]] = []
    drawable_elements = 0
    toolpath_points = 0
    candidate_elements = []
    for element in parsed_svg.elements():
        if not isinstance(element, (Path, Shape)):
            continue
        fill = getattr(element, "fill", None)
        stroke = getattr(element, "stroke", None)
        if (fill is None or fill == "none") and (stroke is None or stroke == "none"):
            continue
        candidate_elements.append(element)

    total_elements = len(candidate_elements)
    _set_progress(
        progress,
        phase="vector-processing",
        percent=8.0,
        completed=0,
        total=total_elements,
        detail=f"Processing {total_elements:,} SVG elements...",
        compute_backend="geos-cpu",
    )
    progress_stride = max(1, total_elements // 100) if total_elements else 1
    preview_state = {"points": 0, "chunks": 0}
    preview_pending: list[list[list[float]]] = []

    for element_index, element in enumerate(candidate_elements):
        if element_index % progress_stride == 0:
            _check_cancel_requested(progress)
            fraction = element_index / max(1, total_elements)
            _set_progress(
                progress,
                phase="vector-processing",
                percent=8.0 + (fraction * 82.0),
                completed=element_index,
                total=total_elements,
                detail=f"Processing SVG element {element_index + 1:,} of {total_elements:,}...",
                compute_backend="geos-cpu",
            )
        fill = getattr(element, "fill", None)
        stroke = getattr(element, "stroke", None)

        color = fill if fill is not None and fill != "none" else stroke
        path_element = Path(element) if isinstance(element, Shape) else Path(element)
        polygon = path_to_shapely(path_element)
        if polygon is None:
            continue
        drawable_elements += 1

        polygon = affinity.translate(polygon, xoff=-source_center[0], yoff=-source_center[1])
        polygon = affinity.scale(polygon, xfact=scale, yfact=scale, origin=(0.0, 0.0))
        polygon = affinity.rotate(polygon, rotation, origin=(0.0, 0.0), use_radians=False)
        polygon = affinity.translate(polygon, xoff=destination[0], yoff=destination[1])

        safe_polygon = polygon.intersection(machine_bed)
        if safe_polygon.is_empty:
            continue

        density_fudge = float(params.get("densityFudge", 0.0))
        density_scale = 1.0 - (density_fudge * 0.8)
        spacing = max(
            float(params.get("penThickness", 0.5)),
            (0.5 + (get_luminance(color) * 4.5)) * density_scale,
        )
        hatch_grid = create_hatch_lines(safe_polygon.bounds, spacing)
        # Intersect the complete grid in one GEOS operation. The original code
        # intersected every line independently, which was the primary timeout hot path.
        clipped_grid = safe_polygon.intersection(hatch_grid)

        for line in _iter_lines(clipped_grid):
            coordinates = [[round(float(x), 4), round(float(y), 4)] for x, y in line.coords]
            if len(coordinates) < 2:
                continue
            compiled_paths.append(coordinates)
            preview_pending.append(coordinates)
            if sum(len(path) for path in preview_pending) >= LIVE_PREVIEW_CHUNK_POINTS:
                _publish_live_preview(preview_queue, preview_pending, preview_state)
                preview_pending = []
            toolpath_points += len(coordinates)
            if len(compiled_paths) > MAX_HATCH_PATHS:
                raise GenerationLimitError(
                    f"The SVG generated more than {MAX_HATCH_PATHS:,} hatch paths. "
                    "Reduce the SVG complexity or scale before trying again."
                )
            if toolpath_points > MAX_TOOLPATH_POINTS:
                raise GenerationLimitError(
                    f"The SVG generated more than {MAX_TOOLPATH_POINTS:,} toolpath points. "
                    "Reduce the SVG complexity or scale before trying again."
                )

    _publish_live_preview(preview_queue, preview_pending, preview_state)
    _set_progress(
        progress,
        phase="vector-processing",
        percent=90.0,
        completed=total_elements,
        total=total_elements,
        detail="SVG geometry processing completed.",
        compute_backend="geos-cpu",
    )

    if drawable_elements == 0:
        raise GenerationError("No filled or stroked SVG paths could be converted.")

    _set_progress(
        progress,
        phase="gcode",
        percent=94.0,
        completed=0,
        total=len(compiled_paths),
        detail="Compiling vector paths into G-code...",
        compute_backend="geos-cpu",
    )
    _check_cancel_requested(progress)
    gcode = _compile_gcode(compiled_paths, params)

    duration = time.perf_counter() - started
    result = {
        "gcode": "\n".join(gcode),
        "paths": compiled_paths,
        "stats": {
            "duration_seconds": round(duration, 3),
            "drawable_elements": drawable_elements,
            "hatch_paths": len(compiled_paths),
            "toolpath_points": toolpath_points,
            "gcode_lines": len(gcode),
            "scale_mode": scale_mode,
            "scale_percent": float(params["svgScale"]),
            "source_width": round(svg_width, 4),
            "source_height": round(svg_height, 4),
            "compute_backend": "geos-cpu",
            "gpu_accelerated": False,
            "density_fudge": float(params.get("densityFudge", 0.0)),
            "brightness_cutoff": float(params.get("brightnessCutoff", DEFAULT_BRIGHTNESS_CUTOFF)),
            "live_preview_points": preview_state["points"],
        },
    }
    _set_progress(
        progress,
        phase="completed",
        percent=100.0,
        completed=len(compiled_paths),
        total=len(compiled_paths),
        detail="Toolpath generation completed.",
        compute_backend="geos-cpu",
        force_eta_zero=True,
    )
    return result


def generation_worker(
    svg_content: bytes,
    params: dict[str, Any],
    brightness_map: bytes | None = None,
    progress: MutableMapping[str, Any] | None = None,
    preview_queue: Any | None = None,
) -> dict[str, Any]:
    if progress is not None:
        try:
            now = time.time()
            progress.update({
                "started_at": now,
                "updated_at": now,
                "phase": "starting",
                "percent": 0.5,
                "detail": "Generation worker started.",
                "elapsed_seconds": 0.0,
                "eta_seconds": None,
            })
        except Exception:
            logger.debug("Unable to initialize worker progress", exc_info=True)
    try:
        return generate_toolpath(svg_content, params, brightness_map, progress, preview_queue)
    except CancelledError:
        raise
    except Exception as exc:
        # ProcessPool exceptions otherwise lose the useful worker-side traceback.
        raise RuntimeError(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}") from exc


def _cleanup_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        stale = [
            job_id
            for job_id, job in jobs.items()
            if job.get("updated_at", job["created_at"]) < cutoff
            and job.get("status") in {"completed", "failed", "cancelled"}
        ]
        for job_id in stale:
            jobs.pop(job_id, None)

        completed = sorted(
            (
                (job_id, job.get("updated_at", job["created_at"]))
                for job_id, job in jobs.items()
                if job.get("status") in {"completed", "failed", "cancelled"}
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        for job_id, _ in completed[MAX_RETAINED_RESULTS:]:
            jobs.pop(job_id, None)


def _job_finished(job_id: str, future: Future) -> None:
    try:
        result = future.result()
    except CancelledError:
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.update(status="cancelled", updated_at=time.time())
                _set_progress(
                    job.get("progress"),
                    phase="cancelled",
                    percent=float(job.get("progress", {}).get("percent", 0.0)),
                    detail="Generation was cancelled.",
                )
        return
    except Exception as exc:
        debug_error = str(exc)
        public_error = debug_error.split("\n", 1)[0]
        for prefix in ("RuntimeError: ", "GenerationError: ", "GenerationLimitError: "):
            public_error = public_error.replace(prefix, "", 1)
        logger.error("job_id=%s generation_failed error=%s", job_id, debug_error)
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.update(
                    status="failed",
                    error=public_error,
                    debug_error=debug_error,
                    updated_at=time.time(),
                )
                _set_progress(
                    job.get("progress"),
                    phase="failed",
                    percent=float(job.get("progress", {}).get("percent", 0.0)),
                    detail=public_error,
                )
    else:
        logger.info(
            "job_id=%s generation_completed duration_seconds=%s hatch_paths=%s",
            job_id,
            result.get("stats", {}).get("duration_seconds"),
            result.get("stats", {}).get("hatch_paths"),
        )
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.update(status="completed", result=result, updated_at=time.time())
                _set_progress(
                    job.get("progress"),
                    phase="completed",
                    percent=100.0,
                    detail="Toolpath generation completed.",
                    compute_backend=result.get("stats", {}).get("compute_backend"),
                    force_eta_zero=True,
                )


def _active_job_count() -> int:
    with jobs_lock:
        return sum(job.get("status") in {"queued", "processing"} for job in jobs.values())


def _submit_job(
    app_instance: FastAPI,
    svg_content: bytes,
    params: dict[str, Any],
    job_id: str,
    brightness_map: bytes | None = None,
    progress: MutableMapping[str, Any] | None = None,
    preview_queue: Any | None = None,
) -> Future:
    executor: ProcessPoolExecutor = app_instance.state.executor
    try:
        future = executor.submit(generation_worker, svg_content, params, brightness_map, progress, preview_queue)
    except BrokenProcessPool:
        logger.exception("Geometry worker pool was broken; recreating it")
        executor.shutdown(wait=False, cancel_futures=True)
        app_instance.state.executor = _new_executor()
        future = app_instance.state.executor.submit(generation_worker, svg_content, params, brightness_map, progress, preview_queue)
    future.add_done_callback(lambda completed: _job_finished(job_id, completed))
    return future


def _params_from_form(
    bedX: float,
    bedY: float,
    svgScale: float,
    svgScaleMode: str,
    svgRotate: float,
    svgPosX: float,
    svgPosY: float,
    zMode: str,
    zUp: str,
    zDown: str,
    xyFeedRate: int,
    zPlungeRate: int,
    penThickness: float,
    densityFudge: float,
    brightnessCutoff: float,
    patternLayout: str,
    waveform: str,
    patternCenterX: float,
    patternCenterY: float,
    patternAngle: float,
    patternSpacing: float,
    patternClockwise: bool,
    waveAmplitude: float,
    waveLength: float,
    brightnessModulation: str,
) -> dict[str, Any]:
    return {
        "bedX": bedX,
        "bedY": bedY,
        "svgScale": svgScale,
        "svgScaleMode": svgScaleMode,
        "svgRotate": svgRotate,
        "svgPosX": svgPosX,
        "svgPosY": svgPosY,
        "zMode": zMode,
        "zUp": zUp,
        "zDown": zDown,
        "xyFeedRate": xyFeedRate,
        "zPlungeRate": zPlungeRate,
        "penThickness": penThickness,
        "densityFudge": densityFudge,
        "brightnessCutoff": brightnessCutoff,
        "patternLayout": patternLayout,
        "waveform": waveform,
        "patternCenterX": patternCenterX,
        "patternCenterY": patternCenterY,
        "patternAngle": patternAngle,
        "patternSpacing": patternSpacing,
        "patternClockwise": patternClockwise,
        "waveAmplitude": waveAmplitude,
        "waveLength": waveLength,
        "brightnessModulation": brightnessModulation,
    }


async def _read_svg(file: UploadFile) -> bytes:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=422, detail="The uploaded SVG is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The SVG exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )
    header = content[:8192].lower()
    if b"<svg" not in header:
        raise HTTPException(status_code=422, detail="The uploaded file does not appear to be an SVG.")
    return content


async def _read_brightness_map(file: UploadFile | None) -> bytes | None:
    if file is None:
        return None
    content = await file.read(MAX_BRIGHTNESS_MAP_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=422, detail="The generated brightness map is empty.")
    if len(content) > MAX_BRIGHTNESS_MAP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The brightness map exceeds the {MAX_BRIGHTNESS_MAP_BYTES // (1024 * 1024)} MB upload limit.",
        )
    return content


@app.get("/health")
def health() -> dict[str, Any]:
    _cleanup_jobs()
    return {
        "status": "ok",
        "active_jobs": _active_job_count(),
        "job_workers": JOB_WORKERS,
        "acceleration_backend": ACCELERATION_BACKEND,
    }


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_generation_job(
    request: Request,
    file: UploadFile = File(...),
    brightnessMap: UploadFile | None = File(None),
    bedX: float = Form(210.0),
    bedY: float = Form(297.0),
    svgScale: float = Form(100.0),
    svgScaleMode: str = Form("fit-relative"),
    svgRotate: float = Form(0.0),
    svgPosX: float = Form(105.0),
    svgPosY: float = Form(148.5),
    zMode: str = Form("stepper"),
    zUp: str = Form("5.0"),
    zDown: str = Form("0.0"),
    xyFeedRate: int = Form(2000),
    zPlungeRate: int = Form(300),
    penThickness: float = Form(0.5),
    densityFudge: float = Form(0.0),
    brightnessCutoff: float = Form(DEFAULT_BRIGHTNESS_CUTOFF),
    patternLayout: str = Form("linear"),
    waveform: str = Form("zigzag"),
    patternCenterX: float = Form(105.0),
    patternCenterY: float = Form(148.5),
    patternAngle: float = Form(0.0),
    patternSpacing: float = Form(1.0),
    patternClockwise: bool = Form(True),
    waveAmplitude: float = Form(0.5),
    waveLength: float = Form(3.0),
    brightnessModulation: str = Form("both"),
):
    _cleanup_jobs()
    if _active_job_count() >= MAX_PENDING_JOBS:
        raise HTTPException(status_code=429, detail="The generation queue is full. Try again after a current job completes.")

    svg_content = await _read_svg(file)
    brightness_map = await _read_brightness_map(brightnessMap)
    params = _params_from_form(
        bedX,
        bedY,
        svgScale,
        svgScaleMode,
        svgRotate,
        svgPosX,
        svgPosY,
        zMode,
        zUp,
        zDown,
        xyFeedRate,
        zPlungeRate,
        penThickness,
        densityFudge,
        brightnessCutoff,
        patternLayout,
        waveform,
        patternCenterX,
        patternCenterY,
        patternAngle,
        patternSpacing,
        patternClockwise,
        waveAmplitude,
        waveLength,
        brightnessModulation,
    )
    try:
        validate_generation_params(params)
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job_id = uuid.uuid4().hex
    now = time.time()
    progress = request.app.state.progress_manager.dict({
        "phase": "queued",
        "percent": 0.0,
        "completed": 0,
        "total": 0,
        "detail": "Waiting for an available generation worker...",
        "compute_backend": None,
        "started_at": now,
        "updated_at": now,
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
        "cancel_requested": False,
    })
    preview_queue = request.app.state.progress_manager.list()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "filename": file.filename,
            "progress": progress,
            "preview": preview_queue,
        }

    future = _submit_job(request.app, svg_content, params, job_id, brightness_map, progress, preview_queue)
    with jobs_lock:
        jobs[job_id]["future"] = future
        jobs[job_id]["status"] = "processing" if future.running() else "queued"

    logger.info("job_id=%s filename=%s bytes=%d generation_submitted", job_id, file.filename, len(svg_content))
    return {"job_id": job_id, "status": jobs[job_id]["status"]}


@app.get("/jobs/{job_id}")
def get_generation_job(job_id: str, preview_after: int = 0):
    _cleanup_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Generation job not found or expired.")
        future: Future | None = job.get("future")
        current_status = job["status"]
        if current_status == "queued" and future is not None and future.running():
            current_status = "processing"
            job["status"] = current_status
        progress_proxy = job.get("progress")
        progress_snapshot = dict(progress_proxy) if progress_proxy is not None else None
        preview_proxy = job.get("preview")
        preview_total = len(preview_proxy) if preview_proxy is not None else 0
        preview_start = max(0, min(int(preview_after), preview_total))
        preview_end = min(preview_total, preview_start + 8)
        preview_items = [preview_proxy[index] for index in range(preview_start, preview_end)] if preview_proxy is not None else []
        response = {
            "job_id": job_id,
            "status": current_status,
            "error": job.get("error"),
            "progress": progress_snapshot,
            "preview": preview_items,
            "preview_next": preview_end,
            "preview_total": preview_total,
        }
        if current_status == "completed":
            response["stats"] = job.get("result", {}).get("stats", {})
        return response


@app.get("/jobs/{job_id}/result")
def get_generation_result(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Generation job not found or expired.")
        if job["status"] == "failed":
            raise HTTPException(status_code=422, detail=job.get("error", "Generation failed."))
        if job["status"] != "completed":
            raise HTTPException(status_code=409, detail="Generation has not completed yet.")
        return job["result"]


@app.delete("/jobs/{job_id}")
def cancel_generation_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Generation job not found or expired.")
        future: Future | None = job.get("future")
        progress = job.get("progress")
        if progress is not None:
            try:
                progress["cancel_requested"] = True
            except Exception:
                logger.debug("Unable to flag cancellation for job_id=%s", job_id, exc_info=True)
        cancelled = bool(future and future.cancel())
        if cancelled:
            job.update(status="cancelled", updated_at=time.time())
            _set_progress(
                progress,
                phase="cancelled",
                percent=float(progress.get("percent") or 0.0) if progress is not None else 0.0,
                detail="Generation was cancelled.",
            )
        else:
            _set_progress(
                progress,
                phase="cancelling",
                percent=float(progress.get("percent") or 0.0) if progress is not None else 0.0,
                detail="Cancellation requested; stopping the current worker step...",
            )
            if job.get("status") not in {"completed", "failed", "cancelled"}:
                job.update(updated_at=time.time())
        return {
            "job_id": job_id,
            "status": "cancelled" if cancelled else job["status"],
            "cancelled": cancelled,
            "cancel_requested": True,
        }


@app.post("/generate")
async def generate_gcode_compatibility(
    request: Request,
    file: UploadFile = File(...),
    brightnessMap: UploadFile | None = File(None),
    bedX: float = Form(210.0),
    bedY: float = Form(297.0),
    svgScale: float = Form(100.0),
    svgScaleMode: str = Form("fit-relative"),
    svgRotate: float = Form(0.0),
    svgPosX: float = Form(105.0),
    svgPosY: float = Form(148.5),
    zMode: str = Form("stepper"),
    zUp: str = Form("5.0"),
    zDown: str = Form("0.0"),
    xyFeedRate: int = Form(2000),
    zPlungeRate: int = Form(300),
    penThickness: float = Form(0.5),
    densityFudge: float = Form(0.0),
    brightnessCutoff: float = Form(DEFAULT_BRIGHTNESS_CUTOFF),
    patternLayout: str = Form("linear"),
    waveform: str = Form("zigzag"),
    patternCenterX: float = Form(105.0),
    patternCenterY: float = Form(148.5),
    patternAngle: float = Form(0.0),
    patternSpacing: float = Form(1.0),
    patternClockwise: bool = Form(True),
    waveAmplitude: float = Form(0.5),
    waveLength: float = Form(3.0),
    brightnessModulation: str = Form("both"),
):
    """Backward-compatible synchronous endpoint for existing API clients."""
    svg_content = await _read_svg(file)
    brightness_map = await _read_brightness_map(brightnessMap)
    params = _params_from_form(
        bedX,
        bedY,
        svgScale,
        svgScaleMode,
        svgRotate,
        svgPosX,
        svgPosY,
        zMode,
        zUp,
        zDown,
        xyFeedRate,
        zPlungeRate,
        penThickness,
        densityFudge,
        brightnessCutoff,
        patternLayout,
        waveform,
        patternCenterX,
        patternCenterY,
        patternAngle,
        patternSpacing,
        patternClockwise,
        waveAmplitude,
        waveLength,
        brightnessModulation,
    )
    try:
        validate_generation_params(params)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(request.app.state.executor, generation_worker, svg_content, params, brightness_map)
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.error("Synchronous generation failed: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc).split("\n", 1)[0]) from exc


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
