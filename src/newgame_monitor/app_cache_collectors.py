"""从已登录模拟器的正常 App 公开页面缓存中读取数据。

仅读取新游/测试业务缓存，不读取账号、Cookie、Token 或设备标识。
"""
import json
import os
import shutil
import subprocess
import time
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageStat


SERIAL = os.environ.get("NEWGAME_ADB_SERIAL", "127.0.0.1:16384")

_OPPO_RESOURCE_MARKER = (
    b"\xfa\x07\x2ccom.heytap.cdo.common.domain.dto.ResourceDto"
)
_OPPO_DETAIL_LINK = re.compile(rb"oap://gc/dt\?id=(\d+)")
_OPPO_OFFLINE_BLOB_CACHE: dict[str, tuple[str, bytes | None]] = {}


def _adb_path() -> Path:
    configured = os.environ.get("NEWGAME_ADB")
    if configured:
        value = Path(configured)
        if value.is_file():
            return value
        raise FileNotFoundError("NEWGAME_ADB 指向的文件不存在")
    discovered = shutil.which("adb")
    if discovered:
        return Path(discovered)
    raise FileNotFoundError("未找到 ADB；请设置 NEWGAME_ADB 或将 adb 加入 PATH")


def _adb(*args: str) -> bytes:
    return subprocess.check_output([str(_adb_path()), "-s", SERIAL, *args], stderr=subprocess.DEVNULL)


def _read_root_file(path: str) -> bytes:
    return _adb("exec-out", "su", "-c", f"cat '{path}'")


def _find_game_dicts(value, found: dict, *, id_key: str, name_key: str) -> None:
    if isinstance(value, dict):
        if value.get(id_key) and value.get(name_key):
            found[str(value[id_key])] = value
        for child in value.values():
            _find_game_dicts(child, found, id_key=id_key, name_key=name_key)
    elif isinstance(value, list):
        for child in value:
            _find_game_dicts(child, found, id_key=id_key, name_key=name_key)


def _refresh_huawei_new_games() -> bytes | None:
    """按需打开华为游戏中心的新游页并下拉刷新缓存。"""
    if os.environ.get("NEWGAME_REFRESH_HUAWEI") != "1":
        return None
    package = "com.huawei.gamebox"
    try:
        _adb("shell", "am", "force-stop", package)
        _adb("shell", "monkey", "-p", package, "1")
        time.sleep(7)
        latest = b""
        for attempt in range(3):
            latest = _dump_ui(f"huawei-refresh-{attempt}")
            root = ET.fromstring(latest)
            continue_node = next((node for node in root.iter("node") if node.get("text") == "继续使用"), None)
            if continue_node is not None:
                _tap_node(continue_node)
                time.sleep(6)
                continue
            new_games = next((node for node in root.iter("node") if node.get("text") == "新游"), None)
            if new_games is not None:
                _tap_node(new_games)
                time.sleep(8)
                size_text = _adb("shell", "wm", "size").decode("utf-8", errors="ignore")
                size_match = re.search(r"(\d+)x(\d+)", size_text)
                width, height = (1080, 1920)
                if size_match:
                    width, height = int(size_match.group(1)), int(size_match.group(2))
                _adb(
                    "shell", "input", "swipe",
                    str(width // 2), str(int(height * 0.28)),
                    str(width // 2), str(int(height * 0.72)), "650",
                )
                time.sleep(8)
                latest = _dump_ui("huawei-refresh-complete")
                return latest
            time.sleep(3)
        return latest or None
    except Exception:
        # 刷新是缓存采集的增强步骤；已有缓存仍可作为降级来源。
        return None


def _huawei_event_fields(game: dict, now_ms: int) -> tuple[str, str, str | None]:
    """区分华为产品卡与 GameEvent 活动卡，避免把招募活动当作预约。"""
    test_info = game.get("testInfo") or {}
    event_card = game.get("gcode") == "GameEvent" or bool(game.get("startTime"))
    event_text = " ".join(
        str(value or "")
        for value in (game.get("typeName"), game.get("name"), test_info.get("typeText"))
    )
    if "招募" in event_text:
        event_type = "recruiting_beta"
    elif "测试" in event_text or test_info.get("type") not in (None, -1):
        event_type = "beta"
    else:
        release_ms = game.get("releaseDate")
        event_type = "reservation" if not release_ms or release_ms > now_ms else "launch"

    timestamp = game.get("startTime") if event_card else (test_info.get("startTime") or game.get("releaseDate"))
    try:
        event_time = datetime.fromtimestamp(int(timestamp) / 1000).astimezone().isoformat() if timestamp else ""
    except (TypeError, ValueError, OSError):
        event_time = ""
    status = game.get("name") if event_card else test_info.get("typeText")
    return event_type, event_time, status


def collect_huawei_cache() -> tuple[list[dict], list[tuple[str, bytes]]]:
    refresh_ui = _refresh_huawei_new_games()
    package = "com.huawei.gamebox"
    command = f"su -c 'find /data/data/{package}/cache/httpCache -type f -size -2097152c 2>/dev/null'"
    paths = _adb("shell", command).decode("utf-8", errors="ignore").splitlines()
    page = None
    for path in paths:
        content = _read_root_file(path)
        text = content.decode("utf-8", errors="ignore")
        start = text.find('{"rtnCode"')
        if start < 0:
            continue
        try:
            candidate = json.JSONDecoder().raw_decode(text[start:])[0]
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if candidate.get("rtnCode") == 0 and candidate.get("name") == "新游":
            page = candidate
            break
    if page is None:
        raise ValueError("华为缓存中未找到“新游”页面；请先在 App 中刷新该页")

    games = {}
    _find_game_dicts(page, games, id_key="appId", name_key="name")
    items = []
    now_ms = int(datetime.now().timestamp() * 1000)
    for game in games.values():
        # 新游页面也混有运营内容卡；它们有 appId/name，但不是游戏实体。
        if not game.get("pkgName") and game.get("landingPageType") is not None:
            continue
        event_type, event_time, status = _huawei_event_fields(game, now_ms)
        tags = [game.get("tagName")] if game.get("tagName") else []
        tags.extend(x.get("name") for x in game.get("impressTags") or [] if x.get("name"))
        score = (game.get("commentInfo") or {}).get("score")
        try:
            score = float(score) if score not in (None, "") else None
        except (TypeError, ValueError):
            score = None
        items.append({
            "source": "huawei_gamecenter",
            "source_item_id": str(game["appId"]),
            "name": game["name"],
            "package_name": game.get("pkgName"),
            "category": game.get("tagName") or game.get("kindName"),
            "tags": list(dict.fromkeys(tags)),
            "gameplay_intro": game.get("briefDes") or game.get("description"),
            "full_description": game.get("description"),
            "icon_url": game.get("icon"),
            "detail_url": game.get("openUrl") or game.get("deeplink"),
            "rating": score,
            "version_name": game.get("version"),
            "size_bytes": game.get("size"),
            "event_type": event_type,
            "event_time": event_time,
            "status": status,
            "raw": game,
        })
    public_raw = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw_responses = [("new-games-cache", public_raw)]
    if refresh_ui:
        raw_responses.append(("new-games-refresh-ui", refresh_ui))
    return items, raw_responses


def collect_xiaomi_cache() -> tuple[list[dict], list[tuple[str, bytes]]]:
    database = "/data/data/com.xiaomi.gamecenter/databases/gamecenter_v2"
    command = f"su -c 'sqlite3 {database} \"select DATA from DISCOVERY where ID=10003019;\"'"
    raw = _adb("shell", command).decode("utf-8", errors="strict").strip()
    if not raw:
        raise ValueError("小米缓存中未找到内测专区；请先在 App 中刷新该页")
    page = json.loads(raw)
    games = {}
    _find_game_dicts(page, games, id_key="id", name_key="title")
    items = []
    for game in games.values():
        detail = game.get("dInfo") or {}
        apk = detail.get("apk") or {}
        testing = detail.get("testing") or {}
        subscribe = detail.get("subscribe") or {}
        event_time = _xiaomi_event_time(testing, subscribe)
        tags = [x.get("name") for x in game.get("tag") or [] if x.get("name")]
        score = game.get("score") or game.get("scoreV2")
        try:
            score = float(score) if score not in (None, "") else None
        except (TypeError, ValueError):
            score = None
        items.append({
            "source": "xiaomi_gamecenter",
            "source_item_id": str(game["id"]),
            "name": game["title"],
            "package_name": apk.get("packageName"),
            "developer": detail.get("developer_name"),
            "category": tags[0] if tags else None,
            "tags": tags,
            "gameplay_intro": game.get("summary"),
            "icon_url": detail.get("icon"),
            "detail_url": game.get("actUrl"),
            "rating": score,
            "version_name": apk.get("versionName"),
            "size_bytes": apk.get("apkSize") or None,
            "event_type": "beta" if testing else "reservation",
            "event_time": event_time,
            "status": testing.get("name") or subscribe.get("text"),
            "raw": game,
        })
    return items, [("beta-zone-cache", raw.encode("utf-8"))]


def _xiaomi_event_time(testing: dict, subscribe: dict) -> str:
    """仅把小米对用户明确展示到“日”的时间作为预约档期。"""
    if testing:
        timestamp = testing.get("begin")
    else:
        visible_text = str(subscribe.get("text") or "").strip()
        has_exact_day = re.search(r"(?:20\d{2}年)?\d{1,2}月\d{1,2}日", visible_text)
        timestamp = subscribe.get("t") if has_exact_day else None
    try:
        return datetime.fromtimestamp(float(timestamp)).astimezone().isoformat() if timestamp else ""
    except (TypeError, ValueError, OSError):
        return ""


def _bounds(node) -> tuple[int, int, int, int]:
    values = [int(x) for x in re.findall(r"\d+", node.get("bounds", ""))]
    return tuple(values) if len(values) == 4 else (0, 0, 0, 0)


def _tap_node(node) -> None:
    left, top, right, bottom = _bounds(node)
    _adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))


