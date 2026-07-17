from __future__ import annotations

import asyncio
import base64
import ftplib
import hashlib
import http.client
import io
import json
import logging
import math
import os
import queue
import ssl
import threading
import textwrap
import time
import urllib.parse
from multiprocessing import Manager
import traceback
import uuid
from concurrent.futures import CancelledError, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterable, MutableMapping

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from linedraw_engine import LineDrawSettings, convert_image
from converter_engines import (
    CommonRasterSettings,
    InkscapeTraceSettings,
    Pixels2SvgSettings,
    PotraceSettings,
    converter_engine_status,
    convert_inkscape,
    convert_pixels2svg,
    convert_potrace,
)
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
from svgelements import Arc, Close, Color, CubicBezier, Line, Move, Path, QuadraticBezier, SVG, Shape

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
GCODE_MAX_LINE_LENGTH = 64
PEN_ACTION_DELAY_SECONDS = 0.5
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
converter_transfers: dict[str, dict[str, Any]] = {}
converter_transfers_lock = threading.Lock()
CONVERTER_TRANSFER_TTL_SECONDS = max(60, int(os.getenv("CONVERTER_TRANSFER_TTL_SECONDS", "900")))
MAX_CONVERTER_TRANSFERS = max(1, int(os.getenv("MAX_CONVERTER_TRANSFERS", "12")))
NETWORK_DELIVERY_ENABLED = os.getenv("NETWORK_DELIVERY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
NETWORK_DELIVERY_TIMEOUT_SECONDS = max(3, min(1800, int(os.getenv("NETWORK_DELIVERY_TIMEOUT_SECONDS", "300"))))
MAX_NETWORK_GCODE_BYTES = max(1024, int(os.getenv("MAX_NETWORK_GCODE_BYTES", str(10 * 1024 * 1024))))


class GenerationError(ValueError):
    """An SVG or parameter problem that can be shown safely to the user."""


class GenerationLimitError(GenerationError):
    """The requested toolpath is too large to generate safely."""


class NetworkDeliveryError(ValueError):
    """A safe, user-facing network delivery failure."""


class GcodeDeliveryRequest(BaseModel):
    protocol: str = Field(min_length=3, max_length=12)
    filename: str = Field(min_length=1, max_length=255)
    gcode: str = Field(min_length=1)
    url: str = Field(default="", max_length=2048)
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    directory: str = Field(default="", max_length=1024)
    username: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=2048)
    ftp_tls: bool = False
    passive: bool = True
    verify_tls: bool = True


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


app = FastAPI(title="HatchPlot API", version="2.5.0", lifespan=lifespan)


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


def _network_gcode_bytes(gcode: str) -> bytes:
    content = str(gcode).rstrip("\r\n") + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_NETWORK_GCODE_BYTES:
        raise NetworkDeliveryError(
            f"The G-code file exceeds the {MAX_NETWORK_GCODE_BYTES // (1024 * 1024)} MB network delivery limit."
        )
    return encoded


def _safe_delivery_filename(filename: str) -> str:
    cleaned = str(filename).strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or os.path.basename(cleaned) != cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or any(ord(character) < 32 for character in cleaned)
    ):
        raise NetworkDeliveryError("The remote filename must be a plain filename without directory components.")
    return cleaned


def _webdav_target_url(base_url: str, filename: str) -> str:
    parsed = urllib.parse.urlsplit(str(base_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkDeliveryError("Enter a complete WebDAV folder URL beginning with http:// or https://.")
    try:
        parsed.port
    except ValueError as exc:
        raise NetworkDeliveryError("The WebDAV URL contains an invalid port.") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NetworkDeliveryError("The WebDAV URL cannot contain credentials, a query string, or a fragment.")
    folder_path = parsed.path.rstrip("/") + "/"
    target_path = folder_path + urllib.parse.quote(filename, safe="")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, target_path, "", ""))


def _deliver_webdav(payload: GcodeDeliveryRequest, content: bytes, filename: str, progress: Callable[[int, int], None] | None = None) -> str:
    target_url = _webdav_target_url(payload.url, filename)
    parsed = urllib.parse.urlsplit(target_url)
    request_path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
    headers = [
        # Treat G-code as an opaque file. Some embedded WebDAV servers parse
        # text PUT bodies like form data and discard content containing `=`.
        ("Content-Type", "application/octet-stream"),
        # Preserve this exact spelling. Some embedded WebDAV/SD-card servers
        # incorrectly treat HTTP header names as case-sensitive and interpret
        # urllib's normalized `Content-length` spelling as a zero-byte PUT.
        ("Content-Length", str(len(content))),
        ("User-Agent", "HatchPlot/2.5"),
        ("Connection", "close"),
    ]
    if payload.username or payload.password:
        credentials = f"{payload.username}:{payload.password}".encode("utf-8")
        headers.append(("Authorization", "Basic " + base64.b64encode(credentials).decode("ascii")))

    context = ssl.create_default_context() if payload.verify_tls else ssl._create_unverified_context()
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection_kwargs: dict[str, Any] = {
        "host": parsed.hostname,
        "port": parsed.port,
        "timeout": NETWORK_DELIVERY_TIMEOUT_SECONDS,
    }
    if parsed.scheme == "https":
        connection_kwargs["context"] = context
    connection = connection_class(**connection_kwargs)
    try:
        connection.putrequest("PUT", request_path, skip_accept_encoding=True)
        for header, value in headers:
            connection.putheader(header, value)
        connection.endheaders()
        sent = 0
        for offset in range(0, len(content), 64 * 1024):
            chunk = content[offset:offset + (64 * 1024)]
            connection.send(chunk)
            sent += len(chunk)
            if progress is not None:
                progress(sent, len(content))
        response = connection.getresponse()
        response_status = int(response.status)
        response.read(4096)
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        detail = str(exc).strip()[:240]
        raise NetworkDeliveryError(f"Unable to reach the WebDAV server: {detail}") from exc
    finally:
        connection.close()
    if response_status not in {200, 201, 204}:
        raise NetworkDeliveryError(f"The WebDAV server rejected the upload with HTTP {response_status}.")
    return target_url


def _ftp_host(host: str) -> str:
    cleaned = str(host).strip()
    if not cleaned or any(character in cleaned for character in "/\\@") or any(character.isspace() for character in cleaned):
        raise NetworkDeliveryError("Enter an FTP hostname or IP address without a URL scheme or path.")
    return cleaned


def _ftp_directory(directory: str) -> str:
    cleaned = str(directory).strip()
    if "\x00" in cleaned or any(part == ".." for part in cleaned.replace("\\", "/").split("/")):
        raise NetworkDeliveryError("The FTP directory cannot contain parent-directory components.")
    return cleaned


def _deliver_ftp(payload: GcodeDeliveryRequest, content: bytes, filename: str, progress: Callable[[int, int], None] | None = None) -> str:
    host = _ftp_host(payload.host)
    directory = _ftp_directory(payload.directory)
    port = payload.port or 21
    client: ftplib.FTP = (
        ftplib.FTP_TLS(context=ssl.create_default_context())
        if payload.ftp_tls
        else ftplib.FTP()
    )
    try:
        client.connect(host, port, timeout=NETWORK_DELIVERY_TIMEOUT_SECONDS)
        client.login(payload.username or "anonymous", payload.password or "anonymous@")
        if payload.ftp_tls:
            client.prot_p()
        client.set_pasv(payload.passive)
        if directory:
            client.cwd(directory)
        sent = 0
        def report_chunk(chunk: bytes) -> None:
            nonlocal sent
            sent += len(chunk)
            if progress is not None:
                progress(sent, len(content))
        client.storbinary(f"STOR {filename}", io.BytesIO(content), callback=report_chunk)
        try:
            client.quit()
        except ftplib.all_errors:
            client.close()
    except ftplib.all_errors as exc:
        try:
            client.close()
        except OSError:
            pass
        detail = str(exc).strip()[:240] or "the FTP server rejected the connection"
        raise NetworkDeliveryError(f"FTP upload failed: {detail}") from exc
    scheme = "ftps" if payload.ftp_tls else "ftp"
    remote_path = "/".join(part for part in [directory.strip("/"), urllib.parse.quote(filename, safe="")] if part)
    return f"{scheme}://{host}:{port}/{remote_path}"


