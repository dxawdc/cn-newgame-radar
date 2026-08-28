"""对外部媒体下载实施统一的 SSRF、体积和图片像素边界。"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image


DEFAULT_TRUSTED_MEDIA_HOSTS = {
    "taptap.cn", "tapimg.com", "qcloudimg.com", "gtimg.com",
    "mi.com", "xiaomi.com", "mi-img.com", "vivo.com.cn", "vivoglobal.com",
    "oppo.com", "oppomobile.com", "heytapimg.com", "heytap.com",
    "heytapimage.com",
    "huawei.com", "dbankcdn.com", "hicloud.com", "hihonor.com",
    "honor.com", "hihonorcdn.com", "9game.cn", "9game.com",
    "4399.com", "4399.cn", "img4399.com", "71acg.net",
    "233leyuan.com", "haoyou.com", "apple.com", "mzstatic.com",
}


class UnsafeDownloadError(ValueError):
    """URL、响应或图片违反安全下载策略。"""


@dataclass(frozen=True)
class SafeDownload:
    content: bytes
    status_code: int
    content_type: str
    final_url: str


def trusted_media_hosts() -> set[str]:
    configured = {
        item.strip().casefold().rstrip(".")
        for item in os.environ.get("NEWGAME_TRUSTED_MEDIA_HOSTS", "").split(",")
        if item.strip()
    }
    return configured or set(DEFAULT_TRUSTED_MEDIA_HOSTS)


def _trusted_host(hostname: str, allowed_hosts: set[str]) -> bool:
    hostname = hostname.casefold().rstrip(".")
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in allowed_hosts)


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return not any((
        address.is_private, address.is_loopback, address.is_link_local,
        address.is_multicast, address.is_reserved, address.is_unspecified,
    ))


def validate_download_url(
    url: str, *, allowed_hosts: set[str] | None = None,
    resolver=socket.getaddrinfo,
) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"}:
        raise UnsafeDownloadError("仅允许 http/https 下载")
    if parsed.username or parsed.password:
        raise UnsafeDownloadError("下载 URL 不允许包含认证信息")
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname:
        raise UnsafeDownloadError("下载 URL 缺少主机名")
    if parsed.port not in {None, 80, 443}:
        raise UnsafeDownloadError("下载 URL 端口不在允许范围")
    if not _trusted_host(hostname, allowed_hosts or trusted_media_hosts()):
        raise UnsafeDownloadError(f"媒体主机不在可信清单：{hostname}")
    try:
        addresses = {
            item[4][0] for item in resolver(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise UnsafeDownloadError(f"媒体主机 DNS 解析失败：{hostname}") from exc
    if not addresses or not all(_public_address(address) for address in addresses):
        raise UnsafeDownloadError(f"媒体主机解析到非公网地址：{hostname}")
    return url


def download_bytes(
    url: str, *, headers: dict[str, str] | None = None,
    max_bytes: int = 15 * 1024 * 1024, max_redirects: int = 3,
    allowed_content_types: tuple[str, ...] = ("image/",),
    validate_image: bool = False, max_pixels: int = 40_000_000,
    max_dimension: int = 12_000, timeout: tuple[int, int] = (5, 25),
    allowed_hosts: set[str] | None = None, resolver=socket.getaddrinfo,
    session: requests.Session | None = None,
) -> SafeDownload:
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    client = session or requests.Session()
    owns_session = session is None
    current = url
    try:
        for redirect_count in range(max_redirects + 1):
            validate_download_url(current, allowed_hosts=allowed_hosts, resolver=resolver)
            response = client.get(
                current, headers=headers, timeout=timeout, stream=True, allow_redirects=False,
            )
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= max_redirects:
                        raise UnsafeDownloadError("媒体下载重定向次数超限")
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeDownloadError("媒体下载重定向缺少 Location")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if allowed_content_types and not any(
                    content_type.startswith(prefix) for prefix in allowed_content_types
                ):
                    raise UnsafeDownloadError(f"响应类型不允许：{content_type or '(empty)'}")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > max_bytes:
                            raise UnsafeDownloadError("响应 Content-Length 超过限制")
                    except ValueError as exc:
                        raise UnsafeDownloadError("响应 Content-Length 无效") from exc
                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafeDownloadError("响应内容超过体积限制")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if validate_image:
                    try:
                        image = Image.open(BytesIO(content))
                        width, height = image.size
                    except Exception as exc:
                        raise UnsafeDownloadError("响应不是可解析图片") from exc
                    if width <= 0 or height <= 0 or width > max_dimension or height > max_dimension:
                        raise UnsafeDownloadError("图片尺寸超过限制")
                    if width * height > max_pixels:
                        raise UnsafeDownloadError("图片像素总量超过限制")
                return SafeDownload(content, response.status_code, content_type, current)
            finally:
                response.close()
        raise UnsafeDownloadError("媒体下载重定向次数超限")
    finally:
        if owns_session:
            client.close()