def _dump_ui(name: str, attempts: int = 3) -> bytes:
    remote = f"/sdcard/{name}.xml"
    last_error = None
    for attempt in range(attempts):
        try:
            _adb("shell", "uiautomator", "dump", remote)
            return _adb("exec-out", "cat", remote)
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(1.5)
    raise last_error


def _capture_screen() -> bytes:
    return _adb("exec-out", "screencap", "-p")


def _ui_detail_enabled(source: str, name: str) -> bool:
    """按需打开游戏详情页。

    NEWGAME_UI_DETAILS=1 表示采集当前列表的全部详情；
    NEWGAME_UI_DETAIL_TARGETS 可传 {source: [name, ...]} 做定向补全。
    """
    if os.environ.get("NEWGAME_UI_DETAILS") == "1":
        return True
    raw = os.environ.get("NEWGAME_UI_DETAIL_TARGETS")
    if not raw:
        return False
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return name in targets.get(source, [])


def _text_by_suffix(root, suffix: str) -> str | None:
    node = next(
        (
            item for item in root.iter("node")
            if item.get("resource-id", "").endswith(f"/{suffix}") and item.get("text")
        ),
        None,
    )
    return node.get("text").strip() if node is not None else None


def _texts_by_suffix(root, suffix: str) -> list[str]:
    return list(dict.fromkeys(
        item.get("text").strip() for item in root.iter("node")
        if item.get("resource-id", "").endswith(f"/{suffix}") and item.get("text", "").strip()
    ))