def deliver_gcode_file(payload: GcodeDeliveryRequest, progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    if not NETWORK_DELIVERY_ENABLED:
        raise NetworkDeliveryError("Network G-code delivery is disabled by the server administrator.")
    filename = _safe_delivery_filename(payload.filename)
    content = _network_gcode_bytes(payload.gcode)
    protocol = payload.protocol.strip().lower()
    if protocol == "webdav":
        destination = _deliver_webdav(payload, content, filename, progress)
    elif protocol == "ftp":
        destination = _deliver_ftp(payload, content, filename, progress)
    else:
        raise NetworkDeliveryError("Protocol must be webdav or ftp.")
    return {
        "status": "sent",
        "protocol": "ftps" if protocol == "ftp" and payload.ftp_tls else protocol,
        "filename": filename,
        "bytes": len(content),
        "destination": destination,
    }


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


def _point_xy(point: Any) -> tuple[float, float] | None:
    if point is None:
        return None
    try:
        x = float(point.x)
        y = float(point.y)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def _point_line_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = (dx * dx) + (dy * dy)
    if denominator <= 1e-18:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    projection = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator
    nearest_x = start[0] + (projection * dx)
    nearest_y = start[1] + (projection * dy)
    return math.hypot(point[0] - nearest_x, point[1] - nearest_y)


def _append_unique(points: list[tuple[float, float]], point: tuple[float, float] | None) -> None:
    if point is None:
        return
    if not points or point != points[-1]:
        points.append(point)


def _flatten_cubic(
    start: tuple[float, float],
    control1: tuple[float, float],
    control2: tuple[float, float],
    end: tuple[float, float],
    tolerance: float,
    points: list[tuple[float, float]],
) -> None:
    """Flatten a cubic Bezier without expensive numerical length integration."""
    stack = [(start, control1, control2, end, 0)]
    maximum_depth = 14
    while stack:
        p0, p1, p2, p3, depth = stack.pop()
        flatness = max(
            _point_line_distance(p1, p0, p3),
            _point_line_distance(p2, p0, p3),
        )
        if flatness <= tolerance or depth >= maximum_depth:
            _append_unique(points, p3)
            continue

        p01 = ((p0[0] + p1[0]) * 0.5, (p0[1] + p1[1]) * 0.5)
        p12 = ((p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5)
        p23 = ((p2[0] + p3[0]) * 0.5, (p2[1] + p3[1]) * 0.5)
        p012 = ((p01[0] + p12[0]) * 0.5, (p01[1] + p12[1]) * 0.5)
        p123 = ((p12[0] + p23[0]) * 0.5, (p12[1] + p23[1]) * 0.5)
        midpoint = ((p012[0] + p123[0]) * 0.5, (p012[1] + p123[1]) * 0.5)
        stack.append((midpoint, p123, p23, p3, depth + 1))
        stack.append((p0, p01, p012, midpoint, depth + 1))


def _flatten_quadratic(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    tolerance: float,
    points: list[tuple[float, float]],
) -> None:
    stack = [(start, control, end, 0)]
    maximum_depth = 14
    while stack:
        p0, p1, p2, depth = stack.pop()
        if _point_line_distance(p1, p0, p2) <= tolerance or depth >= maximum_depth:
            _append_unique(points, p2)
            continue
        p01 = ((p0[0] + p1[0]) * 0.5, (p0[1] + p1[1]) * 0.5)
        p12 = ((p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5)
        midpoint = ((p01[0] + p12[0]) * 0.5, (p01[1] + p12[1]) * 0.5)
        stack.append((midpoint, p12, p2, depth + 1))
        stack.append((p0, p01, midpoint, depth + 1))


def path_to_centerlines(element: Path, sample_step: float) -> list[LineString]:
    """Flatten native SVG segments using geometric error rather than point spacing.

    The previous implementation called ``segment.length()`` for every Bezier.
    svgelements evaluates that length using numerical integration, which becomes
    extremely expensive for traced SVGs containing tens of thousands of curves.
    Adaptive subdivision preserves visible curve detail while avoiding those
    integrations and omitting redundant points along nearly straight segments.
    """
    lines: list[LineString] = []
    # sample_step historically represented desired point spacing. A quarter of
    # that value is a conservative maximum curve-to-chord deviation.
    tolerance = max(0.0025, float(sample_step) * 0.25)
    for raw_subpath in element.as_subpaths():
        subpath = Path(raw_subpath)
        points: list[tuple[float, float]] = []
        for segment in subpath:
            if isinstance(segment, Move):
                _append_unique(points, _point_xy(segment.end))
                continue
            if isinstance(segment, (Line, Close)):
                _append_unique(points, _point_xy(segment.end))
                continue
            if isinstance(segment, CubicBezier):
                start = _point_xy(segment.start)
                control1 = _point_xy(segment.control1)
                control2 = _point_xy(segment.control2)
                end = _point_xy(segment.end)
                if None not in (start, control1, control2, end):
                    _append_unique(points, start)
                    _flatten_cubic(start, control1, control2, end, tolerance, points)
                continue
            if isinstance(segment, QuadraticBezier):
                start = _point_xy(segment.start)
                control = _point_xy(segment.control)
                end = _point_xy(segment.end)
                if None not in (start, control, end):
                    _append_unique(points, start)
                    _flatten_quadratic(start, control, end, tolerance, points)
                continue

            # Arcs and uncommon custom segments retain a small generic fallback.
            try:
                segment_length = float(segment.length())
            except (AttributeError, TypeError, ValueError):
                continue
            if not math.isfinite(segment_length) or segment_length <= 0.0:
                continue
            sample_count = max(1, min(4096, int(math.ceil(segment_length / max(sample_step, 0.01)))))
            for index in range(sample_count + 1):
                _append_unique(points, _point_xy(segment.point(index / sample_count)))
        if len(points) >= 2:
            lines.append(LineString(points))
    return lines


def _transform_svg_geometry(
    geometry: Any,
    source_center: tuple[float, float],
    scale: float,
    rotation: float,
    destination: tuple[float, float],
) -> Any:
    geometry = affinity.translate(geometry, xoff=-source_center[0], yoff=-source_center[1])
    geometry = affinity.scale(geometry, xfact=scale, yfact=scale, origin=(0.0, 0.0))
    geometry = affinity.rotate(geometry, rotation, origin=(0.0, 0.0), use_radians=False)
    return affinity.translate(geometry, xoff=destination[0], yoff=destination[1])


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

    generation_mode = str(params.get("generationMode", "hatch"))
    if generation_mode not in {"hatch", "full-fill", "single-line", "outline", "outline-hatch"}:
        raise GenerationError("generationMode must be hatch, full-fill, single-line, outline, or outline-hatch.")
    if params.get("outlineTraceMethod", "boundary") not in {"boundary", "centerline"}:
        raise GenerationError("outlineTraceMethod must be boundary or centerline.")
    if params.get("workspaceOrigin", "top-left") not in {"top-left", "top-right", "bottom-left", "bottom-right"}:
        raise GenerationError("workspaceOrigin must be top-left, top-right, bottom-left, or bottom-right.")

    if generation_mode in {"hatch", "full-fill", "single-line", "outline-hatch"}:
        density_fudge = float(params.get("densityFudge", 0.0))
        if not math.isfinite(density_fudge) or not -0.5 <= density_fudge <= 0.5:
            raise GenerationError("densityFudge must be between -0.5 and 0.5.")

        brightness_cutoff = float(params.get("brightnessCutoff", DEFAULT_BRIGHTNESS_CUTOFF))
        if not math.isfinite(brightness_cutoff) or not 0.0 <= brightness_cutoff <= 1.0:
            raise GenerationError("brightnessCutoff must be between 0.0 and 1.0.")

        if params.get("patternLayout", "linear") not in {"linear", "spiral", "concentric", "radial"}:
            raise GenerationError("patternLayout must be linear, spiral, concentric, or radial.")
        allowed_waveforms = {"zigzag", "sawtooth", "sine", "ekg", "straight", "swirl"}
        if params.get("waveform", "zigzag") not in allowed_waveforms:
            raise GenerationError("waveform must be zigzag, sawtooth, sine, ekg, swirl, or straight.")
        if generation_mode == "single-line" and params.get("patternLayout", "linear") not in {"linear", "concentric"}:
            raise GenerationError("Single Line layout must be linear or concentric.")
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

    if generation_mode in {"outline", "outline-hatch"}:
        for key in ("sourceWidthMm", "sourceHeightMm"):
            value = float(params.get(key, 0.0))
            if value and (not math.isfinite(value) or value <= 0.0):
                raise GenerationError(f"{key} must be a positive finite number when provided.")

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

    for key in ("startGcode", "endGcode"):
        value = str(params.get(key, ""))
        if "\x00" in value:
            raise GenerationError(f"{key} cannot contain null characters.")
        if len(value.encode("utf-8")) > 65_536:
            raise GenerationError(f"{key} cannot exceed 64 KiB.")


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




def _simplify_trace(points: list[tuple[float, float]], tolerance: float, closed: bool = False) -> list[list[float]]:
    """Simplify a raster-derived trace while preserving its visible geometry."""
    if len(points) < 2:
        return []
    source = points
    if closed and points[0] != points[-1]:
        source = [*points, points[0]]
    simplified = LineString(source).simplify(max(0.0, tolerance), preserve_topology=False)
    if simplified.is_empty:
        return []
    if isinstance(simplified, MultiLineString):
        line = max(simplified.geoms, key=lambda item: item.length, default=None)
        if line is None:
            return []
    elif isinstance(simplified, LineString):
        line = simplified
    else:
        return []
    coordinates = [[round(float(x), 4), round(float(y), 4)] for x, y in line.coords]
    if closed and len(coordinates) >= 3 and coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0][:])
    return coordinates if len(coordinates) >= 2 else []


def _trace_pixel_graph(
    adjacency: dict[int, set[int]],
    point_for_id: Any,
    *,
    simplify_tolerance: float,
    minimum_length: float,
) -> list[list[list[float]]]:
    """Convert an undirected pixel graph into plotter-friendly polylines."""
    visited: set[tuple[int, int]] = set()
    paths: list[list[list[float]]] = []

    def edge_key(first: int, second: int) -> tuple[int, int]:
        return (first, second) if first < second else (second, first)

    def trace(start: int, neighbor: int) -> list[int]:
        result = [start]
        previous = start
        current = neighbor
        visited.add(edge_key(start, neighbor))
        while True:
            result.append(current)
            candidates = [
                item for item in adjacency.get(current, ())
                if item != previous and edge_key(current, item) not in visited
            ]
            if len(adjacency.get(current, ())) != 2 or not candidates:
                break
            next_item = candidates[0]
            visited.add(edge_key(current, next_item))
            previous, current = current, next_item
        return result

    # Endpoints and junctions first, then closed loops left in the graph.
    starts = [node for node, neighbors in adjacency.items() if len(neighbors) != 2]
    for start in starts:
        for neighbor in tuple(adjacency.get(start, ())):
            if edge_key(start, neighbor) in visited:
                continue
            node_path = trace(start, neighbor)
            points = [point_for_id(node) for node in node_path]
            if len(points) < 2 or LineString(points).length < minimum_length:
                continue
            simplified = _simplify_trace(points, simplify_tolerance, closed=False)
            if simplified:
                paths.append(simplified)

    for start, neighbors in adjacency.items():
        for neighbor in tuple(neighbors):
            if edge_key(start, neighbor) in visited:
                continue
            node_path = trace(start, neighbor)
            closed = len(node_path) > 2 and node_path[-1] in adjacency.get(node_path[0], ())
            if closed:
                visited.add(edge_key(node_path[-1], node_path[0]))
            points = [point_for_id(node) for node in node_path]
            if len(points) < 2 or LineString(points).length < minimum_length:
                continue
            simplified = _simplify_trace(points, simplify_tolerance, closed=closed)
            if simplified:
                paths.append(simplified)
    return paths


def _thin_binary_linework(mask: np.ndarray, maximum_iterations: int = 96) -> np.ndarray:
    """Zhang-Suen thinning for browser-rendered SVG linework."""
    image = mask.astype(bool, copy=True)
    if image.shape[0] < 3 or image.shape[1] < 3:
        return image

    for _ in range(maximum_iterations):
        changed = False
        for first_pass in (True, False):
            padded = np.pad(image, 1, mode="constant", constant_values=False)
            p2 = padded[:-2, 1:-1]
            p3 = padded[:-2, 2:]
            p4 = padded[1:-1, 2:]
            p5 = padded[2:, 2:]
            p6 = padded[2:, 1:-1]
            p7 = padded[2:, :-2]
            p8 = padded[1:-1, :-2]
            p9 = padded[:-2, :-2]
            neighbors = (p2.astype(np.uint8) + p3 + p4 + p5 + p6 + p7 + p8 + p9)
            transitions = (
                ((~p2) & p3).astype(np.uint8)
                + ((~p3) & p4)
                + ((~p4) & p5)
                + ((~p5) & p6)
                + ((~p6) & p7)
                + ((~p7) & p8)
                + ((~p8) & p9)
                + ((~p9) & p2)
            )
            removable = image & (neighbors >= 2) & (neighbors <= 6) & (transitions == 1)
            if first_pass:
                removable &= ~(p2 & p4 & p6)
                removable &= ~(p4 & p6 & p8)
            else:
                removable &= ~(p2 & p4 & p8)
                removable &= ~(p2 & p6 & p8)
            if np.any(removable):
                image[removable] = False
                changed = True
        if not changed:
            break
    return image


def _vectorize_skeleton(
    skeleton: np.ndarray,
    bed_x: float,
    bed_y: float,
    simplify_tolerance: float,
    minimum_length: float,
) -> list[list[list[float]]]:
    height, width = skeleton.shape
    rows, columns = np.nonzero(skeleton)
    node_ids = {int(row) * width + int(column) for row, column in zip(rows, columns)}
    adjacency: dict[int, set[int]] = {node: set() for node in node_ids}
    offsets = (-width - 1, -width, -width + 1, -1, 1, width - 1, width, width + 1)
    for node in node_ids:
        row, column = divmod(node, width)
        for offset in offsets:
            other = node + offset
            if other not in node_ids:
                continue
            other_row, other_column = divmod(other, width)
            row_delta = other_row - row
            column_delta = other_column - column
            if abs(row_delta) > 1 or abs(column_delta) > 1:
                continue
            if row_delta and column_delta:
                # Do not add a diagonal shortcut when the skeleton already has an
                # orthogonal connection around that corner. This keeps smooth curves
                # as one degree-2 loop instead of splitting them into tiny branches.
                horizontal = row * width + other_column
                vertical = other_row * width + column
                if horizontal in node_ids or vertical in node_ids:
                    continue
            adjacency[node].add(other)

    def point_for_id(node: int) -> tuple[float, float]:
        row, column = divmod(node, width)
        return (((column + 0.5) / width) * bed_x, ((row + 0.5) / height) * bed_y)

    return _trace_pixel_graph(
        adjacency,
        point_for_id,
        simplify_tolerance=simplify_tolerance,
        minimum_length=minimum_length,
    )


def _vectorize_boundaries(
    mask: np.ndarray,
    bed_x: float,
    bed_y: float,
    simplify_tolerance: float,
    minimum_length: float,
) -> list[list[list[float]]]:
    height, width = mask.shape
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    top = mask & ~padded[:-2, 1:-1]
    right = mask & ~padded[1:-1, 2:]
    bottom = mask & ~padded[2:, 1:-1]
    left = mask & ~padded[1:-1, :-2]
    grid_width = width + 1
    adjacency: dict[int, set[int]] = {}

    def add_edge(first: int, second: int) -> None:
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)

    for row, column in zip(*np.nonzero(top)):
        add_edge(int(row) * grid_width + int(column), int(row) * grid_width + int(column) + 1)
    for row, column in zip(*np.nonzero(right)):
        add_edge(int(row) * grid_width + int(column) + 1, (int(row) + 1) * grid_width + int(column) + 1)
    for row, column in zip(*np.nonzero(bottom)):
        add_edge((int(row) + 1) * grid_width + int(column) + 1, (int(row) + 1) * grid_width + int(column))
    for row, column in zip(*np.nonzero(left)):
        add_edge((int(row) + 1) * grid_width + int(column), int(row) * grid_width + int(column))

    def point_for_id(node: int) -> tuple[float, float]:
        row, column = divmod(node, grid_width)
        return ((column / width) * bed_x, (row / height) * bed_y)

    return _trace_pixel_graph(
        adjacency,
        point_for_id,
        simplify_tolerance=simplify_tolerance,
        minimum_length=minimum_length,
    )


