from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from main import (  # noqa: E402
    GcodeDeliveryRequest,
    NetworkDeliveryError,
    _deliver_ftp,
    _deliver_webdav,
    _network_gcode_bytes,
    _safe_delivery_filename,
    _webdav_target_url,
)


class NetworkDeliveryValidationTests(unittest.TestCase):
    def test_gcode_is_utf8_and_ends_with_one_newline(self) -> None:
        self.assertEqual(_network_gcode_bytes("G0 X0 Y0\n\n"), b"G0 X0 Y0\n")

    def test_remote_filename_rejects_directory_components(self) -> None:
        self.assertEqual(_safe_delivery_filename("drawing.nc"), "drawing.nc")
        for filename in ("../drawing.nc", "folder/drawing.nc", "folder\\drawing.nc", ""):
            with self.subTest(filename=filename), self.assertRaises(NetworkDeliveryError):
                _safe_delivery_filename(filename)

    def test_webdav_target_appends_encoded_filename(self) -> None:
        target = _webdav_target_url("https://plotter.local/dav/jobs", "my drawing.nc")
        self.assertEqual(target, "https://plotter.local/dav/jobs/my%20drawing.nc")

    def test_webdav_url_rejects_embedded_credentials(self) -> None:
        with self.assertRaises(NetworkDeliveryError):
            _webdav_target_url("https://user:secret@plotter.local/jobs", "drawing.nc")


class NetworkDeliveryClientTests(unittest.TestCase):
    @patch("main.http.client.HTTPSConnection")
    def test_webdav_uses_put_with_exact_content_length_header(self, connection_factory: MagicMock) -> None:
        connection = connection_factory.return_value
        response = connection.getresponse.return_value
        response.status = 201
        response.read.return_value = b""
        payload = GcodeDeliveryRequest(
            protocol="webdav",
            filename="drawing.nc",
            gcode="G0 X0 Y0",
            url="https://plotter.local/dav",
            username="plotter",
            password="secret",
        )

        destination = _deliver_webdav(payload, b"G0 X0 Y0\n", "drawing.nc")

        self.assertEqual(destination, "https://plotter.local/dav/drawing.nc")
        connection.putrequest.assert_called_once_with("PUT", "/dav/drawing.nc", skip_accept_encoding=True)
        self.assertIn(
            (("Content-Length", str(len(b"G0 X0 Y0\n"))), {}),
            [(call.args, call.kwargs) for call in connection.putheader.call_args_list],
        )
        authorization_calls = [
            call.args[1]
            for call in connection.putheader.call_args_list
            if call.args[0] == "Authorization"
        ]
        self.assertEqual(len(authorization_calls), 1)
        self.assertTrue(authorization_calls[0].startswith("Basic "))
        connection.endheaders.assert_called_once_with(b"G0 X0 Y0\n")
        connection.close.assert_called_once_with()

    @patch("main.ftplib.FTP")
    def test_ftp_uploads_to_selected_directory(self, ftp_factory: MagicMock) -> None:
        client = ftp_factory.return_value
        payload = GcodeDeliveryRequest(
            protocol="ftp",
            filename="drawing.nc",
            gcode="G0 X0 Y0",
            host="plotter.local",
            port=2121,
            directory="jobs",
            username="plotter",
            password="secret",
            passive=True,
        )

        destination = _deliver_ftp(payload, b"G0 X0 Y0\n", "drawing.nc")

        client.connect.assert_called_once_with("plotter.local", 2121, timeout=30)
        client.login.assert_called_once_with("plotter", "secret")
        client.cwd.assert_called_once_with("jobs")
        command, stream = client.storbinary.call_args.args
        self.assertEqual(command, "STOR drawing.nc")
        self.assertEqual(stream.read(), b"G0 X0 Y0\n")
        self.assertEqual(destination, "ftp://plotter.local:2121/jobs/drawing.nc")

    @patch("main.ftplib.FTP_TLS")
    def test_ftps_protects_the_data_connection(self, ftp_tls_factory: MagicMock) -> None:
        client = ftp_tls_factory.return_value
        payload = GcodeDeliveryRequest(
            protocol="ftp",
            filename="drawing.nc",
            gcode="G0 X0 Y0",
            host="plotter.local",
            ftp_tls=True,
        )

        destination = _deliver_ftp(payload, b"G0 X0 Y0\n", "drawing.nc")

        self.assertIsNotNone(ftp_tls_factory.call_args.kwargs.get("context"))
        client.prot_p.assert_called_once_with()
        self.assertEqual(destination, "ftps://plotter.local:21/drawing.nc")


if __name__ == "__main__":
    unittest.main()
