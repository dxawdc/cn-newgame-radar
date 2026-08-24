"""只读扫描模拟器 App 缓存中的公开接口路径，并剥离查询参数。

不会保存缓存原文，也不会输出 Cookie、Token、账号或设备标识。
"""
import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PACKAGES = {
    "huawei": "com.huawei.gamebox",
    "honor": "com.hihonor.gamecenter",
    "xiaomi": "com.xiaomi.gamecenter",
    "oppo": "com.nearme.gamecenter",
}

URL_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{8,500}")
ROUTE_RE = re.compile(
    rb"/[A-Za-z0-9_.-]{0,60}(?:newgame|new_game|newGame|game|Game|beta|test|Test|appointment|reserve|calendar)"
    rb"[A-Za-z0-9_./-]{0,160}"
)
TEXT_EXTENSIONS = {".json", ".xml", ".txt", ".log", ".js", ".html", ".db", ".sqlite", ""}


def adb(adb_path: Path, serial: str, *args: str) -> bytes:
    return subprocess.check_output([str(adb_path), "-s", serial, *args], stderr=subprocess.DEVNULL)


def clean_url(raw: bytes) -> str | None:
    try:
        value = raw.decode("utf-8", errors="ignore").rstrip(".,;)'\"")
        parts = urlsplit(value)
        if not parts.hostname:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", choices=PACKAGES)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", default="127.0.0.1:16384")
    parser.add_argument("--max-files", type=int, default=1200)
    parser.add_argument("--roots", nargs="*", help="相对 App 数据目录的扫描子目录")
    parser.add_argument("--show-files", action="store_true")
    args = parser.parse_args()

    package = PACKAGES[args.platform]
    roots = args.roots or ["cache", "files", "databases"]
    root_paths = " ".join(f"/data/data/{package}/{item}" for item in roots)
    command = f"su -c 'find {root_paths} -type f -size -2097152c 2>/dev/null'"
    paths = adb(args.adb, args.serial, "shell", command).decode("utf-8", errors="ignore").splitlines()
    urls, routes, scanned = set(), set(), 0
    route_files = {}
    for path in paths:
        if scanned >= args.max_files:
            break
        suffix = Path(path).suffix.lower()
        if suffix not in TEXT_EXTENSIONS and not any(key in path.lower() for key in ("http", "cache", "config", "log")):
            continue
        try:
            content = adb(args.adb, args.serial, "exec-out", "su", "-c", f"cat '{path}'")
        except subprocess.CalledProcessError:
            continue
        scanned += 1
        if content.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")):
            continue
        for match in URL_RE.findall(content):
            value = clean_url(match)
            if value and any(key in value.lower() for key in ("game", "new", "test", "appoint", "reserve")):
                urls.add(value)
        for match in ROUTE_RE.findall(content):
            value = match.decode("utf-8", errors="ignore")
            if len(value) >= 6:
                routes.add(value)
                route_files.setdefault(value, path)

    print(f"平台={args.platform} 扫描文件={scanned}")
    print("[URL，无查询参数]")
    for value in sorted(urls):
        print(value)
    print("[候选路由]")
    for value in sorted(routes):
        suffix = f" <= {route_files[value]}" if args.show_files else ""
        print(value + suffix)


if __name__ == "__main__":
    main()