def _generate_outline_paths_from_raster(
    outline_map: bytes,
    params: dict[str, Any],
    progress: MutableMapping[str, Any] | None = None,
    preview_queue: Any | None = None,
) -> tuple[list[list[list[float]]], dict[str, Any]]:
    """Trace the browser-rendered SVG so output matches the visible canvas."""
    bed_x = float(params["bedX"])
    bed_y = float(params["bedY"])
    pen_thickness = float(params.get("penThickness", 0.5))
    _set_progress(progress, phase="outline-decoding", percent=3.0, detail="Decoding the browser-rendered outline map...")
    _check_cancel_requested(progress)
    try:
        image = Image.open(io.BytesIO(outline_map))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise GenerationError(f"Unable to read the rendered outline map: {exc}") from exc
    if image.width * image.height > MAX_BRIGHTNESS_MAP_PIXELS:
        raise GenerationLimitError(f"The outline map contains more than {MAX_BRIGHTNESS_MAP_PIXELS:,} pixels.")

    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32)
    luminance = (0.299 * rgba[:, :, 0] + 0.587 * rgba[:, :, 1] + 0.114 * rgba[:, :, 2]) / 255.0
    darkness = np.clip((1.0 - luminance) * (rgba[:, :, 3] / 255.0), 0.0, 1.0)
    maximum_darkness = float(np.max(darkness)) if darkness.size else 0.0
    if maximum_darkness < 0.01:
        raise GenerationError("The rendered SVG does not contain visible linework to trace.")
    threshold = max(0.025, min(0.35, maximum_darkness * 0.18))
    mask = darkness >= threshold

    # Remove isolated antialiasing specks without eroding valid one-pixel lines.
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    neighbor_count = sum(
        padded[1 + row_offset:1 + row_offset + mask.shape[0], 1 + column_offset:1 + column_offset + mask.shape[1]]
        for row_offset, column_offset in (
            (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)
        )
    )
    mask &= neighbor_count >= 1
    ink_pixels = int(np.count_nonzero(mask))
    if ink_pixels < 2:
        raise GenerationError("The rendered SVG does not contain enough visible linework to trace.")

    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    interior = mask.copy()
    for row_offset, column_offset in (
        (-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)
    ):
        interior &= padded[1 + row_offset:1 + row_offset + mask.shape[0], 1 + column_offset:1 + column_offset + mask.shape[1]]
    interior_fraction = float(np.count_nonzero(interior)) / ink_pixels
    trace_method = str(params.get("outlineTraceMethod", "centerline"))
    pixel_mm = max(bed_x / max(1, image.width), bed_y / max(1, image.height))
    simplify_tolerance = max(pixel_mm * 0.65, pen_thickness * 0.06, 0.015)
    minimum_length = max(pixel_mm * 2.0, pen_thickness * 0.75, 0.08)

    _set_progress(
        progress,
        phase="outline-tracing",
        percent=18.0,
        detail=(
            "Extracting centerlines from visible SVG linework..."
            if trace_method == "centerline"
            else "Extracting visible boundaries from filled SVG artwork..."
        ),
        compute_backend="numpy-cpu",
    )
    _check_cancel_requested(progress)
    if trace_method == "centerline":
        skeleton = _thin_binary_linework(mask)
        paths = _vectorize_skeleton(skeleton, bed_x, bed_y, simplify_tolerance, minimum_length)
    else:
        paths = _vectorize_boundaries(mask, bed_x, bed_y, simplify_tolerance, minimum_length)

    if not paths:
        raise GenerationError("The rendered SVG linework could not be converted into continuous outline paths.")
    toolpath_points = sum(len(path) for path in paths)
    if len(paths) > MAX_HATCH_PATHS:
        raise GenerationLimitError(
            f"The SVG generated more than {MAX_HATCH_PATHS:,} outline paths. Reduce its complexity or scale."
        )
    if toolpath_points > MAX_TOOLPATH_POINTS:
        raise GenerationLimitError(
            f"The SVG generated more than {MAX_TOOLPATH_POINTS:,} outline points. Reduce its complexity or scale."
        )

    preview_state = {"points": 0, "chunks": 0}
    _publish_live_preview(preview_queue, paths, preview_state)
    _set_progress(
        progress,
        phase="outline-tracing",
        percent=90.0,
        completed=len(paths),
        total=len(paths),
        detail=f"Traced {len(paths):,} visible {trace_method} path{'s' if len(paths) != 1 else ''}.",
        compute_backend="numpy-cpu",
    )
    return paths, {
        "outline_trace_source": "browser-rendered-raster",
        "outline_trace_method": trace_method,
        "outline_threshold": round(threshold, 4),
        "outline_ink_pixels": ink_pixels,
        "outline_interior_fraction": round(interior_fraction, 4),
        "outline_sampling_mm": round(pixel_mm, 4),
        "compute_backend": "numpy-cpu",
        "gpu_accelerated": False,
        "live_preview_points": preview_state["points"],
    }


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


_DENSITY_BAYER_8 = (
    (0, 48, 12, 60, 3, 51, 15, 63),
    (32, 16, 44, 28, 35, 19, 47, 31),
    (8, 56, 4, 52, 11, 59, 7, 55),
    (40, 24, 36, 20, 43, 27, 39, 23),
    (2, 50, 14, 62, 1, 49, 13, 61),
    (34, 18, 46, 30, 33, 17, 45, 29),
    (10, 58, 6, 54, 9, 57, 5, 53),
    (42, 26, 38, 22, 41, 25, 37, 21),
)


def _ordered_density_threshold(lane_index: int) -> float:
    """Return a stable 64-level threshold for adjacent hatch carriers.

    Ordered thresholds convert normalized image darkness into actual local line
    density. Black pixels retain every nearby carrier, mid-gray pixels retain a
    proportional subset, and white pixels retain none.
    """
    index = max(0, int(lane_index)) % 64
    row, column = divmod(index, 8)
    return (_DENSITY_BAYER_8[row][column] + 0.5) / 64.0


def _density_lane_index(
    layout: str,
    carrier_index: int,
    point: tuple[float, float],
    center_x: float,
    center_y: float,
    spacing: float,
) -> int:
    # A spiral is represented by one carrier, so use its current turn as the
    # density lane. Other layouts already have one carrier per row/ring/spoke.
    if layout == "spiral":
        radius = math.hypot(point[0] - center_x, point[1] - center_y)
        return int(math.floor(radius / max(spacing, 1e-9)))
    return carrier_index


def _passes_density_gate(darkness: float, cutoff: float, lane_index: int) -> bool:
    if darkness < cutoff:
        return False
    if cutoff >= 1.0:
        return darkness >= 1.0
    normalized = max(0.0, min(1.0, (darkness - cutoff) / (1.0 - cutoff)))
    return normalized >= _ordered_density_threshold(lane_index)


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
    full_fill = str(params.get("generationMode", "hatch")) == "full-fill"
    density_scale = 1.0 - (density_fudge * 0.8)
    # Full Fill deliberately overlaps adjacent pen strokes. The fixed ratio
    # guarantees a step-over strictly smaller than the physical tip width.
    layout_spacing = (
        pen_thickness * 0.8
        if full_fill
        else max(pen_thickness, float(params.get("patternSpacing", pen_thickness * 1.55)) * density_scale)
    )
    if full_fill:
        waveform = "straight"
        density_fudge = 0.0
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
    pixels = rgba.load()
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
            lane_index = _density_lane_index(
                layout, carrier_index, point, center_x, center_y, layout_spacing
            )
            sampled_points += 1
            passes_fill = darkness > 0.0 and darkness >= brightness_cutoff if full_fill else _passes_density_gate(
                darkness, brightness_cutoff, lane_index
            )
            if not passes_fill:
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
                previous_point = None
                continue

            # Re-sample at the actual displaced output coordinate. This keeps
            # high-amplitude zig-zag, sawtooth, sine, and EKG points from
            # wandering into white or transparent regions near artwork edges.
            output_darkness = _sample_darkness(
                pixels, rgba.width, rgba.height, bed_x, bed_y, x, y
            )
            output_darkness = max(
                0.0, min(1.0, output_darkness * (1.0 + density_fudge))
            )
            passes_output_fill = output_darkness > 0.0 and output_darkness >= brightness_cutoff if full_fill else _passes_density_gate(
                output_darkness, brightness_cutoff, lane_index
            )
            if not passes_output_fill:
                if len(current_run) >= 2:
                    completed_paths.append(current_run)
                    preview_pending.append(current_run)
                current_run = []
                previous_point = None
                phase = 0.0
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
        "generation_mode": "full-fill" if full_fill else "hatch",
        "workspace_origin": str(params.get("workspaceOrigin", "top-left")),
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
        "full_fill": full_fill,
        "step_over_mm": round(layout_spacing, 4),
        "pattern_center_x": center_x,
        "pattern_center_y": center_y,
        "pattern_angle": angle_degrees,
        "pattern_clockwise": clockwise,
        "wave_amplitude_mm": wave_amplitude,
        "wave_length_mm": wave_length,
        "brightness_modulation": brightness_mode,
        "density_mapping": "ordered-64-level",
        "live_preview_points": preview_state["points"],
    }


