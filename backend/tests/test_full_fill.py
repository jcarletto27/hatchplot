from __future__ import annotations

import io
import pathlib
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from main import GenerationError, generate_toolpath, validate_generation_params


def _png_bytes(pixels: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode="L").save(buffer, format="PNG")
    return buffer.getvalue()


class FullFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.params = {
            "bedX": 10.0,
            "bedY": 10.0,
            "svgScale": 100.0,
            "svgScaleMode": "absolute",
            "svgRotate": 0.0,
            "svgPosX": 5.0,
            "svgPosY": 5.0,
            "zMode": "stepper",
            "zUp": "5",
            "zDown": "0",
            "xyFeedRate": 1000,
            "zPlungeRate": 300,
            "penThickness": 0.5,
            "densityFudge": 0.4,
            "brightnessCutoff": 0.025,
            "generationMode": "full-fill",
            "outlineTraceMethod": "boundary",
            "workspaceOrigin": "top-left",
            "patternLayout": "linear",
            "waveform": "zigzag",
            "patternCenterX": 5.0,
            "patternCenterY": 5.0,
            "patternAngle": 0.0,
            "patternSpacing": 5.0,
            "patternClockwise": True,
            "waveAmplitude": 2.0,
            "waveLength": 3.0,
            "brightnessModulation": "both",
            "startGcode": "",
            "endGcode": "",
        }

    def test_full_fill_uses_overlapping_straight_carriers(self) -> None:
        pixels = np.full((101, 101), 255, dtype=np.uint8)
        pixels[20:81, 20:81] = 128
        validate_generation_params(self.params)

        result = generate_toolpath(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" viewBox="0 0 10 10">'
            b'<rect x="2" y="2" width="6" height="6" fill="#808080"/></svg>',
            self.params,
            _png_bytes(pixels),
        )

        self.assertEqual(result["stats"]["generation_mode"], "full-fill")
        self.assertTrue(result["stats"]["full_fill"])
        self.assertEqual(result["stats"]["path_sequence"], "outline-then-full-fill")
        self.assertGreater(result["stats"]["outline_paths"], 0)
        self.assertGreater(result["stats"]["hatch_paths"], 0)
        self.assertEqual(result["stats"]["waveform"], "straight")
        self.assertAlmostEqual(result["stats"]["step_over_mm"], 0.4)
        self.assertLess(result["stats"]["step_over_mm"], self.params["penThickness"])
        self.assertGreater(len(result["paths"]), 10)
        self.assertIn("FILL-Linear.nc", result["stats"]["output_filename"])
        points = [point for path in result["paths"] for point in path]
        self.assertAlmostEqual(min(point[0] for point in points), 2.0, delta=0.1)
        self.assertAlmostEqual(max(point[0] for point in points), 8.0, delta=0.1)

    def test_zero_cutoff_does_not_fill_white_background(self) -> None:
        self.params["brightnessCutoff"] = 0.0
        with self.assertRaises(GenerationError):
            generate_toolpath(
                b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>',
                self.params,
                _png_bytes(np.full((21, 21), 255, dtype=np.uint8)),
            )


if __name__ == "__main__":
    unittest.main()
