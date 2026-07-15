# HatchPlot

HatchPlot converts SVG artwork into plotter-ready G-code. It combines a browser workspace built with Paper.js, a queued FastAPI/Shapely generation backend, and an Nginx frontend. The application runs with Docker Compose and supports CPU or optional NVIDIA CUDA brightness sampling.

## Current features

- SVG upload with layer enable/disable controls.
- Physical SVG scale, rotation, placement, automatic centering, and fit-to-workspace handling.
- Top-left, top-right, bottom-left, or bottom-right machine-coordinate origins.
- Persistent machine, generation, simulation, and workspace settings, with a single-card guided workflow.
- Canvas zoom and panning, source-SVG visibility, pattern-center pin placement, and brightness-cutoff exclusion preview.
- Background generation jobs with live progress, cancellation, estimated remaining time, and streamed toolpath preview.
- G-code simulation up to 300× speed with synchronized line highlighting.
- `.nc` export using `{first-8-source-characters}-{origin}-{generation}-{layout}.nc`, with layout omitted when it does not apply.
- A 64-character-safe G-code header describing the output filename, machine, SVG, transform, layers, generation mode, pattern, waveform, brightness controls, path count, and estimated machining time.
- Machining-time estimation from drawing distance, pen-up travel, configured XY feed, and a 0.5-second delay for each pen-up and pen-down action.
- A one-click **Best Guess** analyzer that selects conservative detail-first hatch settings from the transformed artwork.
- Tooltips for generation values, layouts, waveforms, and coordinate options.

The Toolpath workspace opens one setup card at a time. New installations begin at **Machine Setup**; after machine settings have been saved once, later visits begin at **Artwork & Placement**. Use middle-mouse drag or Ctrl+left-mouse drag to pan the toolpath canvas.

Export names use two-letter origin codes (`TL`, `TR`, `BL`, or `BR`) and generation codes (`OUTLINE`, `HATCH`, or `OTH`). For example, an Outline then Hatch job using a spiral layout from `rapunzel.svg` with a bottom-left origin exports as `rapunzel-BL-OTH-Spiral.nc`.

### Generation modes

- **Brightness Hatch** maps grayscale to ordered local carrier density. Darker regions retain more carriers, lighter regions retain fewer, and pixels below the brightness cutoff are excluded.
- **Outline Trace** follows native SVG vector geometry. Stroked paths use their centerlines and filled shapes use their vector boundaries.
- **Outline then Hatch** plots native SVG outlines first and then appends the selected brightness-driven hatch pattern.

Hatch layouts include linear, spiral, concentric, and radial carriers. Waveforms include zig-zag, sawtooth, sine, EKG, and straight lines, with amplitude, wavelength, angle, spacing, center, direction, and brightness modulation controls.

## Image to SVG page

Use the workspace switch at the top of either page to move between **Toolpath** and **Image Conversion**, or visit `/converter.html` directly.

The converter accepts browser-readable raster formats including PNG, JPEG, WebP, BMP, GIF, and AVIF. It produces plotter-oriented, polyline-only SVG files with three modes:

- **Contours only** detects connected image edges and emits open or closed stroked polylines.
- **Hatches only** converts grayscale into three ordered hatch-density bands.
- **Contours + hatches** combines both passes, with contours listed before hatches.

Controls include physical output width, trace resolution, preview stroke width, automatic contrast, blur, alpha handling, Canny edge thresholds, contour simplification, minimum contour length, hatch cell size, three tonal thresholds, inversion, and stroke-order optimization. Previewed SVGs contain no embedded raster image and no filled pixel-cell regions.

**Send to Toolpath Workspace** uses a short-lived backend transfer token rather than browser storage. The workspace consumes the generated SVG once and explicitly selects the requested generation mode. **Outline Trace** is recommended because converter output already contains the final contour and hatch polylines.

The converter engine is a Python 3/OpenCV adaptation of the contour, hatch, and nearest-endpoint ordering concepts from Lingdong Huang's MIT-licensed `linedraw` project. Attribution and the license text are included in `THIRD_PARTY_NOTICES.md`.


## Start with CPU generation

```bash
docker compose down
docker compose up --build -d
docker compose ps
docker compose logs -f backend frontend
```

Default host ports are configured in `.env`:

- HatchPlot: `http://HOST:9090`
- Image converter: `http://HOST:9090/converter.html`
- API documentation: `http://HOST:9000/docs`
- Backend health: `http://HOST:9000/health`

## Optional NVIDIA CUDA sampling

CUDA acceleration applies to dense brightness-map sampling. SVG parsing, Shapely/GEOS clipping, path sequencing, and G-code assembly remain CPU operations.

Install a compatible NVIDIA driver and NVIDIA Container Toolkit, then configure Docker:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi
```

Start HatchPlot with the GPU override:

```bash
docker compose -f compose.yml -f compose.gpu.yml down
docker compose -f compose.yml -f compose.gpu.yml up --build -d
docker compose -f compose.yml -f compose.gpu.yml logs -f backend
```

`ACCELERATION_BACKEND` accepts:

- `auto`: use CUDA when CuPy and an NVIDIA device are available, otherwise use NumPy;
- `cpu`: always use NumPy; or
- `cuda`: require CUDA and fail clearly when it is unavailable.

## Configuration

The `.env` and Compose files expose the host ports, worker count, pending-job limit, upload and brightness-map limits, retained-result limit, job TTL, toolpath/path limits, logging level, acceleration backend, and default brightness cutoff.

Jobs, completed results, and pending converter-transfer tokens are held in backend memory. Restarting the backend clears them. Browser settings remain local to the current browser profile and site origin.

## Troubleshooting

```bash
docker compose ps
docker compose logs --tail=300 backend frontend
docker inspect --format='{{json .State.Health}}' hatch-plotter-api
```

Every API response includes an `X-Request-ID`, and the same value appears in backend logs.