def _generate_single_line_path(
    brightness_map: bytes,
    params: dict[str, Any],
    progress: MutableMapping[str, Any] | None = None,
    preview_queue: Any | None = None,
) -> tuple[list[list[list[float]]], dict[str, Any]]:
    """Raster artwork with one uninterrupted, always-pen-down polyline."""
    bed_x = float(params["bedX"])
    bed_y = float(params["bedY"])
    pen_thickness = float(params.get("penThickness", 0.5))
    density_fudge = float(params.get("densityFudge", 0.0))
    cutoff = float(params.get("brightnessCutoff", DEFAULT_BRIGHTNESS_CUTOFF))
    layout = str(params.get("patternLayout", "linear"))
    waveform = str(params.get("waveform", "zigzag"))
    center_x = float(params.get("patternCenterX", bed_x / 2.0))
    center_y = float(params.get("patternCenterY", bed_y / 2.0))
    angle = float(params.get("patternAngle", 0.0))
    clockwise = bool(params.get("patternClockwise", True))
    spacing = max(pen_thickness, float(params.get("patternSpacing", pen_thickness * 1.55)))
    requested_amplitude = max(0.0, float(params.get("waveAmplitude", spacing * 0.4)))
    # Non-looping textures remain inside their lane, preventing adjacent passes
    # from crossing. Swirl overlap is deliberate density, so it may use full size.
    amplitude = requested_amplitude if waveform == "swirl" else min(requested_amplitude, spacing * 0.45)
    wave_length = max(pen_thickness * 2.0, float(params.get("waveLength", pen_thickness * 6.0)))
    brightness_mode = str(params.get("brightnessModulation", "both"))

    _set_progress(progress, phase="decoding", percent=2.0, detail="Decoding the single-line brightness map...")
    try:
        image = Image.open(io.BytesIO(brightness_map))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise GenerationError(f"Unable to read the generated brightness map: {exc}") from exc
    if image.width * image.height > MAX_BRIGHTNESS_MAP_PIXELS:
        raise GenerationLimitError(f"The brightness map contains more than {MAX_BRIGHTNESS_MAP_PIXELS:,} pixels.")

    rgba = image.convert("RGBA")
    pixel_mm = max(bed_x / max(1, rgba.width - 1), bed_y / max(1, rgba.height - 1))
    sample_step = max(pen_thickness * 0.4, pixel_mm, 0.05)
    source = np.asarray(rgba, dtype=np.uint8)
    samples = source.astype(np.float64)
    luminance = (0.299 * samples[:, :, 0] + 0.587 * samples[:, :, 1] + 0.114 * samples[:, :, 2]) / 255.0
    adjusted_darkness = np.clip(
        (1.0 - luminance) * (samples[:, :, 3] / 255.0) * (1.0 + density_fudge),
        0.0,
        1.0,
    )
    qualifying_y, qualifying_x = np.nonzero(
        (adjusted_darkness >= cutoff) & (adjusted_darkness > 0.0)
    )
    if qualifying_x.size == 0:
        raise GenerationError("The transformed SVG contains no pixels above the brightness cutoff.")

    # Raster only the cutoff-qualified artwork extent, not the machine bed.
    scan_left = (float(qualifying_x.min()) / max(1, rgba.width - 1)) * bed_x
    scan_right = (float(qualifying_x.max()) / max(1, rgba.width - 1)) * bed_x
    scan_top = (float(qualifying_y.min()) / max(1, rgba.height - 1)) * bed_y
    scan_bottom = (float(qualifying_y.max()) / max(1, rgba.height - 1)) * bed_y
    scan_width = max(sample_step, scan_right - scan_left)
    scan_height = max(sample_step, scan_bottom - scan_top)
    local_center_x = min(scan_width, max(0.0, center_x - scan_left))
    local_center_y = min(scan_height, max(0.0, center_y - scan_top))
    carrier_layout = "spiral" if layout == "concentric" else "linear"
    carriers = _build_pattern_carriers(
        carrier_layout, scan_width, scan_height, local_center_x, local_center_y, spacing,
        sample_step, angle, clockwise,
    )

    # Keep only in-bed portions, retaining serpentine carrier order. Joining
    # their endpoints makes one continuous raster path with no pen lifts.
    base: list[tuple[float, float]] = []
    for carrier in carriers:
        if carrier_layout == "spiral":
            # A spiral repeatedly exits and re-enters a rectangular image near
            # its outer turns. Dropping those outside arcs creates long diagonal
            # chords. Clamp them to the boundary so motion follows an edge.
            inside = []
            for point in carrier:
                bounded = (
                    min(scan_right, max(scan_left, point[0] + scan_left)),
                    min(scan_bottom, max(scan_top, point[1] + scan_top)),
                )
                if not inside or math.dist(inside[-1], bounded) > 1e-6:
                    inside.append(bounded)
        else:
            inside = [
                (point[0] + scan_left, point[1] + scan_top)
                for point in carrier
                if 0.0 <= point[0] <= scan_width and 0.0 <= point[1] <= scan_height
            ]
        if len(inside) < 2:
            continue
        if base and math.dist(base[-1], inside[0]) > sample_step:
            base.extend(_sample_segment(base[-1], inside[0], sample_step)[1:])
        base.extend(inside if not base else inside[1:])
    if len(base) < 2:
        raise GenerationError("Unable to fit a continuous raster line inside the workspace.")
    if len(base) > MAX_TOOLPATH_POINTS:
        final_base_point = base[-1]
        stride = int(math.ceil(len(base) / MAX_TOOLPATH_POINTS))
        base = base[::stride]
        if base[-1] != final_base_point:
            base.append(final_base_point)

    darkness_sets, compute_backend = _sample_carrier_darkness(rgba, [base], bed_x, bed_y, progress)
    darkness_values = darkness_sets[0]
    points: list[list[float]] = []
    phase = 0.0
    previous = base[0]
    total = len(base)
    for index, (point, raw_darkness) in enumerate(zip(base, darkness_values, strict=True)):
        if index % 2048 == 0:
            _check_cancel_requested(progress)
            _set_progress(
                progress, phase="path-building", percent=10.0 + (80.0 * index / total),
                completed=index, total=total, detail="Building one continuous raster line...",
                compute_backend=compute_backend,
            )
        darkness = max(0.0, min(1.0, float(raw_darkness) * (1.0 + density_fudge)))
        active_darkness = darkness if darkness >= cutoff else 0.0
        distance = math.dist(previous, point) if index else 0.0
        local_wave_length = wave_length
        if brightness_mode in {"frequency", "both"}:
            local_wave_length = max(pen_thickness * 2.0, wave_length * (1.6 - active_darkness))
        phase += distance / local_wave_length
        previous = point
        before = base[max(0, index - 1)]
        after = base[min(total - 1, index + 1)]
        tangent_x = after[0] - before[0]
        tangent_y = after[1] - before[1]
        tangent_length = math.hypot(tangent_x, tangent_y) or 1.0
        tangent_x /= tangent_length
        tangent_y /= tangent_length
        normal_x, normal_y = -tangent_y, tangent_x
        radius = amplitude * (
            active_darkness if brightness_mode in {"amplitude", "both"} else float(active_darkness > 0.0)
        )
        if waveform == "swirl":
            radians = phase * math.tau
            x = point[0] + radius * ((normal_x * math.sin(radians)) + (tangent_x * math.cos(radians)))
            y = point[1] + radius * ((normal_y * math.sin(radians)) + (tangent_y * math.cos(radians)))
        else:
            offset = _waveform_value(waveform, phase) * radius
            x = point[0] + normal_x * offset
            y = point[1] + normal_y * offset
        points.append([
            round(min(scan_right, max(scan_left, x)), 4),
            round(min(scan_bottom, max(scan_top, y)), 4),
        ])

    if len(points) > MAX_TOOLPATH_POINTS:
        raise GenerationLimitError(f"The single line exceeded {MAX_TOOLPATH_POINTS:,} toolpath points.")
    preview_state = {"points": 0, "chunks": 0}
    _publish_live_preview(preview_queue, [points], preview_state)
    _set_progress(progress, phase="path-building", percent=92.0, completed=total, total=total, detail="Single line ready; compiling G-code...", compute_backend=compute_backend)
    return [points], {
        "source": "browser-brightness-map",
        "generation_mode": "single-line",
        "workspace_origin": str(params.get("workspaceOrigin", "top-left")),
        "map_width": rgba.width,
        "map_height": rgba.height,
        "carrier_count": 1,
        "continuous_paths": 1,
        "pen_lifts_during_image": 0,
        "sample_step_mm": round(sample_step, 4),
        "pattern_spacing_mm": round(spacing, 4),
        "scan_bounds_mm": [
            round(scan_left, 4), round(scan_top, 4),
            round(scan_right, 4), round(scan_bottom, 4),
        ],
        "compute_backend": compute_backend,
        "gpu_accelerated": compute_backend == "cuda",
        "live_preview_points": preview_state["points"],
    }

def _gcode_comment_value(value: Any) -> str:
    text = str(value if value is not None else "")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def _gcode_comment_lines(value: Any) -> list[str]:
    """Return semicolon comments that fit strict controller line limits."""
    text = _gcode_comment_value(value)
    if not text:
        return [";"]

    return textwrap.wrap(
        text,
        width=GCODE_MAX_LINE_LENGTH,
        initial_indent="; ",
        subsequent_indent="; ",
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
        drop_whitespace=True,
    )


def _workspace_output_point(x: float, y: float, params: dict[str, Any]) -> tuple[float, float]:
    bed_x = float(params["bedX"])
    bed_y = float(params["bedY"])
    origin = str(params.get("workspaceOrigin", "top-left"))
    output_x = bed_x - x if origin.endswith("right") else x
    output_y = bed_y - y if origin.startswith("bottom") else y
    return output_x, output_y


def _clean_output_stem(filename: Any) -> str:
    source_name = os.path.splitext(os.path.basename(str(filename or "hatchplot")))[0]
    cleaned = "".join(
        character
        if ((character.isascii() and character.isalnum()) or character in "-_")
        else "-"
        for character in source_name
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_")[:8] or "hatchplt"


def _suggest_output_filename(params: dict[str, Any]) -> str:
    origin_codes = {
        "top-left": "TL",
        "top-right": "TR",
        "bottom-left": "BL",
        "bottom-right": "BR",
    }
    generation_codes = {
        "outline": "OUTLINE",
        "hatch": "HATCH",
        "full-fill": "FILL",
        "single-line": "1LINE",
        "outline-hatch": "OTH",
    }
    layout_labels = {
        "linear": "Linear",
        "spiral": "Spiral",
        "concentric": "Concentric",
        "radial": "Radial",
    }
    generation_mode = str(params.get("generationMode", "hatch"))
    parts = [
        _clean_output_stem(params.get("sourceFilename", "hatchplot.svg")),
        origin_codes.get(str(params.get("workspaceOrigin", "top-left")), "TL"),
        generation_codes.get(generation_mode, generation_mode.upper() or "HATCH"),
    ]
    if generation_mode in {"hatch", "full-fill", "single-line", "outline-hatch"}:
        layout = str(params.get("patternLayout", "")).strip().lower()
        if layout:
            parts.append(layout_labels.get(layout, layout[:1].upper() + layout[1:]))
    return "-".join(parts) + ".nc"


def _format_machining_duration(seconds: float) -> str:
    rounded_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds_part}s"
    if minutes:
        return f"{minutes}m {seconds_part}s"
    return f"{seconds_part}s"


def _estimate_machining_time(paths: list[list[list[float]]], params: dict[str, Any]) -> dict[str, Any]:
    feed_rate_mm_min = max(1.0, float(params.get("xyFeedRate", 2000)))
    current_x = 0.0
    current_y = 0.0
    draw_distance = 0.0
    travel_distance = 0.0
    toolpath_count = 0

    for path in paths:
        if not path:
            continue
        first_x, first_y = _workspace_output_point(float(path[0][0]), float(path[0][1]), params)
        travel_distance += math.hypot(first_x - current_x, first_y - current_y)
        previous_x, previous_y = first_x, first_y
        for x, y in path[1:]:
            output_x, output_y = _workspace_output_point(float(x), float(y), params)
            draw_distance += math.hypot(output_x - previous_x, output_y - previous_y)
            previous_x, previous_y = output_x, output_y
        current_x, current_y = previous_x, previous_y
        toolpath_count += 1

    if toolpath_count:
        travel_distance += math.hypot(current_x, current_y)

    xy_motion_seconds = ((draw_distance + travel_distance) / feed_rate_mm_min) * 60.0
    pen_action_count = toolpath_count * 2
    pen_delay_seconds = pen_action_count * PEN_ACTION_DELAY_SECONDS
    total_seconds = xy_motion_seconds + pen_delay_seconds
    return {
        "estimated_machining_time_seconds": round(total_seconds, 1),
        "estimated_machining_time": _format_machining_duration(total_seconds),
        "estimated_xy_motion_seconds": round(xy_motion_seconds, 1),
        "estimated_pen_delay_seconds": round(pen_delay_seconds, 1),
        "pen_action_delay_seconds": PEN_ACTION_DELAY_SECONDS,
        "pen_action_count": pen_action_count,
        "draw_distance_mm": round(draw_distance, 2),
        "travel_distance_mm": round(travel_distance, 2),
    }


