import unittest
from io import BytesIO

from PIL import Image

from newgame_monitor.safe_download import (
    UnsafeDownloadError,
    download_bytes,
    validate_download_url,
)


def public_resolver(host, port, type=None):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeResponse:
    def __init__(self, content=b"", status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {"content-type": "image/png"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


class SafeDownloadTest(unittest.TestCase):
    def test_rejects_private_dns_and_untrusted_host(self):
        with self.assertRaisesRegex(UnsafeDownloadError, "可信清单"):
            validate_download_url(
                "https://evil.example/a.png", allowed_hosts={"cdn.example"},
                resolver=public_resolver,
            )
        with self.assertRaisesRegex(UnsafeDownloadError, "非公网地址"):
            validate_download_url(
                "https://cdn.example/a.png", allowed_hosts={"cdn.example"},
                resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
            )

    def test_revalidates_redirect_target(self):
        session = FakeSession([
            FakeResponse(status=302, headers={"location": "http://169.254.169.254/latest"}),
        ])
        with self.assertRaises(UnsafeDownloadError):
            download_bytes(
                "https://cdn.example/start", allowed_hosts={"cdn.example"},
                resolver=public_resolver, session=session,
            )

    def test_enforces_stream_size_and_content_type(self):
        oversized = FakeSession([FakeResponse(
            content=b"x" * 11,
            headers={"content-type": "image/png", "content-length": "11"},
        )])
        with self.assertRaisesRegex(UnsafeDownloadError, "Content-Length"):
            download_bytes(
                "https://cdn.example/a.png", max_bytes=10,
                allowed_hosts={"cdn.example"}, resolver=public_resolver, session=oversized,
            )
        wrong_type = FakeSession([FakeResponse(
            content=b"{}", headers={"content-type": "application/json"},
        )])
        with self.assertRaisesRegex(UnsafeDownloadError, "响应类型"):
            download_bytes(
                "https://cdn.example/a.png", allowed_hosts={"cdn.example"},
                resolver=public_resolver, session=wrong_type,
            )

    def test_enforces_image_pixel_limit(self):
        buffer = BytesIO()
        Image.new("RGB", (20, 20), "red").save(buffer, "PNG")
        session = FakeSession([FakeResponse(content=buffer.getvalue())])
        with self.assertRaisesRegex(UnsafeDownloadError, "像素总量"):
            download_bytes(
                "https://cdn.example/a.png", validate_image=True, max_pixels=100,
                allowed_hosts={"cdn.example"}, resolver=public_resolver, session=session,
            )


if __name__ == "__main__":
    unittest.main()
