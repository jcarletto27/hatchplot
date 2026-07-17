from __future__ import annotations

import io
import math
import pathlib
import sys
import unittest

import numpy as np
from PIL import Image

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from linedraw_engine import LineDrawSettings, convert_image, trace_centerlines  # noqa: E402
from main import (  # noqa: E402
    _compile_gcode,
    _estimate_machining_time,
    _gcode_preamble,
    generate_toolpath,
    validate_generation_params,
)


class CenterlineTraceTests(unittest.TestCase):
    def test_thick_horizontal_mark_becomes_one_centerline(self) -> None:
        gray = np.full((31, 61), 255, dtype=np.uint8)
        gray[11:20, 5:56] = 0
        settings = LineDrawSettings(
            mode="centerline",
            contour_high_threshold=128,
            contour_simplify=0.0,
            minimum_contour_length=5.0,
        )

        lines = trace_centerlines(gray, settings)

        long_lines = [line for line in lines if len(line) >= 2 and abs(line[-1][0] - line[0][0]) > 35]
        self.assertEqual(len(long_lines), 1)
        self.assertLess(max(abs(point[1] - 15.0) for point in long_lines[0]), 2.0)

    def test_converter_accepts_centerline_mode(self) -> None:
        pixels = np.full((40, 80), 255, dtype=np.uint8)
        pixels[16:24, 8:72] = 0
        result = convert_image(
            Image.open(io.BytesIO(_png_bytes(pixels))),
            "mark.png",
            LineDrawSettings(
                mode="centerline",
                max_dimension=128,
                auto_contrast_cutoff=0,
                blur_radius=0,
                contour_high_threshold=128,
                contour_simplify=0,
                minimum_contour_length=4,
            ),
        )
        self.assertGreater(result.stroke_count, 0)
        self.assertEqual(len(result.hatches), 0)
        self.assertIn("mode=centerline", result.svg)


class CustomGcodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = {
            "bedX": 100.0,
            "bedY": 100.0,
            "svgScale": 100.0,
            "svgScaleMode": "absolute",
            "svgRotate": 0.0,
            "svgPosX": 50.0,
            "svgPosY": 50.0,
            "sourceWidthMm": 10.0,
            "sourceHeightMm": 10.0,
            "zMode": "stepper",
            "zUp": "5",
            "zDown": "0",
            "xyFeedRate": 1000,
            "zPlungeRate": 300,
            "penThickness": 0.5,
            "densityFudge": 0.0,
            "brightnessCutoff": 0.025,
            "generationMode": "outline",
            "outlineTraceMethod": "boundary",
            "workspaceOrigin": "top-left",
            "startGcode": "M17\n; fixture ready",
            "endGcode": "M5\nM18",
        }

    def test_custom_commands_wrap_generated_program(self) -> None:
        validate_generation_params(self.params)
        paths = [[[10.0, 20.0], [30.0, 40.0]]]
        estimate = _estimate_machining_time(paths, self.params)
        lines = _compile_gcode(paths, self.params, estimate)
        self.assertEqual(lines[0], "; HatchPlot generated G-code")
        header_end = lines.index("; End HatchPlot header")
        self.assertEqual(lines[header_end + 1:header_end + 3], ["M17", "; fixture ready"])
        self.assertEqual(lines[header_end + 3:header_end + 6], ["G21", "G90", "G0 Z5"])
        self.assertEqual(lines[-3:], ["G0 X0 Y0", "M5", "M18"])
        self.assertEqual(len(_gcode_preamble(self.params, 1, estimate)), lines.index("G0 X10.00 Y20.00"))

    def test_toolpath_centerline_mode_uses_rendered_scan(self) -> None:
        pixels = np.full((100, 100), 255, dtype=np.uint8)
        pixels[44:56, 10:90] = 0
        self.params["outlineTraceMethod"] = "centerline"

        result = generate_toolpath(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
            self.params,
            _png_bytes(pixels),
        )

        self.assertEqual(result["stats"]["outline_trace_method"], "centerline")
        self.assertEqual(result["stats"]["outline_trace_source"], "browser-rendered-raster")
        self.assertGreater(len(result["paths"]), 0)
        gcode_lines = result["gcode"].splitlines()
        header_end = gcode_lines.index("; End HatchPlot header")
        self.assertEqual(gcode_lines[header_end + 1:header_end + 3], ["M17", "; fixture ready"])
        self.assertEqual(gcode_lines[-2:], ["M5", "M18"])

    def test_single_line_raster_has_one_pen_down_path(self) -> None:
        pixels = np.full((32, 32), 255, dtype=np.uint8)
        pixels[8:24, 8:24] = 0
        self.params.update({
            "bedX": 20.0,
            "bedY": 20.0,
            "svgPosX": 10.0,
            "svgPosY": 10.0,
            "generationMode": "single-line",
            "patternLayout": "linear",
            "waveform": "ekg",
            "patternCenterX": 10.0,
            "patternCenterY": 10.0,
            "patternAngle": 0.0,
            "patternSpacing": 1.0,
            "patternClockwise": True,
            "waveAmplitude": 0.4,
            "waveLength": 2.0,
            "brightnessModulation": "both",
        })

        result = generate_toolpath(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>',
            self.params,
            _png_bytes(pixels),
        )

        self.assertEqual(len(result["paths"]), 1)
        self.assertGreater(len(result["paths"][0]), 100)
        self.assertEqual(result["stats"]["continuous_paths"], 1)
        self.assertEqual(result["stats"]["pen_lifts_during_image"], 0)
        left, top, right, bottom = result["stats"]["scan_bounds_mm"]
        self.assertGreater(left, 4.0)
        self.assertGreater(top, 4.0)
        self.assertLess(right, 16.0)
        self.assertLess(bottom, 16.0)
        self.assertTrue(all(
            left <= point[0] <= right and top <= point[1] <= bottom
            for point in result["paths"][0]
        ))
        lines = result["gcode"].splitlines()
        self.assertEqual(lines.count("G1 Z0 F300"), 1)
        self.assertEqual(lines.count("G0 Z5"), 2)

    def test_concentric_single_line_has_no_boundary_chords(self) -> None:
        pixels = np.full((40, 60), 255, dtype=np.uint8)
        pixels[5:35, 8:52] = 0
        self.params.update({
            "bedX": 30.0,
            "bedY": 20.0,
            "svgPosX": 15.0,
            "svgPosY": 10.0,
            "generationMode": "single-line",
            "patternLayout": "concentric",
            "waveform": "straight",
            "patternCenterX": 15.0,
            "patternCenterY": 10.0,
            "patternAngle": 0.0,
            "patternSpacing": 1.0,
            "patternClockwise": True,
            "waveAmplitude": 0.0,
            "waveLength": 2.0,
            "brightnessModulation": "both",
        })

        result = generate_toolpath(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="30" height="20"/>',
            self.params,
            _png_bytes(pixels),
        )

        points = result["paths"][0]
        longest_segment = max(math.dist(first, second) for first, second in zip(points, points[1:]))
        self.assertLess(longest_segment, 2.0)


def _png_bytes(pixels: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buffer, format="PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
