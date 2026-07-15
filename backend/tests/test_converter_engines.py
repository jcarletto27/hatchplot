from __future__ import annotations

import pathlib
import sys
import unittest
import xml.etree.ElementTree as ET

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from converter_engines import (  # noqa: E402
    InkscapeTraceSettings,
    _inkscape_trace_action,
    _make_inkscape_input_svg,
    _rewrite_inkscape_svg,
)


class InkscapeTraceAdapterTests(unittest.TestCase):
    def test_action_matches_inkscape_object_trace_parameter_order(self) -> None:
        action = _inkscape_trace_action(InkscapeTraceSettings(
            scans=12,
            smooth=False,
            stack=True,
            remove_background=False,
            speckles=7,
            smooth_corners=0.75,
            optimize=0.35,
        ))
        self.assertEqual(
            action,
            "select-by-id:source-image;object-trace:12,false,true,false,7,0.75,0.35;export-do",
        )

    def test_input_wrapper_references_only_the_private_temp_raster(self) -> None:
        wrapper = _make_inkscape_input_svg(320, 200)
        self.assertIn('id="source-image"', wrapper)
        self.assertIn('xlink:href="source.png"', wrapper)
        self.assertIn('viewBox="0 0 320 200"', wrapper)
        self.assertNotIn("data:image", wrapper)

    def test_rewrite_removes_source_bitmap_and_preserves_vector_layers(self) -> None:
        source = """<?xml version="1.0"?>
        <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
             width="320" height="200" viewBox="0 0 320 200">
          <image id="source-image" width="320" height="200" xlink:href="source.png"/>
          <g id="trace"><path style="fill:#123456" d="M0,0 L10,0 L10,10 Z"/></g>
        </svg>"""
        rewritten, path_count, point_count = _rewrite_inkscape_svg(
            source,
            "sample & source.png",
            320,
            200,
            160.0,
            100.0,
            "engine=inkscape",
        )
        document = ET.fromstring(rewritten)
        local_names = [element.tag.rsplit("}", 1)[-1] for element in document.iter()]
        self.assertNotIn("image", local_names)
        self.assertIn("path", local_names)
        self.assertEqual(document.attrib["width"], "160.0000mm")
        self.assertEqual(document.attrib["height"], "100.0000mm")
        self.assertEqual(document.attrib["viewBox"], "0 0 320 200")
        self.assertEqual(path_count, 1)
        self.assertGreaterEqual(point_count, 3)
        self.assertIn("sample &amp; source.png", rewritten)


if __name__ == "__main__":
    unittest.main()
