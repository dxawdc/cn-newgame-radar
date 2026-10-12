import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from newgame_monitor.icon_cache import _download_icon
from newgame_monitor.safe_download import SafeDownload
from newgame_monitor.safe_download import UnsafeDownloadError


class IconCacheTest(unittest.TestCase):
    def test_oversized_taptap_original_uses_validated_thumbnail(self):
        buffer = BytesIO()
        Image.new("RGB", (16, 16), "red").save(buffer, "PNG")
        downloaded = SafeDownload(buffer.getvalue(), 200, "image/png", "https://img-tc.tapimg.com/thumb")
        original = "https://img-tc.tapimg.com/market/images/icon.png"
        with tempfile.TemporaryDirectory() as root, patch(
            "newgame_monitor.icon_cache.download_bytes",
            side_effect=[UnsafeDownloadError("图片尺寸超过限制"), downloaded],
        ) as downloader:
            result = _download_icon(original, Path(root))
            self.assertEqual(result["status"], "success")
            self.assertEqual(downloader.call_count, 2)
            self.assertEqual(downloader.call_args_list[1].args[0], original + "/_tap_appicon.jpg")

    def test_existing_icon_is_atomically_replaced(self):
        buffer = BytesIO()
        Image.new("RGB", (16, 16), "red").save(buffer, "PNG")
        downloaded = SafeDownload(buffer.getvalue(), 200, "image/png", "https://tapimg.com/icon.png")
        with tempfile.TemporaryDirectory() as root, patch(
            "newgame_monitor.icon_cache.download_bytes", return_value=downloaded,
        ):
            icon_dir = Path(root)
            first = _download_icon("https://tapimg.com/icon.png", icon_dir)
            self.assertEqual(first["status"], "success")
            target = icon_dir / first["relative_path"]
            target.write_bytes(b"old-content")
            second = _download_icon("https://tapimg.com/icon.png", icon_dir)
            self.assertEqual(second["status"], "success")
            with Image.open(target) as image:
                self.assertEqual(image.format, "WEBP")
            self.assertFalse(list(target.parent.glob(".icon-*.webp")))


if __name__ == "__main__":
    unittest.main()
