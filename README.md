# HatchPlot

Dockerized SVG-to-hatched-G-code generator with a Paper.js preview, FastAPI backend, and Nginx reverse proxy.

## What changed in this build

- Long-running generation uses a background process job instead of holding one HTTP request open.
- The frontend submits `/api/jobs`, polls job status, then downloads the result.
- Hatch grids are clipped in one Shapely operation per SVG shape instead of one operation per hatch line.
- Backend rotation, position, scale, and browser preview now use the same transform model.
- API failures return useful JSON messages and include request IDs in logs and responses.
- The backend has a health endpoint, Docker health checks, bounded job queues, upload limits, toolpath limits, and automatic container restart.
- The older synchronous `/generate` route remains available for compatibility.
- Machine limits and Z/feed settings are stored in browser `localStorage` and restored on the next visit.
- SVGs are centered automatically by default, with a manual **Center in Workspace** control.
- Imported SVG scale is now a true percentage of the original SVG size; 100% preserves the imported dimensions.
- Oversized SVGs prompt before being reduced to fit the active machine limits with a 5% margin on each side.
- Existing API clients retain the previous fit-relative scale behavior unless they send `svgScaleMode=absolute`.
- The browser now renders the transformed SVG into a machine-coordinate brightness map; preview and G-code therefore share one scale, rotation, and position source of truth.
- New browser jobs generate continuous boustrophedon zig-zag paths instead of independent hatch segments. Darker pixels produce taller, tighter zig-zags; white and transparent areas remain undrawn.
- Pen-tip thickness is requested on first use, saved with the machine settings, and controls path pitch plus preview stroke width.

## Start or rebuild

```bash
docker compose down
docker compose up --build -d
docker compose ps
docker compose logs -f backend frontend
```

Default host ports are read from `.env`:

- UI: `http://HOST:9090`
- Backend API/docs: `http://HOST:9000/docs`
- Backend health: `http://HOST:9000/health`

## Resource controls

The following values can be changed in `.env`:

- `JOB_WORKERS`: concurrent geometry worker processes. Start with `1`; increase only when the host has enough CPU and memory.
- `MAX_PENDING_JOBS`: maximum queued/running jobs.
- `MAX_UPLOAD_BYTES`: maximum SVG upload size in bytes.
- `MAX_BRIGHTNESS_MAP_BYTES`: maximum browser-generated PNG map size.
- `MAX_BRIGHTNESS_MAP_PIXELS`: maximum decoded brightness-map pixel count.
- `MIN_DRAW_DARKNESS`: minimum normalized darkness that receives ink; lower values retain lighter tones.
- `MAX_HATCH_PATHS`: maximum generated hatch segments.
- `MAX_TOOLPATH_POINTS`: maximum total coordinates retained for preview and G-code generation.
- `MAX_RETAINED_RESULTS`: maximum completed results kept in backend memory.
- `JOB_TTL_SECONDS`: how long completed results remain available in memory.
- `LOG_LEVEL`: backend log verbosity.

Jobs and results are held in memory. Restarting the backend removes current jobs, and the browser will report that the job expired.

## Troubleshooting

Check container state and health:

```bash
docker compose ps
docker inspect --format='{{json .State.Health}}' hatch-plotter-api
docker compose logs --tail=300 backend
```

Check whether the host killed the backend for excessive memory use:

```bash
docker inspect hatch-plotter-api --format='OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}'
dmesg -T | grep -i -E 'out of memory|oom|killed process'
```

Every API response includes an `X-Request-ID`; the same ID appears in backend logs.

## Browser settings and SVG placement

Machine settings are saved per browser and site origin. Changing the hostname, port, browser profile, or clearing site data creates a separate settings store.

The **Keep SVG centered in the workspace** option is enabled by default. Editing either Center X or Center Y switches to manual placement. Use **Center in Workspace** to re-enable automatic centering.

When an SVG is larger than the active machine width or height at 100% scale, the browser asks whether to scale it down. Accepting the prompt sets the largest uniform scale that fits inside 90% of the bed, leaving a 5% margin on each side. Declining leaves the selected scale unchanged; output outside the bed is clipped by the backend.

## Brightness-driven zig-zag generation

The browser rasterizes the visible SVG using the exact workspace transform before submitting a job. This supports vector fills, gradients, opacity, and embedded raster images without reinterpreting SVG units in the backend. SVGs that reference cross-origin images must embed those images as data URLs; otherwise the browser will prevent brightness-map export.

A solid connected region is emitted as one pen-down serpentine path whenever adjacent rows can be joined without crossing a white or transparent area. Disconnected artwork still requires separate pen lifts. Darker pixels shorten the local zig-zag wavelength and increase its amplitude, placing more ink in that portion of the image.

## Progress and time remaining

Queued jobs now publish phase-based progress through `GET /jobs/{job_id}`. The browser displays:

- current generation phase;
- completed and total scanlines or SVG elements;
- percentage complete;
- elapsed time;
- estimated time remaining; and
- active compute backend.

The estimate is continuously recalculated from completed work. Early estimates can move significantly while the first scanlines are processed, then stabilize as more work completes.

## Optional NVIDIA CUDA acceleration

GPU acceleration applies to dense brightness-map sampling and luminance calculations. Continuous path chaining, Shapely/GEOS clipping, and G-code assembly remain CPU-bound because those stages are branch-heavy and sequential. For small jobs, GPU transfer overhead may make CPU execution equally fast or faster.

The standard image uses NumPy on the CPU and automatically falls back to it when CUDA is unavailable. Set `ACCELERATION_BACKEND` to one of:

- `auto`: use CUDA when CuPy and an NVIDIA device are available, otherwise use NumPy;
- `cpu`: always use NumPy; or
- `cuda`: require CUDA and fail the job with a useful error if it is unavailable.

To run the included CUDA image, install the NVIDIA driver and NVIDIA Container Toolkit on the Docker host, then start the stack with both Compose files:

```bash
docker compose -f compose.yml -f compose.gpu.yml down
docker compose -f compose.yml -f compose.gpu.yml up --build -d
docker compose -f compose.yml -f compose.gpu.yml logs -f backend
```

Confirm the backend selected CUDA in the progress display or generated-job statistics. The backend health response also reports the requested acceleration mode.
