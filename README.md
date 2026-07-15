# HatchPlot

HatchPlot converts SVG artwork into plotter-ready G-code. It combines a browser workspace built with Paper.js, a queued FastAPI/Shapely generation backend, and an Nginx frontend. The application runs with Docker Compose and supports CPU or optional NVIDIA CUDA brightness sampling.

## Current features

- SVG upload with layer enable/disable controls.
- Physical SVG scale, rotation, placement, automatic centering, and fit-to-workspace handling.
- Top-left, top-right, bottom-left, or bottom-right machine-coordinate origins.
- Persistent machine, generation, simulation, and workspace settings.
- Canvas zoom, source-SVG visibility, pattern-center pin placement, and brightness-cutoff exclusion preview.
- Background generation jobs with live progress, cancellation, estimated remaining time, and streamed toolpath preview.
- G-code simulation up to 300× speed with synchronized line highlighting.
- G-code export with a detailed header describing the machine, SVG, transform, layers, generation mode, pattern, waveform, brightness controls, and path count.
- A one-click **Best Guess** analyzer that selects conservative detail-first hatch settings from the transformed artwork.
- Tooltips for generation values, layouts, waveforms, and coordinate options.

### Generation modes

- **Brightness Hatch** maps grayscale to ordered local carrier density. Darker regions retain more carriers, lighter regions retain fewer, and pixels below the brightness cutoff are excluded.
- **Outline Trace** follows native SVG vector geometry. Stroked paths use their centerlines and filled shapes use their vector boundaries.
- **Outline then Hatch** plots native SVG outlines first and then appends the selected brightness-driven hatch pattern.

Hatch layouts include linear, spiral, concentric, and radial carriers. Waveforms include zig-zag, sawtooth, sine, EKG, and straight lines, with amplitude, wavelength, angle, spacing, center, direction, and brightness modulation controls.

## Image to SVG page

Open **Image to SVG** from the HatchPlot header or visit `/converter.html`.

The converter accepts browser-readable raster formats including PNG, JPEG, WebP, BMP, GIF, and AVIF. It provides:

- posterized grayscale, monochrome silhouette, and edge-shape tracing;
- physical output width and trace-resolution controls;
- grayscale levels or threshold controls;
- blur, alpha threshold, despeckle, contour simplification, and smoothing;
- source and generated-SVG previews;
- SVG download; and
- **Send to Toolpath Workspace**, which transfers the generated SVG directly into the main HatchPlot workspace.

Conversion runs locally in the browser. The source raster is not uploaded to the backend.

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

Jobs and completed results are held in backend memory. Restarting the backend clears them. Browser settings and converter transfers are stored only in the current browser profile and site origin.

## Troubleshooting

```bash
docker compose ps
docker compose logs --tail=300 backend frontend
docker inspect --format='{{json .State.Health}}' hatch-plotter-api
```

Every API response includes an `X-Request-ID`, and the same value appears in backend logs.