def _clean_ui_text(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace("\r", "")
    text = re.sub(r"[ \t]*\n[ \t]*\n[ \t]*", "\n\n", text)
    text = re.sub(r"(?<!\n)[ \t]*\n[ \t]*(?!\n)", "", text).strip()
    return text or None


def _capture_oppo_gallery(expected_name: str, root, *, max_images: int = 5) -> list[str]:
    """从 OPPO 详情页的横向图集控件截取可见媒体，并保存为可同步的本地资源。"""
    icon_root = Path(os.environ.get("NEWGAME_ICON_DIR", "data/icons"))
    target_root = icon_root / "gallery" / "oppo_gamecenter"
    target_root.mkdir(parents=True, exist_ok=True)
    name_hash = hashlib.sha1(expected_name.encode("utf-8")).hexdigest()[:12]
    urls: list[str] = []
    seen: set[str] = set()
    stable = 0
    current_root = root
    for index in range(max_images):
        gallery = next(
            (
                item for item in current_root.iter("node")
                if item.get("resource-id", "").endswith("/screenshots_view")
            ),
            None,
        )
        if gallery is None:
            break
        candidates = [
            item for item in gallery.iter("node")
            if item.get("class") == "android.widget.ImageView"
            and item.get("content-desc") in {"图片", "视频"}
            and (_bounds(item)[2] - _bounds(item)[0]) >= 240
            and (_bounds(item)[3] - _bounds(item)[1]) >= 160
        ]
        if not candidates:
            break
        image_node = max(
            candidates,
            key=lambda item: (
                (_bounds(item)[2] - _bounds(item)[0])
                * (_bounds(item)[3] - _bounds(item)[1])
            ),
        )
        left, top, right, bottom = _bounds(image_node)
        try:
            screen = Image.open(BytesIO(_capture_screen())).convert("RGB")
            crop = screen.crop((
                max(0, left), max(0, top), min(screen.width, right), min(screen.height, bottom),
            ))
        except Exception:
            break
        if crop.width < 240 or crop.height < 160:
            break
        digest = hashlib.sha256(crop.resize((32, 32)).tobytes()).hexdigest()
        if digest in seen:
            stable += 1
        else:
            stable = 0
            seen.add(digest)
            filename = f"{name_hash}-{digest[:16]}.webp"
            target = target_root / filename
            if not target.exists():
                crop.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                crop.save(target, "WEBP", quality=84, method=4)
            urls.append(f"local-screenshot://gallery/oppo_gamecenter/{filename}")
        if stable >= 2 or index >= max_images - 1:
            break
        gallery_left, gallery_top, gallery_right, gallery_bottom = _bounds(gallery)
        _adb(
            "shell", "input", "swipe",
            str(max(gallery_left + 120, gallery_right - 160)),
            str((gallery_top + gallery_bottom) // 2),
            str(min(gallery_right - 120, gallery_left + 160)),
            str((gallery_top + gallery_bottom) // 2), "450",
        )
        time.sleep(0.7)
        current_root = ET.fromstring(_dump_ui(f"oppo-detail-{name_hash}-gallery-{index + 1}"))
    return urls


def _capture_honor_detail(expected_name: str) -> dict:
    """从当前荣耀详情页读取公开展示字段。"""
    result: dict = {"name": expected_name, "tags": []}
    for index in range(5):
        xml = _dump_ui(f"honor-detail-{hashlib.sha1(expected_name.encode()).hexdigest()[:10]}-{index}")
        root = ET.fromstring(xml)
        actual_name = _text_by_suffix(root, "zy_app_detail_name") or _text_by_suffix(root, "tvTitle")
        if index == 0 and actual_name and actual_name != expected_name:
            raise ValueError(f"荣耀详情名称不匹配：{expected_name} != {actual_name}")
        result["developer"] = result.get("developer") or _text_by_suffix(root, "zy_app_detail_payment")
        result["brief"] = result.get("brief") or _text_by_suffix(root, "zy_app_detail_desc")
        result["version_name"] = result.get("version_name") or _text_by_suffix(root, "tv_version_name")
        description = _text_by_suffix(root, "app_desc_detail_content")
        if description and len(description) > len(result.get("description") or ""):
            result["description"] = description
        result["tags"] = list(dict.fromkeys([*result["tags"], *_texts_by_suffix(root, "tv_label")]))
        if result.get("description"):
            break
        _adb("shell", "input", "swipe", "720", "2200", "720", "650", "500")
        time.sleep(1.0)
    if result.get("version_name"):
        result["version_name"] = re.sub(r"^版本\s*", "", result["version_name"]).strip()
    result["brief"] = _clean_ui_text(result.get("brief"))
    result["description"] = _clean_ui_text(result.get("description"))
    return result


def _capture_oppo_detail(expected_name: str) -> dict:
    """展开 OPPO「游戏介绍」后读取全文与详情标签。"""
    result: dict = {"name": expected_name, "tags": []}
    for index in range(4):
        xml = _dump_ui(f"oppo-detail-{hashlib.sha1(expected_name.encode()).hexdigest()[:10]}-{index}")
        root = ET.fromstring(xml)
        actual_name = _text_by_suffix(root, "appName")
        if index == 0 and actual_name and actual_name != expected_name:
            raise ValueError(f"OPPO 详情名称不匹配：{expected_name} != {actual_name}")
        result["tags"] = list(dict.fromkeys([*result["tags"], *_texts_by_suffix(root, "tv_tag_name")]))
        if not result.get("screenshot_urls"):
            result["screenshot_urls"] = _capture_oppo_gallery(expected_name, root)
        intro_node = next(
            (item for item in root.iter("node") if item.get("resource-id", "").endswith("/introduction_tv")),
            None,
        )
        if intro_node is not None:
            # 即使控件标记为不可点，轻触文本仍会展开完整介绍。
            _tap_node(intro_node)
            time.sleep(0.8)
            expanded = ET.fromstring(_dump_ui(
                f"oppo-detail-{hashlib.sha1(expected_name.encode()).hexdigest()[:10]}-expanded"
            ))
            description = _text_by_suffix(expanded, "introduction_tv")
            result["description"] = _clean_ui_text(description)
            break
        _adb("shell", "input", "swipe", "720", "2200", "720", "650", "500")
        time.sleep(1.0)
    return result


def _merge_ui_detail(item: dict, detail: dict, *, source: str) -> None:
    if detail.get("developer"):
        item["developer"] = item.get("developer") or detail["developer"]
    tags = list(dict.fromkeys([*item.get("tags", []), *detail.get("tags", [])]))
    item["tags"] = tags
    if not item.get("category") and tags:
        excluded = {"官服", "热门新游", "2D", "3D", "横屏", "竖屏"}
        item["category"] = next((tag for tag in tags if tag not in excluded), None)
    if detail.get("brief"):
        item["gameplay_intro"] = item.get("gameplay_intro") or detail["brief"]
    if detail.get("description"):
        item["full_description"] = detail["description"]
        item["gameplay_intro"] = item.get("gameplay_intro") or detail["description"][:180]
    if detail.get("version_name"):
        item["version_name"] = item.get("version_name") or detail["version_name"]
    item.setdefault("raw", {})["ui_detail"] = {
        **{key: value for key, value in detail.items() if value not in (None, "", [])},
        "source": source,
        "captured_at": datetime.now().astimezone().isoformat(),
    }


def _oppo_network_response_data(blob: bytes) -> bytes | None:
    """从 Java 序列化的 NetworkResponse 中取出业务响应 byte[]。"""
    if not blob.startswith(b"\xac\xed\x00\x05"):
        return None
    if b"com.nearme.network.internal.NetworkResponse" not in blob:
        return None
    marker = b"\x75\x72\x00\x02[B"
    candidates: list[bytes] = []
    offset = 0
    while True:
        start = blob.find(marker, offset)
        if start < 0:
            break
        class_end = blob.find(b"\x78\x70", start + len(marker), start + 96)
        if class_end >= 0 and class_end + 6 <= len(blob):
            size = int.from_bytes(blob[class_end + 2:class_end + 6], "big")
            data_start = class_end + 6
            if 0 < size <= len(blob) - data_start:
                candidates.append(blob[data_start:data_start + size])
        offset = start + len(marker)
    if len(candidates) != 1 or _OPPO_RESOURCE_MARKER not in candidates[0]:
        return None
    return candidates[0]


def _oppo_offline_blobs() -> list[bytes]:
    try:
        base = "/data/data/com.nearme.gamecenter/files/cache/offline/*.0"
        paths = _adb(
            "shell", f"su -c \"ls -1t {base} 2>/dev/null\""
        ).decode("utf-8", errors="ignore").splitlines()
        manifest = _adb(
            "shell", f"su -c \"sha256sum {base} 2>/dev/null\""
        ).decode("utf-8", errors="ignore").splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    hashes = {}
    for line in manifest:
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+(.+\.0)", line.strip())
        if match:
            hashes[match.group(2)] = match.group(1).lower()
    entries = [(path, hashes[path]) for path in paths if path in hashes]
    active_paths = set(paths)
    for stale_path in set(_OPPO_OFFLINE_BLOB_CACHE) - active_paths:
        _OPPO_OFFLINE_BLOB_CACHE.pop(stale_path, None)
    blobs: list[bytes] = []
    for path, digest in entries:
        try:
            cached = _OPPO_OFFLINE_BLOB_CACHE.get(path)
            if cached is None or cached[0] != digest:
                raw = _read_root_file(path)
                payload = _oppo_network_response_data(raw)
                _OPPO_OFFLINE_BLOB_CACHE[path] = (digest, payload)
            payload = _OPPO_OFFLINE_BLOB_CACHE[path][1]
            if payload is not None:
                blobs.append(payload)
        except (OSError, subprocess.SubprocessError):
            continue
    return blobs


def _read_protobuf_varint(blob: bytes, offset: int, end: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    while offset < end and shift <= 63:
        byte = blob[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    return None


def _parse_protobuf_message(
    blob: bytes, start: int = 0, end: int | None = None,
) -> tuple[dict[int, list[int | bytes]], list[tuple[int, int, int]]] | None:
    """严格解析一个 protobuf 消息，同时保留 length-delimited 子区间。"""
    fields: dict[int, list[int | bytes]] = {}
    children: list[tuple[int, int, int]] = []
    cursor = start
    end = len(blob) if end is None else end
    while cursor < end:
        parsed_tag = _read_protobuf_varint(blob, cursor, end)
        if parsed_tag is None:
            return None
        tag, cursor = parsed_tag
        field_number, wire_type = tag >> 3, tag & 7
        if field_number == 0 or field_number >= 1 << 29:
            return None
        if wire_type == 0:
            parsed_value = _read_protobuf_varint(blob, cursor, end)
            if parsed_value is None:
                return None
            value, cursor = parsed_value
        elif wire_type == 1:
            if cursor + 8 > end:
                return None
            value, cursor = blob[cursor:cursor + 8], cursor + 8
        elif wire_type == 2:
            parsed_size = _read_protobuf_varint(blob, cursor, end)
            if parsed_size is None:
                return None
            size, data_start = parsed_size
            data_end = data_start + size
            if data_end > end:
                return None
            value, cursor = blob[data_start:data_end], data_end
            children.append((field_number, data_start, data_end))
        elif wire_type == 5:
            if cursor + 4 > end:
                return None
            value, cursor = blob[cursor:cursor + 4], cursor + 4
        else:
            return None
        fields.setdefault(field_number, []).append(value)
    if cursor != end or not fields:
        return None
    return fields, children


def _oppo_resource_fields(segment: bytes) -> dict[int, list[int | bytes]]:
    """解析一张已精确切边的 ResourceDto 顶层字段。"""
    parsed = _parse_protobuf_message(segment)
    if parsed is None:
        return {}
    fields, _ = parsed
    marker_values = fields.get(127, [])
    if not marker_values or marker_values[0] != b"com.heytap.cdo.common.domain.dto.ResourceDto":
        return {}
    return fields


def _oppo_resource_records(blob: bytes) -> list[dict[int, list[int | bytes]]]:
    """递归找到最小 ResourceDto 子消息，不读取相邻 wrapper 字段。"""
    found: list[dict[int, list[int | bytes]]] = []

    def visit(start: int, end: int, depth: int) -> None:
        parsed = _parse_protobuf_message(blob, start, end)
        if parsed is None:
            return
        fields, children = parsed
        marker_values = fields.get(127, [])
        if marker_values and marker_values[0] == b"com.heytap.cdo.common.domain.dto.ResourceDto":
            found.append(fields)
            return
        if depth >= 16:
            return
        for _, child_start, child_end in children:
            if _OPPO_RESOURCE_MARKER not in blob[child_start:child_end]:
                continue
            visit(child_start, child_end, depth + 1)

    visit(0, len(blob), 0)
    return found


def _oppo_field_texts(fields: dict[int, list[int | bytes]], field_number: int) -> list[str]:
    values = []
    for raw in fields.get(field_number, []):
        if not isinstance(raw, bytes):
            continue
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if value:
            values.append(value)
    return values


def _oppo_screenshot_urls(blob: bytes, name_end: int) -> list[str]:
    """读取当前游戏名之后、下一张游戏卡之前的连续商店截图 URL。"""
    tail = blob[name_end:name_end + 12000]
    next_card = _OPPO_DETAIL_LINK.search(tail)
    if next_card:
        tail = tail[:next_card.start()]
    matches = list(re.finditer(
        rb"https://gc-image\.heytapimage\.com/[^\x00-\x20\"<>]{5,300}"
        rb"\.(?:png|jpg|jpeg|webp)(?:\?[^\x00-\x20\"<>]{0,160})?",
        tail,
        re.IGNORECASE,
    ))
    groups: list[list[str]] = []
    for index, match in enumerate(matches):
        url = match.group().decode("utf-8", errors="ignore").rstrip("\\,]}")
        if not groups or match.start() - matches[index - 1].start() > 320:
            groups.append([])
        if url not in groups[-1]:
            groups[-1].append(url)
    for group in groups:
        if len(group) < 3:
            continue
        suffixes = [re.search(r"\.(png|jpe?g|webp)(?:\?|$)", url, re.I).group(1).lower() for url in group]
        # OPPO 序列化卡片通常先放 1 张 PNG 方形 Icon，随后紧跟 5 张同格式商店截图。
        # 当后续至少 3 张扩展名一致且与首张不同，剔除首张，避免把 Icon 混入五图。
        if len(group) >= 4 and suffixes[0] != suffixes[1] and suffixes[1:].count(suffixes[1]) >= 3:
            return group[1:11]
        return group[:10]
    return []


def _attach_oppo_offline_metadata(items: list[dict], blobs: list[bytes] | None = None) -> None:
    """从 OPPO App 的 ResourceDto 响应补包名、简介、Icon、图集和详情 ID。"""
    if not items:
        return
    blobs = _oppo_offline_blobs() if blobs is None else blobs
    cards_by_name: dict[str, dict] = {}
    ambiguous_names: set[str] = set()
    for blob in blobs:  # blobs 已按缓存时间倒序，优先使用最新卡片。
        for fields in _oppo_resource_records(blob):
            app_ids = [value for value in fields.get(1, []) if isinstance(value, int) and value > 0]
            names = list(dict.fromkeys([
                *_oppo_field_texts(fields, 3), *_oppo_field_texts(fields, 52),
            ]))
            if len(app_ids) != 1 or not names:
                continue
            app_id = app_ids[0]
            detail_ids = []
            for values in fields.values():
                for value in values:
                    if not isinstance(value, bytes):
                        continue
                    detail_ids.extend(int(match.group(1)) for match in _OPPO_DETAIL_LINK.finditer(value))
            detail_ids = list(dict.fromkeys(detail_ids))
            if len(detail_ids) > 1:
                continue
            if detail_ids and detail_ids[0] != app_id:
                continue
            screenshots = list(dict.fromkeys(
                value for value in _oppo_field_texts(fields, 32)
                if value.startswith("https://")
                if re.search(r"\.(?:png|jpe?g|webp)(?:\?|$)", value, re.IGNORECASE)
            ))[:10]
            package_names = _oppo_field_texts(fields, 7)
            icon_urls = [
                value for value in _oppo_field_texts(fields, 14)
                if value.startswith("https://")
            ]
            briefs = _oppo_field_texts(fields, 26)
            categories = _oppo_field_texts(fields, 30)
            card = {
                "package_name": package_names[0] if package_names else None,
                "app_id": str(app_id),
                "detail_url": f"oaps://gc/dt?id={app_id}",
                "deep_link": f"oap://gc/dt?id={app_id}" if detail_ids else None,
                "icon_url": icon_urls[0] if icon_urls else None,
                "screenshot_urls": screenshots,
                "brief": briefs[0] if briefs else None,
                "category": categories[0] if categories else None,
                "parser": "resource-dto-fields-v1",
                "confidence": "strict",
            }
            for name in names:
                if name in ambiguous_names:
                    continue
                existing = cards_by_name.get(name)
                if existing is None:
                    cards_by_name[name] = card
                elif existing["app_id"] == card["app_id"]:
                    for key, value in card.items():
                        if not existing.get(key) and value:
                            existing[key] = value
                else:
                    cards_by_name.pop(name, None)
                    ambiguous_names.add(name)
    for item in items:
        matched = cards_by_name.get(item["name"])
        if not matched:
            continue
        item["package_name"] = item.get("package_name") or matched["package_name"]
        item["icon_url"] = item.get("icon_url") or matched["icon_url"]
        item["detail_url"] = item.get("detail_url") or matched["detail_url"]
        item["gameplay_intro"] = item.get("gameplay_intro") or matched["brief"]
        item["category"] = item.get("category") or matched["category"]
        if matched["category"]:
            item["tags"] = list(dict.fromkeys([*item.get("tags", []), matched["category"]]))
        item.setdefault("raw", {})["oppo_offline_detail"] = {
            key: value for key, value in matched.items() if value
        }


def _attach_ui_icons(
    xml: bytes, screenshot: bytes, items: list[dict], *, source: str,
    name_suffix: str, icon_suffixes: tuple[str, ...],
) -> None:
    """按 UI 控件纵向位置将屏幕上的 Icon 裁切并关联到游戏。"""
    try:
        root = ET.fromstring(xml)
        screen = Image.open(BytesIO(screenshot)).convert("RGB")
    except Exception:
        return
    name_nodes = [
        node for node in root.iter("node")
        if node.get("resource-id", "").endswith(f"/{name_suffix}") and node.get("text")
    ]
    icon_nodes = [
        node for node in root.iter("node")
        if any(node.get("resource-id", "").endswith(f"/{suffix}") for suffix in icon_suffixes)
    ]
    icon_root = Path(os.environ.get("NEWGAME_ICON_DIR", "data/icons")) / "ui" / source
    icon_root.mkdir(parents=True, exist_ok=True)
    for item in items:
        names = [node for node in name_nodes if node.get("text", "").strip() == item["name"]]
        if not names or not icon_nodes:
            continue
        name_node = min(names, key=lambda node: abs((_bounds(node)[1] + _bounds(node)[3]) // 2 - 1280))
        name_y = (_bounds(name_node)[1] + _bounds(name_node)[3]) // 2
        icon_node = min(
            icon_nodes,
            key=lambda node: (
                abs(((_bounds(node)[1] + _bounds(node)[3]) // 2) - name_y),
                0 if node.get("resource-id", "").endswith("/appIcon") else 1,
            ),
        )
        left, top, right, bottom = _bounds(icon_node)
        if right <= left or bottom <= top:
            continue
        suffix = icon_node.get("resource-id", "").rsplit("/", 1)[-1]
        if suffix == "iconLayout":
            size = min(bottom - top, 180)
            center_x, center_y = (left + right) // 2, (top + bottom) // 2
            left, top, right, bottom = center_x - size // 2, center_y - size // 2, center_x + size // 2, center_y + size // 2
        left, top = max(0, left), max(0, top)
        right, bottom = min(screen.width, right), min(screen.height, bottom)
        if right - left < 40 or bottom - top < 40:
            continue
        crop = screen.crop((left, top, right, bottom))
        variance = sum(ImageStat.Stat(crop.resize((24, 24))).var) / 3
        if variance < 8:
            continue
        filename = f"{hashlib.sha256(item['name'].encode('utf-8')).hexdigest()[:24]}.webp"
        target = icon_root / filename
        if not target.exists():
            crop.thumbnail((384, 384), Image.Resampling.LANCZOS)
            crop.save(target, "WEBP", quality=88, method=4)
        item["icon_url"] = f"local-icon://ui/{source}/{filename}"


def _node_by_text(root, text: str, *, max_top: int | None = None, min_top: int | None = None):
    matches = []
    for node in root.iter("node"):
        value = (node.get("text") or "").replace("\n", "").strip()
        top = _bounds(node)[1]
        if value != text.replace("\n", "").strip():
            continue
        if max_top is not None and top > max_top:
            continue
        if min_top is not None and top < min_top:
            continue
        matches.append(node)
    return matches[0] if matches else None


def _wait_for_text_node(
    xml: bytes, text: str, *, dump_prefix: str, attempts: int = 6,
    delay_seconds: float = 3, max_top: int | None = None, min_top: int | None = None,
):
    """等待异步加载的页面入口出现在 UI 树中，并返回节点及最新 UI。"""
    latest = xml
    for attempt in range(attempts):
        root = ET.fromstring(latest)
        node = _node_by_text(root, text, max_top=max_top, min_top=min_top)
        if node is not None:
            return node, latest
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
            latest = _dump_ui(f"{dump_prefix}-{attempt + 1}")
    return None, latest


def _start_app(package: str, component: str, wait_seconds: float = 6) -> bytes:
    _adb("shell", "am", "force-stop", package)
    _adb("shell", "am", "start", "-n", component)
    time.sleep(wait_seconds)
    xml = b""
    root = None
    for attempt in range(3):
        xml = _dump_ui(f"{package}-start-{attempt}")
        root = ET.fromstring(xml)
        foreground_package = root.get("package") or next(
            (node.get("package") for node in root.iter("node") if node.get("package")), ""
        )
        if foreground_package == package:
            break
        _adb("shell", "monkey", "-p", package, "1")
        time.sleep(3)
    if root is None or foreground_package != package:
        raise ValueError(f"未能将 {package} 切换到前台")
    # 用户已明确允许荣耀移动服务风险提示的“继续使用”。
    continue_node = _node_by_text(root, "继续使用")
    if continue_node is not None:
        _tap_node(continue_node)
        time.sleep(3)
        xml = _dump_ui(f"{package}-continued")
    return xml


def _child_text(container, suffix: str) -> str | None:
    node = next(
        (x for x in container.iter("node") if x.get("resource-id", "").endswith(f"/{suffix}") and x.get("text")),
        None,
    )
    return node.get("text").strip() if node is not None else None


def _parse_honor_list(
    xml: bytes, event_type: str, reference: datetime | None = None,
) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for container in root.iter("node"):
        if not container.get("resource-id", "").endswith("/layout_provider_content"):
            continue
        name = _child_text(container, "tv_app_name")
        if not name:
            continue
        info = _child_text(container, "tv_download_info")
        intro = _child_text(container, "tv_desc")
        score = _child_text(container, "game_detail_score_num")
        category = info.split("·", 1)[0].strip() if info and "删档" not in info else None
        end_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日结束", intro or "")
        event_end = "-".join([
            end_match.group(1), end_match.group(2).zfill(2), end_match.group(3).zfill(2)
        ]) if end_match else ""
        event_date = _oppo_explicit_date(intro, reference)
        parsed_type = "launch" if event_date and re.search(r"(?:首发|上线)", intro or "") else event_type
        key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        items.append({
            "source": "honor_gamecenter",
            "source_item_id": key,
            "name": name,
            "category": category,
            "tags": [x.strip() for x in (info or "").split("·") if x.strip()],
            "gameplay_intro": None if end_match else intro,
            "rating": float(score) if score and re.fullmatch(r"\d+(?:\.\d+)?", score) else None,
            "event_type": parsed_type,
            "event_time": event_date,
            "event_end_time": event_end,
            "status": intro if event_date else (info if event_type == "beta" else "精品首发"),
            "raw": {
                "name": name, "info": info, "intro": intro,
                "event_date": event_date, "event_end_time": event_end,
            },
        })
    return items


def _collect_vertical_ui(
    prefix: str, parser, *, max_swipes: int = 8, detail_seen: set[str] | None = None,
) -> tuple[list[dict], list[tuple[str, bytes]]]:
    found, raws, stable = {}, [], 0
    detail_seen = detail_seen if detail_seen is not None else set()
    for index in range(max_swipes):
        xml = _dump_ui(f"{prefix}-{index}")
        raws.append((f"{prefix}-{index}", xml))
        before = len(found)
        parsed = parser(xml)
        if prefix.startswith("honor-"):
            _attach_ui_icons(
                xml, _capture_screen(), parsed, source="honor_gamecenter",
                name_suffix="tv_app_name", icon_suffixes=("iv_app_icon",),
            )
            root = ET.fromstring(xml)
            by_name = {item["name"]: item for item in parsed}
            for container in root.iter("node"):
                if not container.get("resource-id", "").endswith("/layout_provider_content"):
                    continue
                name = _child_text(container, "tv_app_name")
                if (
                    not name or name in detail_seen or name not in by_name
                    or not _ui_detail_enabled("honor_gamecenter", name)
                ):
                    continue
                detail_seen.add(name)
                try:
                    _tap_node(container)
                    time.sleep(2.5)
                    detail = _capture_honor_detail(name)
                    _merge_ui_detail(by_name[name], detail, source="honor_gamecenter_app")
                except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as exc:
                    by_name[name].setdefault("raw", {})["ui_detail_error"] = str(exc)[:300]
                finally:
                    _adb("shell", "input", "keyevent", "4")
                    time.sleep(1.2)
        for item in parsed:
            found[(item["source_item_id"], item["event_type"], item.get("event_time", ""))] = item
        stable = stable + 1 if len(found) == before else 0
        if stable >= 2:
            break
        _adb("shell", "input", "swipe", "720", "2200", "720", "700", "450")
        time.sleep(1)
    return list(found.values()), raws


def collect_honor_ui() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """通过荣耀游戏中心普通用户可见的列表 UI 采集精品首发和内测专区。"""
    _adb("shell", "am", "force-stop", "com.nearme.gamecenter")
    start_xml = _start_app(
        "com.hihonor.gamecenter",
        "com.hihonor.gamecenter/.bu_games_display.splash.SplashActivity",
    )
    new_tab, start_xml = _wait_for_text_node(
        start_xml, "新游", dump_prefix="honor-home-ready", max_top=500,
    )
    if new_tab is None:
        raise ValueError("荣耀游戏中心首页未找到“新游”页签")
    _tap_node(new_tab)
    time.sleep(4)
    new_xml = _dump_ui("honor-new-home")
    root = ET.fromstring(new_xml)
    title = _node_by_text(root, "精品首发")
    if title is None:
        raise ValueError("荣耀新游页未找到“精品首发”")
    title_top = _bounds(title)[1]
    more = next(
        (n for n in root.iter("node") if n.get("text") == "更多" and abs(_bounds(n)[1] - title_top) < 80),
        None,
    )
    if more is None:
        raise ValueError("荣耀精品首发未找到更多入口")
    _tap_node(more)
    time.sleep(3)
    detail_seen: set[str] = set()
    launches, launch_raws = _collect_vertical_ui(
        "honor-launch", lambda xml: _parse_honor_list(xml, "launch"), detail_seen=detail_seen,
    )

    _adb("shell", "input", "keyevent", "4")
    time.sleep(2)
    # 底部“找游戏”固定在普通首页；进入后再点“内测专区”。
    _adb("shell", "input", "tap", "432", "2290")
    time.sleep(4)
    find_xml = _dump_ui("honor-find-games")
    root = ET.fromstring(find_xml)
    beta_entry = next(
        (n for n in root.iter("node") if "内测" in (n.get("text") or "") and n.get("resource-id", "").endswith("/item_three_picture_name")),
        None,
    )
    if beta_entry is None:
        raise ValueError("荣耀找游戏页未找到“内测专区”")
    _tap_node(beta_entry)
    time.sleep(3)
    betas, beta_raws = _collect_vertical_ui(
        "honor-beta", lambda xml: _parse_honor_list(xml, "beta"), detail_seen=detail_seen,
    )
    items = launches + betas
    if not items:
        raise ValueError("荣耀 UI 未读取到精品首发或内测游戏")
    return items, [("new-home", new_xml), ("find-games", find_xml), *launch_raws, *beta_raws]


def _oppo_home_to_new_game() -> bytes:
    _adb("shell", "am", "force-stop", "com.hihonor.gamecenter")
    xml = _start_app("com.nearme.gamecenter", "com.nearme.gamecenter/.ui.activity.SplashActivity", 7)
    root = ET.fromstring(xml)
    target = _node_by_text(root, "新游", max_top=500)
    if target is None:
        raise ValueError("OPPO 游戏中心未找到首页“新游”入口")
    _tap_node(target)
    time.sleep(5)
    new_xml = _dump_ui("oppo-newgame")
    new_root = ET.fromstring(new_xml)
    package = next((n.get("package") for n in new_root.iter("node") if n.get("package")), "")
    if package != "com.nearme.gamecenter":
        raise ValueError("OPPO 新游页被其他 App 抢占前台")
    return new_xml


def _parse_oppo_section(xml: bytes, event_type: str) -> list[dict]:
    root = ET.fromstring(xml)
    parent = {child: node for node in root.iter() for child in node}
    items = []
    for node in root.iter("node"):
        if not (node.get("resource-id", "").endswith("/tv_name") and node.get("text")):
            continue
        if _bounds(node)[1] < 1800:
            continue
        container = node
        for _ in range(5):
            container = parent.get(container)
            if container is None:
                break
            category = _child_text(container, "tv_install_num")
            if category is not None:
                break
        else:
            category = None
        name = node.get("text").strip()
        key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        items.append({
            "source": "oppo_gamecenter",
            "source_item_id": key,
            "name": name,
            "category": None if category == "官服" else category,
            "tags": [category] if category else [],
            "event_type": event_type,
            "event_time": "",
            "status": {"launch": "首发好游", "recruiting_beta": "招募测试", "beta": "内测游戏"}[event_type],
            "raw": {"name": name, "category": category},
        })
    return items


def _oppo_explicit_date(text: str | None, reference: datetime | None = None) -> str:
    """读取 OPPO 卡片文案中的明确事件日，避免把采集日当成首发日。"""
    value = text or ""
    match = None
    for candidate in re.finditer(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", value):
        before = value[max(0, candidate.start() - 12):candidate.start()]
        after = value[candidate.end():candidate.end() + 16]
        follows_event = re.match(
            r"\s*(?:\d{1,2}:\d{2}\s*)?"
            r"(?:首发|正式上线|上线|开测|开启(?:测试|预约)|预下载|公测|内测|首测|终测)",
            after,
        )
        precedes_date = re.search(
            r"(?:首发|正式上线|上线|开测|预下载|公测|内测|首测|终测)"
            r"(?:时间|日期)?\s*$",
            before,
        )
        if follows_event or precedes_date:
            match = candidate
            break
    if match is None:
        return ""
    now = reference or datetime.now().astimezone()
    year_text, month_text, day_text = match.groups()
    month, day = int(month_text), int(day_text)
    year = int(year_text) if year_text else now.year + (
        1 if month < now.month - 6 else -1 if month > now.month + 6 else 0
    )
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def _parse_oppo_today(xml: bytes, reference: datetime | None = None) -> list[dict]:
    root = ET.fromstring(xml)
    parent = {child: node for node in root.iter() for child in node}
    observed_date = (reference or datetime.now().astimezone()).date().isoformat()
    items = []
    for node in root.iter("node"):
        if not (node.get("resource-id", "").endswith("/tv_name") and node.get("text")):
            continue
        if not 700 <= _bounds(node)[1] <= 1500:
            continue
        container, status = node, None
        for _ in range(6):
            container = parent.get(container)
            if container is None:
                break
            status = _child_text(container, "tv_des")
            if status:
                break
        name = node.get("text").strip()
        key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        explicit_date = _oppo_explicit_date(status, reference)
        items.append({
            "source": "oppo_gamecenter",
            "source_item_id": key,
            "name": name,
            "event_type": "reservation" if status and "期待" in status else "launch",
            "event_time": explicit_date or observed_date,
            "status": status,
            "raw": {
                "name": name,
                "status": status,
                "calendar": "today",
                "event_date_source": "status" if explicit_date else "collection_date",
            },
        })
    return items


def _oppo_date(text: str) -> str:
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text.strip())
    if not match:
        return ""
    now = datetime.now().astimezone()
    month, day = int(match.group(1)), int(match.group(2))
    year = now.year + (1 if month < now.month - 6 else -1 if month > now.month + 6 else 0)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_oppo_timeline(xml: bytes, default_type: str, inherited_date: str = "") -> tuple[list[dict], str]:
    root = ET.fromstring(xml)
    parent = {child: node for node in root.iter() for child in node}
    date_nodes = [
        node for node in root.iter("node")
        if node.get("text") and _oppo_date(node.get("text"))
    ]
    # 滚动后的首张卡片常位于新日期标题上方，它仍属于上一屏继承的日期。
    # 只有首屏没有继承日期时，才把当前屏最早的日期标题用作兜底。
    current_date = inherited_date
    if not current_date and date_nodes:
        current_date = _oppo_date(min(date_nodes, key=lambda n: _bounds(n)[1]).get("text"))
    items = []
    games = [
        node for node in root.iter("node")
        if node.get("resource-id", "").endswith("/appName") and node.get("text")
    ]
    for node in sorted(games, key=lambda n: _bounds(n)[1]):
        game_top = _bounds(node)[1]
        preceding = [n for n in date_nodes if _bounds(n)[1] <= game_top]
        if preceding:
            current_date = _oppo_date(max(preceding, key=lambda n: _bounds(n)[1]).get("text"))
        container = node
        for _ in range(6):
            container = parent.get(container)
            if container is None:
                break
            if any(x.get("resource-id", "").endswith("/tagTextView") for x in container.iter("node")):
                break
        descendants = list(container.iter("node")) if container is not None else []
        tags = [
            x.get("text").strip() for x in descendants
            if x.get("resource-id", "").endswith("/tagTextView") and x.get("text")
        ]
        contents = [
            x.get("text").strip() for x in descendants
            if x.get("resource-id", "").endswith("/content") and x.get("text")
        ]
        signal = " ".join(tags)
        if "预下载" in signal:
            event_type = "pre_download"
        elif "首发" in signal:
            event_type = "launch"
        else:
            event_type = default_type
        category = next((x for x in contents if "·" in x), None)
        rating_text = next((x for x in contents if re.fullmatch(r"\d+(?:\.\d+)?", x)), None)
        name = node.get("text").strip()
        key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        items.append({
            "source": "oppo_gamecenter",
            "source_item_id": key,
            "name": name,
            "category": category.split("·", 1)[0].strip() if category else None,
            "tags": [x.strip() for x in category.split("·")] if category else [],
            "rating": float(rating_text) if rating_text else None,
            "event_type": event_type,
            "event_time": current_date,
            "status": signal or {"launch": "首发", "recruiting_beta": "招募测试", "beta": "内测游戏"}[default_type],
            "raw": {"name": name, "tags": tags, "contents": contents, "event_date": current_date},
        })
    # 屏幕底部可能只露出下一组日期标题、尚未露出游戏；下一次滚动应继承它。
    if date_nodes:
        current_date = _oppo_date(max(date_nodes, key=lambda n: _bounds(n)[1]).get("text"))
    return items, current_date


def _enrich_visible_oppo_details(xml: bytes, items: list[dict], detail_seen: set[str]) -> None:
    root = ET.fromstring(xml)
    by_name = {item["name"]: item for item in items}
    candidates = [
        node for node in root.iter("node")
        if node.get("text") in by_name
        and node.get("resource-id", "").endswith(("/appName", "/tv_name"))
    ]
    for node in candidates:
        name = node.get("text").strip()
        item = by_name[name]
        if name in detail_seen or not _ui_detail_enabled("oppo_gamecenter", name):
            continue
        detail_seen.add(name)
        try:
            _tap_node(node)
            time.sleep(2.5)
            detail = _capture_oppo_detail(name)
            _merge_ui_detail(item, detail, source="oppo_gamecenter_app")
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as exc:
            item.setdefault("raw", {})["ui_detail_error"] = str(exc)[:300]
        finally:
            _adb("shell", "input", "keyevent", "4")
            time.sleep(1.2)


def _collect_oppo_timeline(
    prefix: str, event_type: str, detail_seen: set[str] | None = None,
) -> tuple[list[dict], list[tuple[str, bytes]]]:
    found, raws, stable, current_date = {}, [], 0, ""
    detail_seen = detail_seen if detail_seen is not None else set()
    for index in range(9):
        xml = _dump_ui(f"oppo-{prefix}-{index}")
        raws.append((f"{prefix}-{index}", xml))
        items, current_date = _parse_oppo_timeline(xml, event_type, current_date)
        # OPPO 在滚动后才会落新卡片缓存；内部按路径去重，只读新增文件。
        offline_blobs = _oppo_offline_blobs()
        _attach_oppo_offline_metadata(items, offline_blobs)
        _attach_ui_icons(
            xml, _capture_screen(), items, source="oppo_gamecenter",
            name_suffix="appName", icon_suffixes=("appIcon", "iconLayout"),
        )
        _enrich_visible_oppo_details(xml, items, detail_seen)
        before = len(found)
        for item in items:
            found[(item["source_item_id"], item["event_type"], item["event_time"])] = item
        stable = stable + 1 if len(found) == before else 0
        if stable >= 2:
            break
        _adb("shell", "input", "swipe", "720", "2200", "720", "650", "500")
        time.sleep(1)
    return list(found.values()), raws


def collect_oppo_ui() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """采集 OPPO 新游页的首发、招募测试和内测三个稳定分区。"""
    first_xml = _oppo_home_to_new_game()
    found, raws = {}, [("newgame", first_xml)]
    detail_seen: set[str] = set()
    today_items = _parse_oppo_today(first_xml)
    _attach_oppo_offline_metadata(today_items)
    _attach_ui_icons(
        first_xml, _capture_screen(), today_items, source="oppo_gamecenter",
        name_suffix="tv_name", icon_suffixes=("iv_icon",),
    )
    _enrich_visible_oppo_details(first_xml, today_items, detail_seen)
    for item in today_items:
        found[(item["source_item_id"], item["event_type"], item["event_time"])] = item
    timelines = (("首发好游", "launch"), ("招募测试", "recruiting_beta"), ("内测游戏", "beta"))
    for index, (title, event_type) in enumerate(timelines):
        # 详情页多次返回后 ViewPager 偶尔会丢失分区入口；
        # 每个分区从新游首页重新进入，避免前一分区影响后一分区。
        main_xml = first_xml if index == 0 else _oppo_home_to_new_game()
        raws.append((f"main-before-{event_type}", main_xml))
        root = ET.fromstring(main_xml)
        entry = _node_by_text(root, title, min_top=1200)
        if entry is None:
            raws.append((f"missing-{event_type}", main_xml))
            continue
        _tap_node(entry)
        time.sleep(3)
        timeline_items, timeline_raws = _collect_oppo_timeline(
            event_type, event_type, detail_seen=detail_seen,
        )
        for item in timeline_items:
            found[(item["source_item_id"], item["event_type"], item["event_time"])] = item
        raws.extend(timeline_raws)
        _adb("shell", "input", "keyevent", "4")
        time.sleep(2)
    if not found:
        raise ValueError("OPPO 新游分区未读取到游戏")
    return list(found.values()), raws