def _gcode_preamble(
    params: dict[str, Any],
    path_count: int,
    machining_estimate: dict[str, Any] | None = None,
) -> list[str]:
    layers = params.get("enabledLayers") or []
    if isinstance(layers, str):
        layers_text = layers
    else:
        layers_text = ", ".join(_gcode_comment_value(layer) for layer in layers) or "All visible layers"

    generation_mode = str(params.get("generationMode", "hatch"))
    workspace_origin = str(params.get("workspaceOrigin", "top-left"))
    display_svg_x, display_svg_y = _workspace_output_point(float(params["svgPosX"]), float(params["svgPosY"]), params)
    display_center_x, display_center_y = _workspace_output_point(
        float(params.get("patternCenterX", 0.0)),
        float(params.get("patternCenterY", 0.0)),
        params,
    )
    direction = "clockwise" if bool(params.get("patternClockwise", True)) else "counterclockwise"
    comments = [
        "HatchPlot generated G-code",
        f"Output file: {_suggest_output_filename(params)}",
        f"Source SVG: {_gcode_comment_value(params.get('sourceFilename', 'uploaded.svg'))}",
        f"Enabled layers: {_gcode_comment_value(layers_text)}",
        f"Machine bed: {float(params['bedX']):.3f} x {float(params['bedY']):.3f} mm",
        f"Workspace origin: {workspace_origin}",
        f"Generation mode: {generation_mode}",
        f"Z control: {_gcode_comment_value(params['zMode'])}; up={_gcode_comment_value(params['zUp'])}; down={_gcode_comment_value(params['zDown'])}; plunge={int(params['zPlungeRate'])} mm/min",
        f"XY feed rate: {int(params['xyFeedRate'])} mm/min",
        f"Pen size: {float(params.get('penThickness', 0.5)):.3f} mm",
        f"SVG transform: scale={float(params['svgScale']):.3f}% ({_gcode_comment_value(params.get('svgScaleMode', 'fit-relative'))}); rotation={float(params['svgRotate']):.3f} deg; center=({display_svg_x:.3f}, {display_svg_y:.3f}) mm",
    ]
    if generation_mode in {"outline", "outline-hatch"}:
        outline_method = str(params.get("outlineTraceMethod", "boundary"))
        comments.append(
            "Outline: scan-engine centerline extraction from visible artwork"
            if outline_method == "centerline"
            else "Outline: native SVG strokes and filled-shape vector boundaries"
        )
    if generation_mode in {"hatch", "full-fill", "single-line", "outline-hatch"}:
        effective_density_fudge = 0.0 if generation_mode == "full-fill" else float(params.get("densityFudge", 0.0))
        effective_modulation = "none" if generation_mode == "full-fill" else params.get("brightnessModulation", "both")
        effective_waveform = "straight" if generation_mode == "full-fill" else params.get("waveform", "zigzag")
        spacing = (
            float(params.get("penThickness", 0.5)) * 0.8
            if generation_mode == "full-fill"
            else float(params.get("patternSpacing", 1.0))
        )
        comments.extend([
            f"Brightness: cutoff={float(params.get('brightnessCutoff', DEFAULT_BRIGHTNESS_CUTOFF)):.3f}; density fudge={effective_density_fudge:+.3f}; modulation={_gcode_comment_value(effective_modulation)}",
            f"Pattern: layout={_gcode_comment_value(params.get('patternLayout', 'linear'))}; spacing={spacing:.3f} mm; angle={float(params.get('patternAngle', 0.0)):.3f} deg; center=({display_center_x:.3f}, {display_center_y:.3f}) mm; direction={direction}",
            f"Waveform: type={_gcode_comment_value(effective_waveform)}; amplitude={0.0 if generation_mode == 'full-fill' else float(params.get('waveAmplitude', 0.5)):.3f} mm; wavelength={float(params.get('waveLength', 3.0)):.3f} mm",
        ])
        if generation_mode == "full-fill":
            comments.append("Full Fill: straight overlapping strokes; step-over is 80% of pen size")
            comments.append("Sequence: SVG boundary outlines are plotted first, followed by Full Fill strokes")
    if generation_mode == "outline-hatch":
        comments.append(
            "Sequence: selected outline traces are plotted first, followed "
            "by brightness-driven hatch paths"
        )
    if machining_estimate:
        comments.append(
            "Estimated machining time: "
            f"{machining_estimate['estimated_machining_time']} "
            f"(XY at {int(float(params.get('xyFeedRate', 2000)))} mm/min; "
            f"{PEN_ACTION_DELAY_SECONDS:.1f}s per pen action)"
        )
        comments.append(
            "Estimated motion: "
            f"{float(machining_estimate['draw_distance_mm']):.2f} mm drawing; "
            f"{float(machining_estimate['travel_distance_mm']):.2f} mm pen-up travel"
        )
    comments.extend([
        f"Toolpaths: {path_count}",
        "End HatchPlot header",
    ])

    lines = [line for comment in comments for line in _gcode_comment_lines(comment)]
    lines.extend(_custom_gcode_lines(params.get("startGcode", "")))
    lines.extend([
        "G21",
        "G90",
        f"G0 Z{params['zUp']}" if params["zMode"] == "stepper" else f"M3 S{params['zUp']}",
    ])
    return lines


def _custom_gcode_lines(value: Any) -> list[str]:
    """Normalize saved multi-line machine commands without changing their content."""
    return [line.rstrip("\r") for line in str(value or "").splitlines() if line.strip()]


