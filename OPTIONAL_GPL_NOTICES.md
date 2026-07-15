# Optional GPL Converter Notices

The components described below are opt-in and are not included in the standard image. The Python engines are installed by both GPL profiles; the exact Inkscape runtime is installed by the CPU GPL profile only.

## Inkscape

The optional CPU GPL converter image installs **Inkscape 1.4** from Debian Trixie and invokes its `object-trace` action for exact color multi-scan bitmap tracing. HatchPlot does not copy or reimplement Inkscape's tracing source in the standard image.

- Project: `https://inkscape.org/`
- Source: `https://gitlab.com/inkscape/inkscape`
- License: GPL-2.0-or-later
- License copy: `licenses/GPL-2.0.txt`

The Inkscape runtime is present only in `Dockerfile.gpl`. The GPU/GPL image reports this engine as unavailable unless a compatible Inkscape 1.4+ runtime is separately supplied.

## Potrace / potracer

The optional GPL converter image installs **potracer**, the pure-Python Potrace port maintained by Tatarize and based on Peter Selinger's Potrace 1.16 algorithm.

- Project: `https://github.com/tatarize/potrace`
- Package: `potracer==0.0.4`
- License: GNU General Public License version 2 or later
- License copy: `licenses/GPL-2.0.txt`

Potrace is a trademark of Peter Selinger.

## Pixels2SVG

The optional GPL converter image installs **pixels2svg** by Valentin François. It merges adjacent pixels of the same or similar color into SVG polygonal regions.

- Project: `https://github.com/ValentinFrancois/pixels2svg`
- Package: `pixels2svg==0.2.3`
- License: GNU General Public License version 3 or later
- License copy: `licenses/GPL-3.0.txt`
