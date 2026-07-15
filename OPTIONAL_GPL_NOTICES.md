# Optional GPL Converter Notices

The dependencies described below are installed only when HatchPlot is built with `compose.gpl.yml` or `compose.gpu-gpl.yml`. They are not included in the standard image.

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