def _compile_gcode(
    paths: list[list[list[float]]],
    params: dict[str, Any],
    machining_estimate: dict[str, Any] | None = None,
) -> list[str]:
    z_mode = str(params["zMode"])
    z_up = str(params["zUp"])
    z_down = str(params["zDown"])
    xy_feed_rate = int(params["xyFeedRate"])
    z_plunge_rate = int(params["zPlungeRate"])

    if machining_estimate is None:
        machining_estimate = _estimate_machining_time(paths, params)
    gcode = _gcode_preamble(params, len(paths), machining_estimate)
    for path in paths:
        first_x, first_y = _workspace_output_point(float(path[0][0]), float(path[0][1]), params)
        gcode.append(f"G0 X{first_x:.2f} Y{first_y:.2f}")
        gcode.append(f"G1 Z{z_down} F{z_plunge_rate}" if z_mode == "stepper" else f"M3 S{z_down}")
        for point_index, (x, y) in enumerate(path[1:]):
            output_x, output_y = _workspace_output_point(float(x), float(y), params)
            feed = f" F{xy_feed_rate}" if point_index == 0 else ""
            gcode.append(f"G1 X{output_x:.2f} Y{output_y:.2f}{feed}")
        gcode.append(f"G0 Z{z_up}" if z_mode == "stepper" else f"M3 S{z_up}")
    gcode.append("G0 X0 Y0")
    gcode.extend(_custom_gcode_lines(params.get("endGcode", "")))
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

    generation_mode = str(params.get("generationMode", "hatch"))
    if generation_mode == "single-line" and brightness_map is None:
        raise GenerationError("Single Line Raster requires the browser-rendered brightness map.")
    if generation_mode == "outline" and params.get("outlineTraceMethod", "boundary") == "centerline":
        if brightness_map is None:
            raise GenerationError("Centerline outline tracing requires the browser-rendered artwork map.")
        compiled_paths, outline_stats = _generate_outline_paths_from_raster(
            brightness_map, params, progress, preview_queue
        )
        _set_progress(
            progress,
            phase="gcode",
            percent=94.0,
            completed=0,
            total=len(compiled_paths),
            detail="Compiling centerline traces into G-code...",
            compute_backend="numpy-cpu",
        )
        machining_estimate = _estimate_machining_time(compiled_paths, params)
        gcode = _compile_gcode(compiled_paths, params, machining_estimate)
        duration = time.perf_counter() - started
        stats = {
            **machining_estimate,
            **outline_stats,
            "output_filename": _suggest_output_filename(params),
            "duration_seconds": round(duration, 3),
            "drawable_elements": None,
            "outline_paths": len(compiled_paths),
            "hatch_paths": 0,
            "continuous_paths": len(compiled_paths),
            "generation_mode": "outline",
            "workspace_origin": str(params.get("workspaceOrigin", "top-left")),
            "toolpath_points": sum(len(path) for path in compiled_paths),
            "gcode_lines": len(gcode),
            "gcode_header_lines": len(_gcode_preamble(params, len(compiled_paths), machining_estimate)),
            "pen_thickness_mm": float(params.get("penThickness", 0.5)),
        }
        _set_progress(
            progress,
            phase="completed",
            percent=100.0,
            completed=len(compiled_paths),
            total=len(compiled_paths),
            detail="Centerline trace generation completed.",
            compute_backend="numpy-cpu",
            force_eta_zero=True,
        )
        return {"gcode": "\n".join(gcode), "paths": compiled_paths, "stats": stats}

    if generation_mode == "full-fill" and not params.get("_fullFillPassOnly"):
        _set_progress(
            progress,
            phase="outline-processing",
            percent=3.0,
            detail="Tracing SVG boundaries before Full Fill...",
            compute_backend="geos-cpu",
        )
        outline_params = dict(params)
        outline_params["generationMode"] = "outline"
        outline_params["outlineTraceMethod"] = "boundary"
        outline_result = generate_toolpath(svg_content, outline_params, None, None, None)
        outline_paths = list(outline_result.get("paths") or [])

        fill_params = dict(params)
        fill_params["_fullFillPassOnly"] = True
        fill_result = generate_toolpath(svg_content, fill_params, None, None, None)
        fill_paths = list(fill_result.get("paths") or [])
        fill_stats = dict(fill_result.get("stats") or {})
        compiled_paths = outline_paths + fill_paths
        toolpath_points = sum(len(path) for path in compiled_paths)
        if len(compiled_paths) > MAX_HATCH_PATHS:
            raise GenerationLimitError(
                f"The outline and Full Fill result generated more than {MAX_HATCH_PATHS:,} toolpaths."
            )
        if toolpath_points > MAX_TOOLPATH_POINTS:
            raise GenerationLimitError(
                f"The outline and Full Fill result exceeded {MAX_TOOLPATH_POINTS:,} toolpath points."
            )

        preview_state = {"points": 0, "chunks": 0}
        preview_pending: list[list[list[float]]] = []
        for path in compiled_paths:
            preview_pending.append(path)
            if sum(len(item) for item in preview_pending) >= LIVE_PREVIEW_CHUNK_POINTS:
                _publish_live_preview(preview_queue, preview_pending, preview_state)
                preview_pending = []
        _publish_live_preview(preview_queue, preview_pending, preview_state)

        _set_progress(
            progress,
            phase="gcode",
            percent=94.0,
            completed=0,
            total=len(compiled_paths),
            detail="Compiling outline-first and Full-Fill-second paths into G-code...",
            compute_backend="geos-cpu",
        )
        machining_estimate = _estimate_machining_time(compiled_paths, params)
        gcode = _compile_gcode(compiled_paths, params, machining_estimate)
        duration = time.perf_counter() - started
        stats = {
            **fill_stats,
            **machining_estimate,
            "output_filename": _suggest_output_filename(params),
            "duration_seconds": round(duration, 3),
            "outline_paths": len(outline_paths),
            "hatch_paths": len(fill_paths),
            "continuous_paths": len(compiled_paths),
            "toolpath_points": toolpath_points,
            "gcode_lines": len(gcode),
            "gcode_header_lines": len(_gcode_preamble(params, len(compiled_paths), machining_estimate)),
            "generation_mode": "full-fill",
            "path_sequence": "outline-then-full-fill",
            "live_preview_points": preview_state["points"],
        }
        _set_progress(
            progress,
            phase="completed",
            percent=100.0,
            completed=len(compiled_paths),
            total=len(compiled_paths),
            detail="Outline then Full Fill generation completed.",
            compute_backend="geos-cpu",
            force_eta_zero=True,
        )
        return {"gcode": "\n".join(gcode), "paths": compiled_paths, "stats": stats}

    if generation_mode == "outline-hatch":
        if brightness_map is None:
            raise GenerationError("Outline then Hatch requires the browser-rendered brightness map.")

        _set_progress(
            progress,
            phase="outline-processing",
            percent=3.0,
            detail="Tracing native SVG outlines before brightness hatching...",
            compute_backend="geos-cpu",
        )
        _check_cancel_requested(progress)
        outline_params = dict(params)
        outline_params["generationMode"] = "outline"
        outline_result = generate_toolpath(
            svg_content,
            outline_params,
            brightness_map if outline_params.get("outlineTraceMethod") == "centerline" else None,
            None,
            None,
        )
        outline_paths = list(outline_result.get("paths") or [])
        outline_stats = dict(outline_result.get("stats") or {})
        _check_cancel_requested(progress)

        outline_preview_state = {"points": 0, "chunks": 0}
        outline_preview_pending: list[list[list[float]]] = []
        outline_preview_points = 0
        outline_pending_points = 0
        for outline_path in outline_paths:
            outline_preview_pending.append(outline_path)
            outline_preview_points += len(outline_path)
            outline_pending_points += len(outline_path)
            if outline_pending_points >= LIVE_PREVIEW_CHUNK_POINTS:
                _publish_live_preview(preview_queue, outline_preview_pending, outline_preview_state)
                outline_preview_pending = []
                outline_pending_points = 0
        _publish_live_preview(preview_queue, outline_preview_pending, outline_preview_state)

        hatch_params = dict(params)
        hatch_params["generationMode"] = "hatch"
        hatch_paths, raster_stats = _generate_brightness_paths(
            brightness_map, hatch_params, progress, preview_queue
        )
        compiled_paths = outline_paths + hatch_paths
        toolpath_points = sum(len(path) for path in compiled_paths)
        if len(compiled_paths) > MAX_HATCH_PATHS:
            raise GenerationLimitError(
                f"The combined outline and hatch result generated more than {MAX_HATCH_PATHS:,} toolpaths."
            )
        if toolpath_points > MAX_TOOLPATH_POINTS:
            raise GenerationLimitError(
                f"The combined outline and hatch result exceeded {MAX_TOOLPATH_POINTS:,} toolpath points."
            )

        _set_progress(
            progress,
            phase="gcode",
            percent=94.0,
            completed=0,
            total=len(compiled_paths),
            detail="Compiling outline-first and hatch-second paths into G-code...",
            compute_backend=raster_stats.get("compute_backend"),
        )
        _check_cancel_requested(progress)
        machining_estimate = _estimate_machining_time(compiled_paths, params)
        gcode = _compile_gcode(compiled_paths, params, machining_estimate)
        duration = time.perf_counter() - started
        combined_stats = {
            **machining_estimate,
            "output_filename": _suggest_output_filename(params),
            **raster_stats,
            "duration_seconds": round(duration, 3),
            "drawable_elements": outline_stats.get("drawable_elements"),
            "outline_paths": len(outline_paths),
            "hatch_paths": len(hatch_paths),
            "continuous_paths": len(compiled_paths),
            "toolpath_points": toolpath_points,
            "gcode_lines": len(gcode),
            "gcode_header_lines": len(_gcode_preamble(params, len(compiled_paths), machining_estimate)),
            "pen_thickness_mm": float(params.get("penThickness", 0.5)),
            "generation_mode": "outline-hatch",
            "workspace_origin": str(params.get("workspaceOrigin", "top-left")),
            "outline_sampling_mm": outline_stats.get(
                "outline_sampling_mm",
                max(0.05, float(params.get("penThickness", 0.5)) * 0.35),
            ),
            "outline_trace_source": outline_stats.get("outline_trace_source", "svg-vector-geometry"),
            "outline_trace_method": outline_stats.get("outline_trace_method", "vector-boundary"),
            "path_sequence": "outline-then-hatch",
            "live_preview_points": outline_preview_points + int(raster_stats.get("live_preview_points", 0)),
        }
        result = {
            "gcode": "\n".join(gcode),
            "paths": compiled_paths,
            "stats": combined_stats,
        }
        _set_progress(
            progress,
            phase="completed",
            percent=100.0,
            completed=len(compiled_paths),
            total=len(compiled_paths),
            detail="Outline then Hatch toolpath generation completed.",
            compute_backend=raster_stats.get("compute_backend"),
            force_eta_zero=True,
        )
        return result

    if generation_mode in {"hatch", "single-line"} and brightness_map is not None:
        generator = _generate_single_line_path if generation_mode == "single-line" else _generate_brightness_paths
        compiled_paths, raster_stats = generator(brightness_map, params, progress, preview_queue)
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
        machining_estimate = _estimate_machining_time(compiled_paths, params)
        gcode = _compile_gcode(compiled_paths, params, machining_estimate)
        duration = time.perf_counter() - started
        toolpath_points = sum(len(path) for path in compiled_paths)
        result = {
            "gcode": "\n".join(gcode),
            "paths": compiled_paths,
            "stats": {
                **machining_estimate,
                "output_filename": _suggest_output_filename(params),
                "duration_seconds": round(duration, 3),
                "drawable_elements": None,
                "hatch_paths": len(compiled_paths) if generation_mode in {"hatch", "full-fill"} else 0,
                "continuous_paths": len(compiled_paths),
                "toolpath_points": toolpath_points,
                "gcode_lines": len(gcode),
                "gcode_header_lines": len(_gcode_preamble(params, len(compiled_paths), machining_estimate)),
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

    parsed_width = float(getattr(parsed_svg, "width", 0.0) or 0.0)
    parsed_height = float(getattr(parsed_svg, "height", 0.0) or 0.0)
    viewbox = getattr(parsed_svg, "viewbox", None)
    viewbox_values: tuple[float, float, float, float] | None = None
    if viewbox is not None:
        try:
            viewbox_values = (
                float(viewbox.x),
                float(viewbox.y),
                float(viewbox.width),
                float(viewbox.height),
            )
        except (AttributeError, TypeError, ValueError):
            viewbox_values = None

    # Exact Bezier extrema require numerical root solving for every segment.
    # Traced SVGs can contain tens of thousands of curves, making bbox() slower
    # than the actual toolpath work. For outline tracing the viewport is the
    # correct clipping/reference frame and is available without curve analysis.
    if (
        generation_mode in {"outline", "full-fill"}
        and viewbox_values is not None
        and all(math.isfinite(value) for value in viewbox_values)
        and viewbox_values[2] > 0.0
        and viewbox_values[3] > 0.0
    ):
        min_x, min_y = viewbox_values[0], viewbox_values[1]
        svg_width, svg_height = viewbox_values[2], viewbox_values[3]
        max_x, max_y = min_x + svg_width, min_y + svg_height
    else:
        bbox = parsed_svg.bbox()
        if bbox is None or len(bbox) != 4:
            raise GenerationError("The SVG does not contain drawable geometry.")
        if not all(math.isfinite(float(value)) for value in bbox):
            raise GenerationError("The SVG has invalid or non-finite bounds.")
        min_x, min_y, max_x, max_y = map(float, bbox)
        svg_width = max_x - min_x
        svg_height = max_y - min_y
    if generation_mode in {"outline", "full-fill"}:
        # Valid line art may be a single horizontal or vertical open stroke, whose
        # geometry bounds have zero height or width. Only reject a point-like SVG.
        if svg_width <= 0 and svg_height <= 0:
            raise GenerationError("The SVG does not contain a traceable line or boundary.")
    elif svg_width <= 0 or svg_height <= 0:
        raise GenerationError("The SVG width and height must be greater than zero.")

    bed_x = float(params["bedX"])
    bed_y = float(params["bedY"])
    fit_scale = min(
        (bed_x * 0.9) / max(svg_width, 1e-9),
        (bed_y * 0.9) / max(svg_height, 1e-9),
    )
    requested_scale = float(params["svgScale"]) / 100.0
    scale_mode = str(params.get("svgScaleMode", "fit-relative"))
    source_width_mm = float(params.get("sourceWidthMm", 0.0) or 0.0)
    source_height_mm = float(params.get("sourceHeightMm", 0.0) or 0.0)
    # Browser outline jobs include the imported physical size because SVG parsers
    # normalize CSS units to px. This maps parsed coordinates back to millimeters
    # before applying the user's percentage scale.
    if generation_mode in {"outline", "full-fill"} and parsed_width > 0.0 and parsed_height > 0.0:
        if source_width_mm > 0.0 and source_height_mm > 0.0:
            physical_scale = min(source_width_mm / parsed_width, source_height_mm / parsed_height)
        else:
            # svgelements normalizes SVG/CSS lengths to 96-DPI CSS pixels.
            physical_scale = 25.4 / 96.0
        scale = physical_scale * requested_scale
    else:
        # New browser clients use absolute percentage scaling: 100% preserves the
        # SVG's imported size. Older API clients retain the previous fit-relative
        # behavior unless they explicitly request absolute scaling.
        scale = requested_scale if scale_mode == "absolute" else fit_scale * requested_scale
    source_center = (
        (parsed_width / 2.0, parsed_height / 2.0)
        if generation_mode in {"outline", "full-fill"} and parsed_width > 0.0 and parsed_height > 0.0
        else ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    )
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
        detail=f"Processing {total_elements:,} SVG elements for {generation_mode} generation...",
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
                detail=f"Processing SVG element {element_index + 1:,} of {total_elements:,} for {generation_mode} generation...",
                compute_backend="geos-cpu",
            )
        fill = getattr(element, "fill", None)
        stroke = getattr(element, "stroke", None)

        color = fill if fill is not None and fill != "none" else stroke
        path_element = element if isinstance(element, Path) else Path(element)

        if generation_mode == "outline":
            target_step_mm = max(0.05, float(params.get("penThickness", 0.5)) * 0.35)
            source_step = target_step_mm / max(abs(scale), 1e-9)
            source_geometries: list[Any] = path_to_centerlines(path_element, source_step)
            if not source_geometries:
                continue
            drawable_elements += 1
            transformed_lines: list[LineString] = []
            simplify_tolerance = max(0.01, float(params.get("penThickness", 0.5)) * 0.04)
            for source_geometry in source_geometries:
                transformed = _transform_svg_geometry(source_geometry, source_center, scale, rotation, destination)
                clipped = transformed.intersection(machine_bed)
                for clipped_line in _iter_lines(clipped):
                    simplified = clipped_line.simplify(simplify_tolerance, preserve_topology=False)
                    if isinstance(simplified, LineString) and not simplified.is_empty and len(simplified.coords) >= 2:
                        transformed_lines.append(simplified)
            clipped_grid: Any = MultiLineString(transformed_lines) if transformed_lines else MultiLineString([])
        else:
            polygon = path_to_shapely(path_element)
            if polygon is None:
                continue
            drawable_elements += 1
            polygon = _transform_svg_geometry(polygon, source_center, scale, rotation, destination)
            safe_polygon = polygon.intersection(machine_bed)
            if safe_polygon.is_empty:
                continue
            if generation_mode == "full-fill":
                spacing = float(params.get("penThickness", 0.5)) * 0.8
            else:
                density_fudge = float(params.get("densityFudge", 0.0))
                density_scale = 1.0 - (density_fudge * 0.8)
                spacing = max(
                    float(params.get("penThickness", 0.5)),
                    (0.5 + (get_luminance(color) * 4.5)) * density_scale,
                )
            hatch_grid = create_hatch_lines(
                safe_polygon.bounds,
                spacing,
                float(params.get("patternAngle", 45.0)),
            )
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
                    f"The SVG generated more than {MAX_HATCH_PATHS:,} toolpaths. "
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
        raise GenerationError("No SVG outlines could be traced." if generation_mode == "outline" else "No filled or stroked SVG paths could be converted.")

    _set_progress(
        progress,
        phase="gcode",
        percent=94.0,
        completed=0,
        total=len(compiled_paths),
        detail=f"Compiling {generation_mode} paths into G-code...",
        compute_backend="geos-cpu",
    )
    _check_cancel_requested(progress)
    machining_estimate = _estimate_machining_time(compiled_paths, params)
    gcode = _compile_gcode(compiled_paths, params, machining_estimate)

    duration = time.perf_counter() - started
    stats: dict[str, Any] = {
        **machining_estimate,
        "output_filename": _suggest_output_filename(params),
        "duration_seconds": round(duration, 3),
        "drawable_elements": drawable_elements,
        "hatch_paths": len(compiled_paths) if generation_mode in {"hatch", "full-fill"} else 0,
        "outline_paths": len(compiled_paths) if generation_mode == "outline" else 0,
        "continuous_paths": len(compiled_paths),
        "generation_mode": generation_mode,
        "workspace_origin": str(params.get("workspaceOrigin", "top-left")),
        "toolpath_points": toolpath_points,
        "gcode_lines": len(gcode),
        "gcode_header_lines": len(_gcode_preamble(params, len(compiled_paths), machining_estimate)),
        "scale_mode": scale_mode,
        "scale_percent": float(params["svgScale"]),
        "source_width": round(source_width_mm if generation_mode in {"outline", "full-fill"} and source_width_mm > 0.0 else svg_width, 4),
        "source_height": round(source_height_mm if generation_mode in {"outline", "full-fill"} and source_height_mm > 0.0 else svg_height, 4),
        "compute_backend": "geos-cpu",
        "gpu_accelerated": False,
        "live_preview_points": preview_state["points"],
    }
    if generation_mode in {"hatch", "full-fill"}:
        stats.update({
            "density_fudge": float(params.get("densityFudge", 0.0)),
            "brightness_cutoff": float(params.get("brightnessCutoff", DEFAULT_BRIGHTNESS_CUTOFF)),
        })
        if generation_mode == "full-fill":
            stats.update({
                "full_fill": True,
                "step_over_mm": round(float(params.get("penThickness", 0.5)) * 0.8, 4),
                "pattern_spacing_mm": round(float(params.get("penThickness", 0.5)) * 0.8, 4),
                "pattern_angle": float(params.get("patternAngle", 45.0)),
                "waveform": "straight",
            })
    else:
        stats.update({
            "outline_sampling_mm": max(0.05, float(params.get("penThickness", 0.5)) * 0.35),
            "outline_trace_source": "svg-vector-geometry",
            "outline_trace_method": str(params.get("outlineTraceMethod", "boundary")),
        })

    result = {
        "gcode": "\n".join(gcode),
        "paths": compiled_paths,
        "stats": stats,
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
    sourceWidthMm: float,
    sourceHeightMm: float,
    zMode: str,
    zUp: str,
    zDown: str,
    xyFeedRate: int,
    zPlungeRate: int,
    penThickness: float,
    densityFudge: float,
    brightnessCutoff: float,
    generationMode: str,
    outlineTraceMethod: str,
    workspaceOrigin: str,
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
    startGcode: str,
    endGcode: str,
) -> dict[str, Any]:
    return {
        "bedX": bedX,
        "bedY": bedY,
        "svgScale": svgScale,
        "svgScaleMode": svgScaleMode,
        "svgRotate": svgRotate,
        "svgPosX": svgPosX,
        "svgPosY": svgPosY,
        "sourceWidthMm": sourceWidthMm,
        "sourceHeightMm": sourceHeightMm,
        "zMode": zMode,
        "zUp": zUp,
        "zDown": zDown,
        "xyFeedRate": xyFeedRate,
        "zPlungeRate": zPlungeRate,
        "penThickness": penThickness,
        "densityFudge": densityFudge,
        "brightnessCutoff": brightnessCutoff,
        "generationMode": generationMode,
        "outlineTraceMethod": outlineTraceMethod,
        "workspaceOrigin": workspaceOrigin,
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
        "startGcode": startGcode,
        "endGcode": endGcode,
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


def _cleanup_converter_transfers(now: float | None = None) -> None:
    current = time.time() if now is None else now
    with converter_transfers_lock:
        expired = [
            transfer_id
            for transfer_id, record in converter_transfers.items()
            if current - float(record.get("created_at", 0.0)) > CONVERTER_TRANSFER_TTL_SECONDS
        ]
        for transfer_id in expired:
            converter_transfers.pop(transfer_id, None)
        while len(converter_transfers) > MAX_CONVERTER_TRANSFERS:
            oldest = min(
                converter_transfers,
                key=lambda key: float(converter_transfers[key].get("created_at", 0.0)),
            )
            converter_transfers.pop(oldest, None)


def _store_converter_transfer(svg: str, filename: str, stats: dict[str, Any]) -> str:
    _cleanup_converter_transfers()
    transfer_id = uuid.uuid4().hex
    with converter_transfers_lock:
        converter_transfers[transfer_id] = {
            "svg": svg,
            "filename": filename,
            "stats": stats,
            "created_at": time.time(),
        }
    _cleanup_converter_transfers()
    return transfer_id


def vectorize_raster_image(content: bytes, filename: str, settings: dict[str, Any]) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            source_image = source.convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        raise GenerationError("The uploaded file is not a readable raster image.") from exc

    source_width, source_height = source_image.size
    if source_width < 1 or source_height < 1:
        raise GenerationError("The source image has invalid dimensions.")
    if source_width * source_height > MAX_BRIGHTNESS_MAP_PIXELS:
        raise GenerationLimitError(
            f"The source image contains more than {MAX_BRIGHTNESS_MAP_PIXELS:,} pixels."
        )

    engine = str(settings.get("engine", "linedraw")).strip().lower()
    engines = converter_engine_status()
    if engine not in engines:
        raise GenerationError("Unknown vectorization engine.")
    if not engines[engine].get("available"):
        reason = str(engines[engine].get("reason", "")).strip()
        detail = f" {reason}" if reason else " Rebuild HatchPlot with the GPL converter profile."
        raise GenerationError(f"{engines[engine].get('label', engine)} is unavailable.{detail}")

    common = CommonRasterSettings(
        output_width_mm=max(1.0, min(5000.0, float(settings.get("outputWidth", 150.0)))),
        max_dimension=max(64, min(4096, int(settings.get("maxDimension", 1024)))),
        auto_contrast_cutoff=max(0.0, min(20.0, float(settings.get("autoContrastCutoff", 2.0)))),
        blur_radius=max(0, min(12, int(settings.get("blurRadius", 1)))),
        alpha_threshold=max(0, min(255, int(settings.get("alphaThreshold", 16)))),
        invert=bool(settings.get("invert", False)),
        white_background=bool(settings.get("whiteBackground", True)),
    )

    if engine == "linedraw":
        line_settings = LineDrawSettings(
            mode=str(settings.get("mode", "contour-hatch")),
            output_width_mm=common.output_width_mm,
            max_dimension=max(128, common.max_dimension),
            auto_contrast_cutoff=common.auto_contrast_cutoff,
            blur_radius=common.blur_radius,
            alpha_threshold=common.alpha_threshold,
            invert=common.invert,
            white_background=common.white_background,
            contour_low_threshold=max(1, min(254, int(settings.get("contourLowThreshold", 70)))),
            contour_high_threshold=max(2, min(255, int(settings.get("contourHighThreshold", 180)))),
            contour_simplify=max(0.0, min(20.0, float(settings.get("contourSimplify", 1.5)))),
            minimum_contour_length=max(0.0, min(100000.0, float(settings.get("minimumContourLength", 10.0)))),
            hatch_size=max(4, min(128, int(settings.get("hatchSize", 16)))),
            hatch_light_threshold=max(1, min(254, int(settings.get("hatchLightThreshold", 160)))),
            hatch_mid_threshold=max(0, min(254, int(settings.get("hatchMidThreshold", 96)))),
            hatch_dark_threshold=max(0, min(254, int(settings.get("hatchDarkThreshold", 40)))),
            sort_strokes=bool(settings.get("sortStrokes", True)),
            stroke_width_mm=max(0.05, min(10.0, float(settings.get("strokeWidthMm", 0.35)))),
            maximum_strokes=min(MAX_HATCH_PATHS, 30000),
            maximum_points=MAX_TOOLPATH_POINTS,
        )
        try:
            result = convert_image(source_image, filename, line_settings)
        except ValueError as exc:
            raise GenerationError(str(exc)) from exc
        engine_suffix = f"linedraw-{line_settings.mode}"
        stats = {
            "mode": line_settings.mode,
            "source_width": source_width,
            "source_height": source_height,
            "trace_width": result.width,
            "trace_height": result.height,
            "output_width_mm": round(result.output_width_mm, 4),
            "output_height_mm": round(result.output_height_mm, 4),
            "path_count": result.stroke_count,
            "contour_paths": len(result.contours),
            "hatch_paths": len(result.hatches),
            "point_count": result.point_count,
            "pen_up_travel_px": round(result.travel_distance_px, 3),
            "engine": "linedraw",
            "engine_detail": "Linedraw-inspired plotter polylines",
        }
        svg = result.svg
    elif engine == "inkscape":
        inkscape_settings = InkscapeTraceSettings(
            scans=max(2, min(256, int(settings.get("inkscapeScans", 8)))),
            smooth=bool(settings.get("inkscapeSmooth", True)),
            stack=bool(settings.get("inkscapeStack", True)),
            remove_background=bool(settings.get("inkscapeRemoveBackground", True)),
            speckles=max(0, min(100000, int(settings.get("inkscapeSpeckles", 2)))),
            smooth_corners=max(0.0, min(1.334, float(settings.get("inkscapeSmoothCorners", 1.0)))),
            optimize=max(0.0, min(5.0, float(settings.get("inkscapeOptimize", 0.2)))),
            maximum_paths=min(MAX_HATCH_PATHS, 50000),
            timeout_seconds=max(10, min(600, int(os.getenv("INKSCAPE_TRACE_TIMEOUT_SECONDS", "120")))),
        )
        try:
            result = convert_inkscape(source_image, filename, common, inkscape_settings)
        except ValueError as exc:
            raise GenerationError(str(exc)) from exc
        engine_suffix = "inkscape-trace"
        stats = {
            "mode": "color-multiscan",
            "source_width": source_width,
            "source_height": source_height,
            "trace_width": result.width,
            "trace_height": result.height,
            "output_width_mm": round(result.output_width_mm, 4),
            "output_height_mm": round(result.output_height_mm, 4),
            "path_count": result.path_count,
            "contour_paths": result.path_count,
            "hatch_paths": 0,
            "point_count": result.point_count,
            "pen_up_travel_px": 0.0,
            "engine": result.engine,
            "engine_detail": result.engine_detail,
        }
        svg = result.svg
    elif engine == "potrace":
        potrace_settings = PotraceSettings(
            threshold=max(0, min(255, int(settings.get("potraceThreshold", 128)))),
            turd_size=max(0, min(100000, int(settings.get("potraceTurdSize", 2)))),
            turn_policy=str(settings.get("potraceTurnPolicy", "minority")),
            alpha_max=max(0.0, min(1.334, float(settings.get("potraceAlphaMax", 1.0)))),
            optimize_curves=bool(settings.get("potraceOptimizeCurves", True)),
            optimize_tolerance=max(0.0, min(5.0, float(settings.get("potraceOptimizeTolerance", 0.2)))),
        )
        try:
            result = convert_potrace(source_image, filename, common, potrace_settings)
        except ValueError as exc:
            raise GenerationError(str(exc)) from exc
        engine_suffix = "potrace"
        stats = {
            "mode": "regions",
            "source_width": source_width,
            "source_height": source_height,
            "trace_width": result.width,
            "trace_height": result.height,
            "output_width_mm": round(result.output_width_mm, 4),
            "output_height_mm": round(result.output_height_mm, 4),
            "path_count": result.path_count,
            "contour_paths": result.path_count,
            "hatch_paths": 0,
            "point_count": result.point_count,
            "pen_up_travel_px": 0.0,
            "engine": result.engine,
            "engine_detail": result.engine_detail,
        }
        svg = result.svg
    elif engine == "pixels2svg":
        pixel_settings = Pixels2SvgSettings(
            max_colors=max(2, min(256, int(settings.get("pixelsMaxColors", 16)))),
            color_tolerance=max(0, min(765, int(settings.get("pixelsColorTolerance", 64)))),
            remove_background=bool(settings.get("pixelsRemoveBackground", True)),
            background_tolerance=max(0.0, min(50.0, float(settings.get("pixelsBackgroundTolerance", 1.0)))),
            maximum_artifact_percent=max(0.0, min(100.0, float(settings.get("pixelsArtifactPercent", 0.1)))),
            group_by_color=bool(settings.get("pixelsGroupByColor", True)),
            maximum_paths=min(MAX_HATCH_PATHS, 50000),
        )
        try:
            result = convert_pixels2svg(source_image, filename, common, pixel_settings)
        except ValueError as exc:
            raise GenerationError(str(exc)) from exc
        engine_suffix = "pixels2svg"
        stats = {
            "mode": "color-regions",
            "source_width": source_width,
            "source_height": source_height,
            "trace_width": result.width,
            "trace_height": result.height,
            "output_width_mm": round(result.output_width_mm, 4),
            "output_height_mm": round(result.output_height_mm, 4),
            "path_count": result.path_count,
            "contour_paths": result.path_count,
            "hatch_paths": 0,
            "point_count": result.point_count,
            "pen_up_travel_px": 0.0,
            "engine": result.engine,
            "engine_detail": result.engine_detail,
        }
        svg = result.svg

    # Stamp the actual backend engine into the SVG itself. The frontend verifies
    # this marker before displaying or transferring a result, preventing stale
    # responses or mismatched frontend/backend builds from masquerading as the
    # selected engine.
    engine_marker = f'data-hatchplot-engine="{engine}"'
    if engine_marker not in svg:
        svg = svg.replace("<svg ", f"<svg {engine_marker} ", 1)
    svg_sha256 = hashlib.sha256(svg.encode("utf-8")).hexdigest()
    stats["requested_engine"] = engine
    stats["svg_sha256"] = svg_sha256

    safe_base = os.path.splitext(os.path.basename(filename or "converted-image"))[0]
    safe_base = "".join(character if character.isalnum() or character in "-_." else "-" for character in safe_base).strip("-.")
    output_filename = f"{safe_base or 'converted-image'}-{engine_suffix}.svg"
    transfer_id = _store_converter_transfer(svg, output_filename, stats)
    logger.info(
        "Vectorized %s with engine=%s paths=%s digest=%s",
        filename,
        engine,
        stats.get("path_count", 0),
        svg_sha256[:12],
    )
    return {
        "svg": svg,
        "filename": output_filename,
        "transfer_id": transfer_id,
        "engine": engine,
        "svg_sha256": svg_sha256,
        "stats": stats,
    }


@app.get("/vectorize/engines")
def vectorize_engines() -> JSONResponse:
    return JSONResponse(
        content={"engines": converter_engine_status()},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.post("/vectorize")
async def vectorize_image(
    file: UploadFile = File(...),
    engine: str = Form("linedraw"),
    mode: str = Form("contour-hatch"),
    outputWidth: float = Form(150.0),
    maxDimension: int = Form(1024),
    autoContrastCutoff: float = Form(2.0),
    blurRadius: int = Form(1),
    alphaThreshold: int = Form(16),
    contourLowThreshold: int = Form(70),
    contourHighThreshold: int = Form(180),
    contourSimplify: float = Form(1.5),
    minimumContourLength: float = Form(10.0),
    hatchSize: int = Form(16),
    hatchLightThreshold: int = Form(160),
    hatchMidThreshold: int = Form(96),
    hatchDarkThreshold: int = Form(40),
    strokeWidthMm: float = Form(0.35),
    sortStrokes: bool = Form(True),
    invert: bool = Form(False),
    whiteBackground: bool = Form(True),
    inkscapeScans: int = Form(8),
    inkscapeSmooth: bool = Form(True),
    inkscapeStack: bool = Form(True),
    inkscapeRemoveBackground: bool = Form(True),
    inkscapeSpeckles: int = Form(2),
    inkscapeSmoothCorners: float = Form(1.0),
    inkscapeOptimize: float = Form(0.2),
    potraceThreshold: int = Form(128),
    potraceTurdSize: int = Form(2),
    potraceTurnPolicy: str = Form("minority"),
    potraceAlphaMax: float = Form(1.0),
    potraceOptimizeCurves: bool = Form(True),
    potraceOptimizeTolerance: float = Form(0.2),
    pixelsMaxColors: int = Form(16),
    pixelsColorTolerance: int = Form(64),
    pixelsRemoveBackground: bool = Form(True),
    pixelsBackgroundTolerance: float = Form(1.0),
    pixelsArtifactPercent: float = Form(0.1),
    pixelsGroupByColor: bool = Form(True),
):
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    filename = file.filename or "converted-image"
    await file.close()
    if not content:
        raise HTTPException(status_code=422, detail="The uploaded image is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The image exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )
    settings = {
        "engine": engine,
        "mode": mode,
        "outputWidth": outputWidth,
        "maxDimension": maxDimension,
        "autoContrastCutoff": autoContrastCutoff,
        "blurRadius": blurRadius,
        "alphaThreshold": alphaThreshold,
        "contourLowThreshold": contourLowThreshold,
        "contourHighThreshold": contourHighThreshold,
        "contourSimplify": contourSimplify,
        "minimumContourLength": minimumContourLength,
        "hatchSize": hatchSize,
        "hatchLightThreshold": hatchLightThreshold,
        "hatchMidThreshold": hatchMidThreshold,
        "hatchDarkThreshold": hatchDarkThreshold,
        "strokeWidthMm": strokeWidthMm,
        "sortStrokes": sortStrokes,
        "invert": invert,
        "whiteBackground": whiteBackground,
        "inkscapeScans": inkscapeScans,
        "inkscapeSmooth": inkscapeSmooth,
        "inkscapeStack": inkscapeStack,
        "inkscapeRemoveBackground": inkscapeRemoveBackground,
        "inkscapeSpeckles": inkscapeSpeckles,
        "inkscapeSmoothCorners": inkscapeSmoothCorners,
        "inkscapeOptimize": inkscapeOptimize,
        "potraceThreshold": potraceThreshold,
        "potraceTurdSize": potraceTurdSize,
        "potraceTurnPolicy": potraceTurnPolicy,
        "potraceAlphaMax": potraceAlphaMax,
        "potraceOptimizeCurves": potraceOptimizeCurves,
        "potraceOptimizeTolerance": potraceOptimizeTolerance,
        "pixelsMaxColors": pixelsMaxColors,
        "pixelsColorTolerance": pixelsColorTolerance,
        "pixelsRemoveBackground": pixelsRemoveBackground,
        "pixelsBackgroundTolerance": pixelsBackgroundTolerance,
        "pixelsArtifactPercent": pixelsArtifactPercent,
        "pixelsGroupByColor": pixelsGroupByColor,
    }
    try:
        return vectorize_raster_image(content, filename, settings)
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/transfers/{transfer_id}")
def consume_converter_transfer(transfer_id: str) -> dict[str, Any]:
    _cleanup_converter_transfers()
    with converter_transfers_lock:
        record = converter_transfers.pop(transfer_id, None)
    if not record:
        raise HTTPException(status_code=404, detail="The converted SVG transfer expired or was already consumed.")
    return {
        "svg": record["svg"],
        "filename": record["filename"],
        "stats": record.get("stats", {}),
    }

@app.get("/health")
def health() -> dict[str, Any]:
    _cleanup_jobs()
    _cleanup_converter_transfers()
    return {
        "status": "ok",
        "active_jobs": _active_job_count(),
        "job_workers": JOB_WORKERS,
        "acceleration_backend": ACCELERATION_BACKEND,
        "converter_engines": converter_engine_status(),
        "network_delivery_enabled": NETWORK_DELIVERY_ENABLED,
        "pending_converter_transfers": len(converter_transfers),
    }


@app.post("/gcode/deliver")
def deliver_gcode(payload: GcodeDeliveryRequest) -> dict[str, Any]:
    try:
        result = deliver_gcode_file(payload)
    except NetworkDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info(
        "Delivered G-code filename=%s protocol=%s bytes=%d destination=%s",
        result["filename"],
        result["protocol"],
        result["bytes"],
        result["destination"],
    )
    return result


@app.post("/gcode/deliver-stream")
def deliver_gcode_stream(payload: GcodeDeliveryRequest) -> StreamingResponse:
    events: queue.Queue[dict[str, Any]] = queue.Queue()

    def report_progress(sent: int, total: int) -> None:
        events.put({"status": "uploading", "sent": sent, "total": total})

    def run_delivery() -> None:
        try:
            events.put({"status": "connecting"})
            events.put(deliver_gcode_file(payload, report_progress))
        except NetworkDeliveryError as exc:
            events.put({"status": "error", "detail": str(exc)})
        except Exception:
            logger.exception("Unexpected streaming network delivery failure")
            events.put({"status": "error", "detail": "The network upload failed unexpectedly."})

    def stream_events() -> Iterable[bytes]:
        threading.Thread(target=run_delivery, daemon=True).start()
        while True:
            event = events.get()
            yield (json.dumps(event) + "\n").encode("utf-8")
            if event.get("status") in {"sent", "error"}:
                break

    return StreamingResponse(stream_events(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


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
    sourceWidthMm: float = Form(0.0),
    sourceHeightMm: float = Form(0.0),
    zMode: str = Form("stepper"),
    zUp: str = Form("5.0"),
    zDown: str = Form("0.0"),
    xyFeedRate: int = Form(2000),
    zPlungeRate: int = Form(300),
    penThickness: float = Form(0.5),
    densityFudge: float = Form(0.0),
    brightnessCutoff: float = Form(DEFAULT_BRIGHTNESS_CUTOFF),
    generationMode: str = Form("hatch"),
    outlineTraceMethod: str = Form("boundary"),
    workspaceOrigin: str = Form("top-left"),
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
    startGcode: str = Form(""),
    endGcode: str = Form(""),
    enabledLayers: str = Form("[]"),
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
        sourceWidthMm,
        sourceHeightMm,
        zMode,
        zUp,
        zDown,
        xyFeedRate,
        zPlungeRate,
        penThickness,
        densityFudge,
        brightnessCutoff,
        generationMode,
        outlineTraceMethod,
        workspaceOrigin,
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
        startGcode,
        endGcode,
    )
    params["sourceFilename"] = file.filename or "uploaded.svg"
    try:
        parsed_layers = json.loads(enabledLayers)
        params["enabledLayers"] = [str(layer) for layer in parsed_layers] if isinstance(parsed_layers, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        params["enabledLayers"] = []
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
    sourceWidthMm: float = Form(0.0),
    sourceHeightMm: float = Form(0.0),
    zMode: str = Form("stepper"),
    zUp: str = Form("5.0"),
    zDown: str = Form("0.0"),
    xyFeedRate: int = Form(2000),
    zPlungeRate: int = Form(300),
    penThickness: float = Form(0.5),
    densityFudge: float = Form(0.0),
    brightnessCutoff: float = Form(DEFAULT_BRIGHTNESS_CUTOFF),
    generationMode: str = Form("hatch"),
    outlineTraceMethod: str = Form("boundary"),
    workspaceOrigin: str = Form("top-left"),
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
    startGcode: str = Form(""),
    endGcode: str = Form(""),
    enabledLayers: str = Form("[]"),
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
        sourceWidthMm,
        sourceHeightMm,
        zMode,
        zUp,
        zDown,
        xyFeedRate,
        zPlungeRate,
        penThickness,
        densityFudge,
        brightnessCutoff,
        generationMode,
        outlineTraceMethod,
        workspaceOrigin,
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
        startGcode,
        endGcode,
    )
    params["sourceFilename"] = file.filename or "uploaded.svg"
    try:
        parsed_layers = json.loads(enabledLayers)
        params["enabledLayers"] = [str(layer) for layer in parsed_layers] if isinstance(parsed_layers, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        params["enabledLayers"] = []
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
