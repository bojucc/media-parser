import os
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

try:
    import fcntl  # noqa: F401
except ImportError:  # The app does not need file locks in tests on Windows.
    sys.modules["fcntl"] = types.SimpleNamespace(LOCK_EX=0, flock=lambda *_args: None)

with patch.dict(os.environ, {
    "SECRET_KEY": "test-only-media-download-key",
    "DATABASE_PATH": os.path.join(tempfile.gettempdir(), "media-parser-download-tests.db"),
}):
    from app import app


class FakeParser:
    def get_title_content(self):
        return "测试视频"

    def get_description(self):
        return None

    def get_real_video_url(self):
        return "https://cdn.example/video.mp4"

    def get_video_list(self):
        return []

    def get_cover_photo_url(self):
        return "https://cdn.example/cover.jpg"

    def get_author_info(self):
        return None

    def get_image_list(self):
        return []

    def get_audio_url(self):
        return None

    def get_subtitles(self):
        return None

    @property
    def is_preview(self):
        return False


class MediaDownloadTest(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.client = app.test_client()

    def parsed_data(self):
        with patch(
            "src.api.parse.WebFetcher.fetch_redirect_url",
            return_value="https://www.douyin.com/video/123",
        ), patch("src.api.parse.ParserFactory.create_parser", return_value=FakeParser()):
            response = self.client.post("/api/parse", json={"text": "https://v.douyin.com/abc"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["data"]

    def test_parse_returns_backend_download_urls_for_video_and_cover(self):
        data = self.parsed_data()

        self.assertTrue(data["video_download_token"])
        self.assertTrue(data["cover_download_token"])

    def test_upstream_session_is_reused_per_thread(self):
        from src.api.download import _get_http_session

        first = _get_http_session()
        second = _get_http_session()
        self.assertIs(first, second)
        self.assertIsNotNone(first.get_adapter("https://"))

    def test_download_logs_transfer_metrics_without_signed_url_or_token(self):
        from src.api.download import create_download_token

        signed_url = "https://cdn.example/video.mp4?signature=private-value"
        with app.app_context():
            token = create_download_token(signed_url, "video", "test")
        upstream = unittest.mock.Mock(
            status_code=200,
            headers={"Content-Type": "video/mp4", "Content-Length": "5"},
            url=signed_url,
        )
        upstream.iter_content.return_value = [b"video"]
        with patch("src.api.download._validate_public_target"), patch(
            "src.api.download._get_http_session"
        ) as get_session:
            get_session.return_value.get.return_value = upstream
            with self.assertLogs("app", level="INFO") as captured:
                response = self.client.get(
                    "/api/download", headers={"X-Media-Download-Token": token}
                )
                self.assertEqual(response.data, b"video")

        log_output = "\n".join(captured.output)
        self.assertIn("cdn.example", log_output)
        self.assertIn("bytes=5", log_output)
        self.assertNotIn("private-value", log_output)
        self.assertNotIn(token, log_output)

    def test_download_uses_parse_title_as_filename(self):
        data = self.parsed_data()
        upstream = unittest.mock.Mock(
            status_code=200,
            headers={"Content-Type": "video/mp4", "Content-Length": "11"},
        )
        upstream.iter_content.return_value = [b"video bytes"]
        session = unittest.mock.Mock()
        session.get.return_value = upstream
        with patch("src.api.download._validate_public_target"), patch(
            "src.api.download._get_http_session", return_value=session
        ):
            response = self.client.get(
                "/api/download",
                headers={"X-Media-Download-Token": data["video_download_token"]},
            )

        self.assertIn("filename*=UTF-8''%E6%B5%8B%E8%AF%95%E8%A7%86%E9%A2%91.mp4", response.headers["Content-Disposition"])

    def test_download_forwards_range_request_to_upstream(self):
        data = self.parsed_data()
        upstream = unittest.mock.Mock(
            status_code=206,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": "5",
                "Content-Range": "bytes 5-9/10",
            },
        )
        upstream.iter_content.return_value = [b"bytes"]
        session = unittest.mock.Mock()
        session.get.return_value = upstream
        with patch("src.api.download._validate_public_target"), patch(
            "src.api.download._get_http_session", return_value=session
        ) as get_session:
            response = self.client.get(
                "/api/download",
                headers={
                    "X-Media-Download-Token": data["video_download_token"],
                    "Range": "bytes=5-9",
                },
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(get_session.return_value.get.call_args.kwargs["headers"]["Range"], "bytes=5-9")

    def test_signed_download_streams_media_from_backend(self):
        data = self.parsed_data()
        upstream = unittest.mock.Mock(
            status_code=200,
            headers={"Content-Type": "video/mp4", "Content-Length": "11"},
        )
        upstream.iter_content.return_value = [b"video bytes"]
        session = unittest.mock.Mock()
        session.get.return_value = upstream
        with patch("src.api.download._validate_public_target"), patch(
            "src.api.download._get_http_session", return_value=session
        ):
            video_response = self.client.get(
                "/api/download",
                headers={"X-Media-Download-Token": data["video_download_token"]},
            )

        self.assertEqual(video_response.status_code, 200)
        self.assertEqual(video_response.data, b"video bytes")
        self.assertEqual(video_response.mimetype, "video/mp4")
        self.assertEqual(video_response.headers["X-Accel-Buffering"], "no")

    def test_missing_token_is_rejected(self):
        response = self.client.get("/api/download")

        self.assertEqual(response.status_code, 400)

    def test_tampered_token_is_rejected(self):
        token = self.parsed_data()["video_download_token"]
        tampered_token = token[:-1] + ("x" if token[-1] != "x" else "y")

        response = self.client.get(
            "/api/download", headers={"X-Media-Download-Token": tampered_token}
        )

        self.assertEqual(response.status_code, 400)

    def test_expired_token_is_rejected(self):
        with patch("src.api.download.time.time", return_value=1000):
            token = self.parsed_data()["video_download_token"]
        with patch("src.api.download.time.time", return_value=1000 + 24 * 60 * 60 + 1):
            response = self.client.get(
                "/api/download", headers={"X-Media-Download-Token": token}
            )

        self.assertEqual(response.status_code, 400)

    def test_private_ip_target_is_rejected_before_fetch(self):
        token = self.parsed_data()["video_download_token"]
        with patch(
            "src.api.download.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ), patch("src.api.download._get_http_session") as get_session:
            response = self.client.get(
                "/api/download", headers={"X-Media-Download-Token": token}
            )

        self.assertEqual(response.status_code, 403)
        get_session.assert_not_called()

    def test_redirect_to_private_ip_is_rejected(self):
        token = self.parsed_data()["video_download_token"]
        public_address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private_address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        redirect = unittest.mock.Mock(
            status_code=302,
            headers={"Location": "https://127.0.0.1/internal"},
        )
        with patch(
            "src.api.download.socket.getaddrinfo",
            side_effect=[[public_address], [private_address]],
        ), patch("src.api.download._get_http_session") as get_session:
            get_session.return_value.get.return_value = redirect
            response = self.client.get(
                "/api/download", headers={"X-Media-Download-Token": token}
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(get_session.return_value.get.call_count, 1)

    def test_upstream_forbidden_response_is_not_returned_as_a_file(self):
        token = self.parsed_data()["video_download_token"]
        upstream = unittest.mock.Mock(status_code=403, headers={})
        session = unittest.mock.Mock()
        session.get.return_value = upstream
        with patch("src.api.download._validate_public_target"), patch(
            "src.api.download._get_http_session", return_value=session
        ):
            response = self.client.get(
                "/api/download", headers={"X-Media-Download-Token": token}
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"error": "媒体源暂时无法访问"})

    def test_oversized_media_is_rejected_before_streaming(self):
        token = self.parsed_data()["video_download_token"]
        upstream = unittest.mock.Mock(
            status_code=200,
            headers={"Content-Type": "video/mp4", "Content-Length": str(512 * 1024 * 1024 + 1)},
        )
        session = unittest.mock.Mock()
        session.get.return_value = upstream
        with patch("src.api.download._validate_public_target"), patch(
            "src.api.download._get_http_session", return_value=session
        ):
            response = self.client.get(
                "/api/download", headers={"X-Media-Download-Token": token}
            )

        self.assertEqual(response.status_code, 413)
        upstream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
