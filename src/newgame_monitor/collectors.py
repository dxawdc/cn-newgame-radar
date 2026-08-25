import hashlib
import html as html_lib
import json
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .app_cache_collectors import (
    _xiaomi_event_time,
    collect_honor_ui,
    collect_huawei_cache,
    collect_oppo_ui,
    collect_xiaomi_cache,
)
from .event_quality import classify_233_event, classify_haoyou_event


HEADERS = {"User-Agent": "NewGameMonitor/0.1 (+low-frequency research collector)"}


def _get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response


def _source_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _clean_html_text(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(html_lib.unescape(value), "html.parser")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _complete_description(value: str) -> str | None:
    """列表页常返回带省略号的短片段，不将其冒充完整详情。"""
    text = (value or "").strip()
    if len(text) < 120 or text.endswith(("...", "…")):
        return None
    return text


def collect_vivo() -> tuple[list[dict], list[tuple[str, bytes]]]:
    endpoints = {
        "launch": "https://main.gamecenter.vivo.com.cn/clientRequest/newGameZone/firstPublishList",
        "beta": "https://main.gamecenter.vivo.com.cn/clientRequest/newGameZone/betaTestList",
    }
    items, raws = [], []
    for event_type, url in endpoints.items():
        response = _get(url)
        raws.append((event_type, response.content))
        payload = response.json()
        data = payload.get("data") or {}
        if event_type == "launch":
            groups = [(event_type, data.get("listData") or [])]
        else:
            groups = [
                ("limited_beta", data.get("limitedTestGameList") or []),
                ("important_beta", data.get("importantTestGameList") or []),
                ("beta", data.get("testGameList") or []),
            ]
        for kind, games in groups:
            for game in games:
                timestamp = game.get("firstPublishDate") or game.get("testStartDate")
                event_time = datetime.fromtimestamp(timestamp / 1000).astimezone().isoformat() if timestamp else ""
                size_kib = game.get("size")
                items.append({
                    "source": "vivo_gamecenter",
                    "source_item_id": str(game.get("id") or game.get("pkgName")),
                    "name": game.get("name") or "未知名称",
                    "package_name": game.get("pkgName"),
                    "developer": game.get("gameDeveloper"),
                    "category": game.get("type"),
                    "tags": game.get("tagList") or [],
                    "gameplay_intro": game.get("recommend_desc"),
                    "icon_url": game.get("icon"),
                    "detail_url": game.get("permissionUrl"),
                    "rating": game.get("comment"),
                    "version_name": game.get("versionName") or game.get("versonName"),
                    "size_bytes": int(size_kib) * 1024 if size_kib is not None else None,
                    "event_type": kind,
                    "event_time": event_time,
                    "status": game.get("dateTitle") or game.get("firstPublishTime"),
                    "raw": game,
                })
    return items, raws


def _parse_xiaomi_discovery(payload: dict) -> list[dict]:
    """解析小米游戏中心“内测专区”公开接口。"""
    games: dict[str, dict] = {}

    def find(value) -> None:
        if isinstance(value, dict):
            if value.get("id") and value.get("title") and isinstance(value.get("dInfo"), dict):
                games[str(value["id"])] = value
            for child in value.values():
                find(child)
        elif isinstance(value, list):
            for child in value:
                find(child)

    find(payload.get("data") or {})
    items = []
    for game in games.values():
        detail = game.get("dInfo") or {}
        apk = detail.get("apk") or {}
        testing = detail.get("testing") or {}
        subscribe = detail.get("subscribe") or {}
        tags = [tag.get("name") for tag in game.get("tag") or [] if tag.get("name")]
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
            "event_time": _xiaomi_event_time(testing, subscribe),
            "status": testing.get("name") or subscribe.get("text"),
            "raw": game,
        })
    return items


def collect_xiaomi() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """直连小米游戏中心公开内测专区，不传账号或设备标识。"""
    url = "https://app.knights.mi.com/knights/recommend/simple/page/normal/v6"
    response = requests.get(
        url,
        params={"id": "10003019", "pageSize": "10"},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errCode") != 200:
        raise ValueError(f"小米内测专区返回异常：{payload.get('errCode')}")
    items = _parse_xiaomi_discovery(payload)
    if not items:
        raise ValueError("小米内测专区未返回游戏")
    return items, [("beta-zone-api", response.content)]


APPLE_GAMES_URL = "https://apps.apple.com/cn/iphone/games"
APPLE_ROOM_FALLBACKS = {
    "热门新游 Top30": "https://apps.apple.com/cn/iphone/room/1612594244",
    "新游视频速递": "https://apps.apple.com/cn/iphone/room/1651176719",
}
APPLE_RSS_URLS = {
    "new-apps": "https://itunes.apple.com/cn/rss/newapplications/limit=100/json",
    "new-free-apps": "https://itunes.apple.com/cn/rss/newfreeapplications/limit=100/json",
    "new-paid-apps": "https://itunes.apple.com/cn/rss/newpaidapplications/limit=100/json",
}
APPLE_CHART_URLS = {
    "top-free": "https://rss.marketingtools.apple.com/api/v2/cn/apps/top-free/100/apps.json",
    "top-paid": "https://rss.marketingtools.apple.com/api/v2/cn/apps/top-paid/100/apps.json",
}

_APPLE_GENRE_LABELS = {
    "Action": "动作", "Adventure": "冒险", "Board": "桌面", "Card": "卡牌",
    "Casual": "休闲", "Family": "家庭聚会", "Music": "音乐", "Puzzle": "益智解谜",
    "Racing": "竞速", "Role Playing": "角色扮演", "Simulation": "模拟",
    "Sports": "体育", "Strategy": "策略", "Trivia": "问答", "Word": "字谜",
}
_APPLE_GENERIC_GENRES = {"Games", "游戏", "Entertainment", "娱乐"}


def _apple_add_candidate(candidates: dict[str, set[str]], app_id: object, signal: str) -> None:
    value = str(app_id or "").strip()
    if value.isdigit():
        candidates.setdefault(value, set()).add(signal)


def _parse_apple_legacy_rss(payload: dict) -> list[str]:
    entries = (payload.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    result = []
    for entry in entries:
        app_id = (((entry or {}).get("id") or {}).get("attributes") or {}).get("im:id")
        if str(app_id or "").isdigit():
            result.append(str(app_id))
    return list(dict.fromkeys(result))


def _parse_apple_marketing_feed(payload: dict) -> list[str]:
    result = []
    for entry in (payload.get("feed") or {}).get("results") or []:
        app_id = entry.get("id")
        if str(app_id or "").isdigit():
            result.append(str(app_id))
    return list(dict.fromkeys(result))


def _parse_apple_room_links(content: bytes) -> dict[str, str]:
    """从中国区游戏首页发现当前新游专题，标题变化时保留固定专题作为降级。"""
    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "lxml")
    rooms = {}
    for heading in soup.select('h2[data-test-id="shelf-title"]'):
        title = heading.get_text(" ", strip=True)
        if not any(keyword in title for keyword in ("热门新游", "新游视频")):
            continue
        anchor = heading.find_parent("a", href=True)
        if anchor and "/room/" in anchor["href"]:
            rooms[title] = urljoin(APPLE_GAMES_URL, anchor["href"])
    return rooms or dict(APPLE_ROOM_FALLBACKS)


def _parse_apple_room_ids(content: bytes) -> list[str]:
    soup = BeautifulSoup(content, "lxml")
    result = []
    for anchor in soup.select('a[href*="/app/"]'):
        match = re.search(r"/id(\d+)", anchor.get("href") or "")
        if match:
            result.append(match.group(1))
    return list(dict.fromkeys(result))


def _apple_release_date(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _apple_candidate_is_recent(release_day: date, signals: set[str], observed: date) -> bool:
    """不同发现源使用不同窗口；拒绝把榜单里的多年老游戏重新标为新游。"""
    delta = (observed - release_day).days
    if delta < 0:
        return delta >= -365
    windows = []
    if any(signal.startswith("rss:") for signal in signals):
        windows.append(90)
    if any(signal.startswith("editorial:") for signal in signals):
        windows.append(180)
    if any(signal.startswith("chart:") for signal in signals):
        windows.append(60)
    return bool(windows) and delta <= max(windows)


def _apple_lookup_item(detail: dict, signals: set[str], observed: date) -> dict | None:
    if detail.get("wrapperType") not in (None, "software"):
        return None
    genres = [str(value) for value in detail.get("genres") or [] if value]
    if detail.get("primaryGenreName") not in ("Games", "游戏") and not ({"Games", "游戏"} & set(genres)):
        return None
    release_day = _apple_release_date(detail.get("releaseDate"))
    if release_day is None or not _apple_candidate_is_recent(release_day, signals, observed):
        return None
    subgenres = [
        _APPLE_GENRE_LABELS.get(value, value)
        for value in genres
        if value not in _APPLE_GENERIC_GENRES
    ]
    signal_tags = []
    if any(signal.startswith("editorial:热门新游") for signal in signals):
        signal_tags.append("Apple 热门新游")
    if any(signal.startswith("editorial:新游视频") for signal in signals):
        signal_tags.append("Apple 新游视频")
    if any(signal.startswith("chart:") for signal in signals):
        signal_tags.append("App Store 榜单")
    tags = list(dict.fromkeys(["iOS", "App Store", *subgenres, *signal_tags]))
    future = release_day > observed
    source_labels = []
    if any(signal.startswith("rss:") for signal in signals):
        source_labels.append("中国区新应用 RSS")
    if any(signal.startswith("editorial:") for signal in signals):
        source_labels.append("Apple 新游专题")
    if any(signal.startswith("chart:") for signal in signals):
        source_labels.append("App Store 榜单")
    try:
        rating = float(detail["averageUserRating"]) if detail.get("averageUserRating") is not None else None
    except (TypeError, ValueError):
        rating = None
    try:
        size_bytes = int(detail["fileSizeBytes"]) if detail.get("fileSizeBytes") else None
    except (TypeError, ValueError):
        size_bytes = None
    status = (
        f"App Store 中国区预订，预计 {release_day.isoformat()}"
        if future else
        f"{'、'.join(dict.fromkeys(source_labels))}；App Store 上架日期 {release_day.isoformat()}"
    )
    return {
        "source": "apple_appstore_cn",
        "source_item_id": str(detail.get("trackId")),
        "name": detail.get("trackName") or "未知名称",
        "package_name": detail.get("bundleId"),
        "developer": detail.get("sellerName") or detail.get("artistName"),
        "category": subgenres[0] if subgenres else "游戏",
        "tags": tags,
        "gameplay_intro": detail.get("trackCensoredName") or detail.get("trackName"),
        "full_description": _clean_html_text(detail.get("description")),
        "icon_url": detail.get("artworkUrl512") or detail.get("artworkUrl100"),
        "detail_url": detail.get("trackViewUrl"),
        "rating": rating,
        "version_name": detail.get("version"),
        "size_bytes": size_bytes,
        "event_type": "reservation" if future else "launch",
        "event_time": release_day.isoformat(),
        "status": status,
        "raw": {
            "storefront": "CN",
            "platform": "iOS",
            "discovery_signals": sorted(signals),
            "release_date": detail.get("releaseDate"),
            "current_version_release_date": detail.get("currentVersionReleaseDate"),
            "minimum_os_version": detail.get("minimumOsVersion"),
            "content_advisory_rating": detail.get("contentAdvisoryRating"),
            "seller_url": detail.get("sellerUrl"),
            "supported_devices": detail.get("supportedDevices") or [],
            "screenshot_urls": detail.get("screenshotUrls") or [],
        },
    }


def collect_apple_ios_cn() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """Apple 免费链路：中国区新应用、编辑新游专题、榜单与公开详情。"""
    candidates: dict[str, set[str]] = {}
    raws: list[tuple[str, bytes]] = []

    try:
        games_response = _get(APPLE_GAMES_URL)
        raws.append(("games-home", games_response.content))
        room_urls = _parse_apple_room_links(games_response.content)
    except (requests.RequestException, ValueError) as exc:
        room_urls = dict(APPLE_ROOM_FALLBACKS)
        raws.append(("error-games-home", str(exc).encode("utf-8")))
    for title, room_url in room_urls.items():
        safe_title = "hot-new" if "热门新游" in title else "new-video"
        try:
            room_response = _get(room_url)
            raws.append((f"room-{safe_title}", room_response.content))
            for app_id in _parse_apple_room_ids(room_response.content):
                _apple_add_candidate(candidates, app_id, f"editorial:{title}")
        except (requests.RequestException, ValueError) as exc:
            raws.append((f"error-room-{safe_title}", str(exc).encode("utf-8")))

    for label, url in APPLE_RSS_URLS.items():
        try:
            response = _get(url)
            raws.append((f"rss-{label}", response.content))
            for app_id in _parse_apple_legacy_rss(response.json()):
                _apple_add_candidate(candidates, app_id, f"rss:{label}")
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            raws.append((f"error-rss-{label}", str(exc).encode("utf-8")))

    for label, url in APPLE_CHART_URLS.items():
        try:
            response = _get(url)
            raws.append((f"chart-{label}", response.content))
            for app_id in _parse_apple_marketing_feed(response.json()):
                _apple_add_candidate(candidates, app_id, f"chart:{label}")
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            raws.append((f"error-chart-{label}", str(exc).encode("utf-8")))

    if not candidates:
        raise ValueError("Apple 免费链路未发现候选 App ID")

    details = []
    app_ids = list(candidates)
    for index in range(0, len(app_ids), 100):
        chunk = app_ids[index:index + 100]
        label = f"lookup-{index // 100 + 1}"
        try:
            response = requests.get(
                "https://itunes.apple.com/lookup",
                params={"id": ",".join(chunk), "country": "cn"},
                headers=HEADERS,
                timeout=30,
            )
            response.raise_for_status()
            raws.append((label, response.content))
            details.extend((response.json() or {}).get("results") or [])
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            raws.append((f"error-{label}", str(exc).encode("utf-8")))
        if index + 100 < len(app_ids):
            time.sleep(0.25)

    observed = datetime.now().astimezone().date()
    items = []
    for detail in details:
        app_id = str(detail.get("trackId") or "")
        item = _apple_lookup_item(detail, candidates.get(app_id, set()), observed)
        if item:
            items.append(item)
    if not items:
        raise ValueError("Apple 免费链路没有符合时间窗口的中国区新游戏")
    return items, raws


def collect_3839() -> tuple[list[dict], list[tuple[str, bytes]]]:
    url = "https://m.3839.com/timeline.html"
    response = _get(url)
    soup = BeautifulSoup(response.content, "lxml")
    items = []
    now = datetime.now().astimezone()
    for panel in soup.select(".NG-panel"):
        label = panel.select_one(":scope > .label")
        match = re.search(r"(\d{2})月(\d{2})日", label.get_text(" ", strip=True) if label else "")
        if not match:
            continue
        month, day = map(int, match.groups())
        year = now.year + (1 if month < now.month - 6 else -1 if month > now.month + 6 else 0)
        event_date = f"{year:04d}-{month:02d}-{day:02d}"
        for li in panel.select(":scope > .gameRow li"):
            anchor = li.select_one(".g-left > a")
            name_node = li.select_one(".g-name em")
            if not anchor or not name_node:
                continue
            detail_url = urljoin(url, anchor.get("href", ""))
            icon = li.select_one(".g-icon img")
            info = li.select_one(".g-info")
            score = li.select_one(".score")
            status = li.select_one(".g-type")
            tags = [x.get_text(strip=True) for x in li.select(".g-tags i")]
            intro = info.get_text(" ", strip=True) if info else None
            if score and intro:
                intro = intro.removeprefix(score.get_text(strip=True)).strip()
            name = name_node.get_text(strip=True)
            event_type = classify_haoyou_event(name, intro or "")
            if event_type is None:
                continue
            items.append({
                "source": "haoyou_kuaibao",
                "source_item_id": _source_id(detail_url),
                "name": name,
                "category": tags[0] if tags else None,
                "tags": tags,
                "gameplay_intro": intro,
                "icon_url": urljoin(url, (icon.get("lz_src") or icon.get("src"))) if icon else None,
                "detail_url": detail_url,
                "rating": float(score.get_text(strip=True)) if score and re.fullmatch(r"\d+(?:\.\d+)?", score.get_text(strip=True)) else None,
                "event_type": event_type,
                "event_time": event_date,
                "status": intro,
                "raw": {
                    "date_label": label.get_text(" ", strip=True),
                    "text": li.get_text(" ", strip=True),
                    "action": status.get_text(strip=True) if status else None,
                },
            })
    # 页面同时保留桌面/移动两套相同卡片时，先在采集批次内去重，避免审计计数虚高。
    unique = {
        (item["source_item_id"], item["event_type"], item["event_time"]): item
        for item in items
    }
    return list(unique.values()), [("timeline", response.content)]


def _parse_4399_event_date(text: str, observed: datetime | None = None) -> str:
    """标准化 4399 表格日期；缺少年份或具体日期时不补造精度。"""
    value = re.sub(r"\s+", "", text or "")
    full = re.fullmatch(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", value)
    if full:
        try:
            return date(*map(int, full.groups())).isoformat()
        except ValueError:
            return ""
    # 4399 的隐藏开测表长期混有历史产品，且只显示 MM-DD。不能据采集年份
    # 推断事件年份，否则会把旧游戏错误标成当年或次年的新游。
    return ""


def collect_4399() -> tuple[list[dict], list[tuple[str, bytes]]]:
    url = "https://fahao.4399.cn/"
    response = _get(url)
    # 页面响应头未声明编码，但当前正文实际为 UTF-8。
    html = response.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    items, raws = [], [("lists", response.content)]
    observed = datetime.now().astimezone()
    for selector, event_type in (("table.j-kftable", "reservation"), ("table.j-kctable", "beta")):
        table = soup.select_one(selector)
        if not table:
            continue
        for row in table.select("tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            anchor = row.select_one("a[href]")
            name = cells[0].get_text(" ", strip=True)
            if not name or name == "游戏名":
                continue
            event_text = cells[1].get_text(" ", strip=True)
            event_time = _parse_4399_event_date(event_text, observed)
            # 开测表的 MM-DD 无年份且包含历史记录，无法证明是本年度事件；
            # 不把这些记录作为“新游动态”入库。预约表即使日期待定，仍可作为新发现保留。
            if event_type == "beta" and not event_time:
                continue
            detail_url = urljoin(url, anchor.get("href")) if anchor else None
            items.append({
                "source": "4399_gamebox",
                "source_item_id": _source_id(detail_url or name),
                "name": name,
                "detail_url": detail_url,
                "event_type": event_type,
                "event_time": event_time,
                "status": cells[2].get_text(" ", strip=True) if event_type == "beta" and len(cells) > 2 else None,
                "raw": {
                    "cells": [c.get_text(" ", strip=True) for c in cells],
                    "event_date_text": event_text,
                },
            })
    # 列表表格不含图片与厂商；公开详情页可补全这些字段。
    for item in items:
        detail_url = item.get("detail_url")
        if not detail_url:
            continue
        try:
            detail_response = requests.get(detail_url, headers=HEADERS, timeout=20)
            if detail_response.status_code != 200:
                continue
        except requests.RequestException:
            continue
        detail_soup = BeautifulSoup(detail_response.content, "lxml")
        icon = next(
            (
                node for node in detail_soup.select("img.m_icon[src]")
                if not node.get("alt") or node.get("alt").strip() == item["name"]
            ),
            detail_soup.select_one("img.m_icon[src]"),
        )
        developer_node = detail_soup.select_one("li.m_game_dev")
        intro_node = detail_soup.select_one("#j-game-summary")
        if icon:
            item["icon_url"] = urljoin(detail_url, icon.get("src"))
        if developer_node:
            item["developer"] = re.sub(r"^厂商[：:]\s*", "", developer_node.get_text(" ", strip=True)) or None
        if intro_node:
            item["gameplay_intro"] = intro_node.get_text(" ", strip=True) or None
        item["raw"]["detail_title"] = detail_soup.title.get_text(strip=True) if detail_soup.title else None
        raws.append((f"detail-{item['source_item_id']}", detail_response.content))
    return items, raws


def _classify_9game_event(name: str, status: str) -> str | None:
    """九游开测表混有赛季、活动和版本内容，只保留整款产品事件。"""
    game_name = re.sub(r"\s+", "", name or "")
    text = re.sub(r"\s+", "", status or "")
    if not game_name or not text:
        return None
    if re.search(r"(?:体验服|先遣服|先锋服|怀旧服|测试服)", game_name):
        return None
    if re.search(r"(?:活动|赛季|联动|版本|更新|返场|专属|来袭|重磅)", text):
        return None
    if "预下载" in text:
        return "pre_download"
    if re.search(r"(?:首发|公测|正式上线|全平台上线)", text):
        return "launch"
    if re.search(r"(?:测试|内测|首测|终测|开测)", text):
        return "limited_beta" if re.search(r"(?:限量|抢号)", text) else "beta"
    if re.search(r"(?:预约|即将)", text):
        return "reservation"
    return None


def _parse_9game_day(label: str, observed: datetime | None = None) -> str:
    """把九游日期分组转成完整日期；仅公布月份时保留为空，避免伪造具体日。"""
    current = (observed or datetime.now().astimezone()).date()
    text = re.sub(r"\s+", "", label or "")
    if text == "今天":
        return current.isoformat()
    if text == "明天":
        return (current + timedelta(days=1)).isoformat()
    full = re.fullmatch(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if full:
        return date(*map(int, full.groups())).isoformat()
    short = re.fullmatch(r"(\d{1,2})(?:-|/|月)(\d{1,2})日?", text)
    if not short:
        return ""
    month, day = map(int, short.groups())
    year = current.year
    if month < current.month - 6:
        year += 1
    elif month > current.month + 6:
        year -= 1
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _parse_9game_schedule_html(
    content: bytes, observed: datetime | None = None, recent_days: int = 14,
) -> list[dict]:
    """解析九游公开开测表中的即将/近期产品事件。"""
    current = observed or datetime.now().astimezone()
    soup = BeautifulSoup(content, "lxml")
    items = []
    for container in soup.select(".des-table1, .des-table2"):
        day_node = container.find("div", class_="day", recursive=False)
        table = container.find("table", recursive=False)
        if day_node is None or table is None:
            continue
        day_label = day_node.get_text(" ", strip=True)
        event_time = _parse_9game_day(day_label, current)
        if "des-table2" in (container.get("class") or []) and event_time:
            if date.fromisoformat(event_time) < current.date() - timedelta(days=recent_days):
                continue
        for row in table.select("tr"):
            anchor = row.select_one("td.nametr a.name[href]")
            if anchor is None:
                continue
            name = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            status_node = row.select_one("td.stattr") or row.select_one("td.timetr")
            status = status_node.get_text(" ", strip=True) if status_node else ""
            event_type = _classify_9game_event(name, status)
            if event_type is None:
                continue
            game_id_node = row.select_one("[data-gameid]")
            game_id = game_id_node.get("data-gameid") if game_id_node else None
            if not game_id:
                match = re.search(r"game-(\d+)", anchor.get("data-statis") or "")
                game_id = match.group(1) if match else _source_id(urljoin("https://www.9game.cn/kc/", anchor["href"]))
            image = row.select_one("td.nametr img")
            category_node = row.select_one("td.typetr")
            detail_url = urljoin("https://www.9game.cn/kc/", anchor["href"])
            items.append({
                "source": "uc_9game",
                "source_item_id": str(game_id),
                "name": name,
                "category": category_node.get_text(" ", strip=True) if category_node else None,
                "tags": [],
                "icon_url": urljoin(detail_url, image.get("xlazyimg") or image.get("src")) if image else None,
                "detail_url": detail_url,
                "event_type": event_type,
                "event_time": event_time,
                "status": status,
                "raw": {
                    "schedule_day": day_label,
                    "schedule_status": status,
                    "schedule_group": "recent" if "des-table2" in (container.get("class") or []) else "upcoming",
                },
            })
    unique = {
        (item["source_item_id"], item["event_type"], item["event_time"]): item
        for item in items
    }
    return list(unique.values())


def _parse_9game_detail_html(content: bytes) -> dict:
    """解析九游游戏专区首屏中的开发者、完整介绍、Icon 和分类。"""
    soup = BeautifulSoup(content, "lxml")
    name_node = soup.select_one(".ngame-title a, h1.ngame-title")
    icon = soup.select_one(".ngame-img img[src]")
    developer_node = soup.select_one(".ngame-desc .company")
    developer = re.sub(
        r"^(?:开发者|开发商|厂商)\s*[：:]\s*", "",
        developer_node.get_text(" ", strip=True) if developer_node else "",
    ).strip()
    description_node = soup.select_one(".ngame-desc .tips .txt") or soup.select_one(".ngame-desc")
    description = ""
    if description_node is not None:
        clone = BeautifulSoup(str(description_node), "lxml")
        for node in clone.select(".company, .more"):
            node.decompose()
        description = _clean_html_text(str(clone))
    points = [node.get_text(" ", strip=True) for node in soup.select(".ngame-types .point")]
    screenshot_nodes = list(soup.select('img[alt^="截图"]'))
    if not screenshot_nodes:
        screenshot_nodes = list(soup.select(".special-img img"))
    if not screenshot_nodes:
        previews = list(soup.select('img[class*="topbanner--preview_img"]'))
        screenshot_nodes = [node for node in previews if (node.get("alt") or "").strip()]
    screenshot_urls = []
    for node in screenshot_nodes:
        value = node.get("src") or node.get("data-src") or node.get("xlazyimg")
        if value:
            screenshot_urls.append(urljoin("https://www.9game.cn/", value))
    return {
        "name": name_node.get_text(" ", strip=True) if name_node else None,
        "developer": developer or None,
        "category": points[0] if points else None,
        "description": description if len(description) >= 40 else None,
        "icon_url": urljoin("https://www.9game.cn/", icon.get("src")) if icon else None,
        "screenshot_urls": list(dict.fromkeys(screenshot_urls))[:10],
    }


def collect_9game() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """采集 UC 九游公开开测表，并逐项补全游戏专区详情。"""
    schedule_url = "https://www.9game.cn/kc/"
    response = _get(schedule_url)
    items = _parse_9game_schedule_html(response.content)
    if not items:
        raise ValueError("九游开测表未解析到有效新游事件")
    raws = [("schedule", response.content)]
    for index, item in enumerate(items):
        if index:
            time.sleep(0.12)
        try:
            detail_response = _get(item["detail_url"])
        except requests.RequestException:
            continue
        parsed = _parse_9game_detail_html(detail_response.content)
        item["developer"] = parsed.get("developer")
        item["category"] = item.get("category") or parsed.get("category")
        item["full_description"] = parsed.get("description")
        item["gameplay_intro"] = re.sub(r"\s+", " ", parsed.get("description") or "")[:180] or None
        item["icon_url"] = item.get("icon_url") or parsed.get("icon_url")
        item["raw"]["detail"] = {
            key: value for key, value in parsed.items() if value not in (None, "")
        }
        raws.append((f"detail-{item['source_item_id']}", detail_response.content))
    return items, raws


def _decode_nuxt_devalue(payload: list):
    """解析 Nuxt `__NUXT_DATA__` 使用的引用数组格式。"""
    memo, resolving = {}, set()

    def resolve(value):
        if not isinstance(value, int):
            return value
        if value in memo:
            return memo[value]
        if value in resolving:
            return None
        resolving.add(value)
        encoded = payload[value]
        if isinstance(encoded, dict):
            decoded = {}
            memo[value] = decoded
            decoded.update({key: resolve(item) for key, item in encoded.items()})
        elif isinstance(encoded, list):
            wrappers = {"Reactive", "Ref", "ShallowReactive", "ShallowRef"}
            if encoded and isinstance(encoded[0], str) and encoded[0] in wrappers:
                decoded = resolve(encoded[1])
            else:
                decoded = [resolve(item) for item in encoded]
            memo[value] = decoded
        else:
            decoded = encoded
            memo[value] = decoded
        resolving.remove(value)
        return decoded

    return resolve(0)


def _walk_taptap_events(value, found: dict):
    if isinstance(value, dict):
        if "game_id" in value and "app_card_info" in value and "id" in value:
            found[str(value["id"])] = value
            return
        for child in value.values():
            _walk_taptap_events(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_taptap_events(child, found)


def collect_taptap() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """解析 TapTap 公开 upcoming 页的 SSR 数据，不调用 robots 禁止的 webapi。"""
    url = "https://www.taptap.cn/upcoming"
    response = _get(url)
    soup = BeautifulSoup(response.content, "html.parser")
    state = soup.find("script", id="__NUXT_DATA__")
    if not state or not state.string:
        raise ValueError("TapTap 页面未找到 __NUXT_DATA__")
    root = _decode_nuxt_devalue(json.loads(state.string))
    events = {}
    _walk_taptap_events(root.get("data", {}), events)
    items = []
    event_type_map = {
        "首发": "launch",
        "上线": "launch",
        "正式上线": "launch",
        "测试": "beta",
        "限量测试": "limited_beta",
        "删档测试": "beta",
        "不删档测试": "beta",
        "预下载": "pre_download",
    }
    for event in events.values():
        app = event.get("app_card_info") or {}
        event_title = event.get("sub_event_type_title") or (event.get("event_type_info") or {}).get("title") or "event"
        developers = app.get("developers") or []
        author = next((x.get("name") for x in developers if x.get("type") == "author"), None)
        developer = author or next((x.get("name") for x in developers if x.get("name")), None)
        tags = [x.get("value") for x in app.get("tags") or [] if x.get("value")]
        description = _clean_html_text((app.get("description") or {}).get("text") or "")
        rating = ((app.get("stat") or {}).get("rating") or {}).get("score")
        try:
            rating = float(rating) if rating not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        start_time = event.get("start_time")
        event_time = datetime.fromtimestamp(start_time).astimezone().isoformat() if start_time else ""
        items.append({
            "source": "taptap",
            "source_item_id": str(event.get("game_id") or app.get("id")),
            "name": app.get("title") or "未知名称",
            "package_name": app.get("identifier") or None,
            "developer": developer,
            "category": tags[0] if tags else None,
            "tags": tags,
            "gameplay_intro": app.get("rec_text") or description or None,
            "full_description": _complete_description(description),
            "icon_url": (app.get("icon") or {}).get("original_url") or (app.get("icon") or {}).get("url"),
            "detail_url": urljoin(url, event.get("landing_page_uri") or f"/app/{event.get('game_id')}"),
            "rating": rating,
            "event_type": event_type_map.get(event_title, event_title),
            "event_time": event_time,
            "status": event_title,
            "raw": event,
        })
    if not items:
        raise ValueError("TapTap SSR 数据中未解析到游戏事件")
    return items, [("upcoming", response.content)]


def _find_233_banner_games(value, found: dict[str, dict]) -> None:
    """从 233 首页 SSR 数据中找出指向游戏详情的运营卡片。"""
    if isinstance(value, dict):
        param = value.get("param1")
        if isinstance(param, str) and "key_game_id=" in param:
            try:
                config = json.loads(param)
            except json.JSONDecodeError:
                config = {}
            match = re.search(r"key_game_id=(\d+)", config.get("jumpUrl", ""))
            if match:
                found[match.group(1)] = {**value, "_config": config}
        for child in value.values():
            _find_233_banner_games(child, found)
    elif isinstance(value, list):
        for child in value:
            _find_233_banner_games(child, found)


def _find_233_detail(value, game_id: str) -> dict | None:
    if isinstance(value, dict):
        if str(value.get("id")) == game_id and value.get("appName") and value.get("iconUrl"):
            return value
        for child in value.values():
            found = _find_233_detail(child, game_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_233_detail(child, game_id)
            if found:
                return found
    return None


def _parse_233_event_date(text: str, fallback_ms: int | None) -> str:
    full = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if full:
        return date(*map(int, full.groups())).isoformat()
    match = re.search(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日", text)
    if match:
        now = datetime.now().astimezone()
        year = int(match.group(1)) if match.group(1) else now.year
        month, day = int(match.group(2)), int(match.group(3))
        if not match.group(1):
            year += 1 if month < now.month - 6 else -1 if month > now.month + 6 else 0
        return f"{year:04d}-{month:02d}-{day:02d}"
    if fallback_ms:
        return datetime.fromtimestamp(fallback_ms / 1000).astimezone().date().isoformat()
    return ""


def _resolve_233_event_date(
    event_type: str, signal: str, banner: dict, detail: dict,
) -> str:
    """按业务时间优先级解析 233 事件日期。

    首页 Banner 的 ``effectiveTimeBegin`` 是运营素材投放时间，会随活动换图而变化；
    首发必须优先使用游戏详情的 ``onlineTime``，不能把投放日当上线日。
    """
    fallback = _parse_233_event_date(signal, banner.get("effectiveTimeBegin"))
    if event_type == "launch":
        online_time = str(detail.get("onlineTime") or "")
        parsed = _parse_233_event_date(online_time, None)
        if parsed and fallback:
            distance = abs((date.fromisoformat(parsed) - date.fromisoformat(fallback)).days)
            if distance <= 90:
                return parsed
        elif parsed and abs((date.fromisoformat(parsed) - datetime.now().astimezone().date()).days) <= 90:
            return parsed
    return fallback


def collect_233() -> tuple[list[dict], list[tuple[str, bytes]]]:
    """采集 233 乐园官网首页明确标注的上线、预约和测试卡片，并用详情页补全字段。"""
    home_url = "https://www.233leyuan.com/"
    response = _get(home_url)
    soup = BeautifulSoup(response.content, "html.parser")
    state = soup.find("script", id="__NUXT_DATA__")
    if not state or not state.string:
        raise ValueError("233 首页未找到 __NUXT_DATA__")
    payload_response = _get(urljoin(home_url, state.get("data-src") or "/_payload.json"))
    root = _decode_nuxt_devalue(json.loads(payload_response.content.decode("utf-8")))
    banners: dict[str, dict] = {}
    _find_233_banner_games(root, banners)
    keywords = ("上线", "首发", "预下载", "预约", "测试", "开测", "发售")
    selected = {
        game_id: banner for game_id, banner in banners.items()
        if any(word in " ".join([
            str(banner.get("name") or ""),
            str((banner.get("_config") or {}).get("content") or ""),
            str((banner.get("_config") or {}).get("buttonText") or ""),
        ]) for word in keywords)
    }
    if not selected:
        raise ValueError("233 首页没有解析到明确的新游/测试运营卡片")

    find_response = _get("https://www.233leyuan.com/find/_payload.json")
    find_root = _decode_nuxt_devalue(json.loads(find_response.content.decode("utf-8")))
    items, raws = [], [
        ("home", response.content), ("home-payload", payload_response.content),
        ("find-payload", find_response.content),
    ]
    for game_id, banner in selected.items():
        detail_url = f"https://www.233leyuan.com/game-detail/{game_id}"
        payload_url = f"{detail_url}/_payload.json"
        detail_response = _get(payload_url)
        raws.append((f"detail-{game_id}", detail_response.content))
        decoded = _decode_nuxt_devalue(json.loads(detail_response.content.decode("utf-8")))
        detail = _find_233_detail(decoded, game_id) or {}
        config = banner.get("_config") or {}
        signal = " ".join([
            str(banner.get("name") or ""), str(config.get("content") or ""),
            str(config.get("buttonText") or ""), str(detail.get("testStatus") or ""),
        ])
        event_type = classify_233_event(signal)
        raw_tags = detail.get("tags") or []
        tags = []
        for tag in raw_tags:
            value = tag.get("name") if isinstance(tag, dict) else tag
            if value:
                tags.append(str(value))
        intro = detail.get("briefIntro") or detail.get("shortDescription") or config.get("content")
        full_description = detail.get("description") or detail.get("shortDescription")
        event_time = _resolve_233_event_date(event_type, signal, banner, detail)
        items.append({
            "source": "233_leyuan",
            "source_item_id": game_id,
            "name": detail.get("appName") or detail.get("displayName") or banner.get("name") or "未知名称",
            "package_name": detail.get("originPackageName") or detail.get("packageName"),
            "developer": detail.get("manufacturer") or detail.get("authorName"),
            "category": tags[0] if tags else None,
            "tags": tags,
            "gameplay_intro": intro,
            "full_description": full_description,
            "icon_url": detail.get("iconUrl256") or detail.get("iconUrl") or config.get("imageUrl"),
            "detail_url": detail_url,
            "rating": float(detail["rating"]) if detail.get("rating") not in (None, "") else None,
            "version_name": detail.get("appVersionName"),
            "size_bytes": detail.get("fileSize64") or detail.get("fileSize") or None,
            "event_type": event_type,
            "event_time": event_time,
            "status": str(config.get("content") or banner.get("name") or "")[:200],
            "raw": {"banner": banner, "detail": detail},
        })
    latest = next(
        (
            value for key, value in (find_root.get("data") or {}).items()
            if key.startswith("find-index-cambrian-latest") and isinstance(value, dict) and value.get("id")
        ),
        None,
    )
    if latest and str(latest["id"]) not in selected:
        raw_tags = latest.get("gameTags") or latest.get("tags") or []
        tags = [
            str(tag.get("name") if isinstance(tag, dict) else tag)
            for tag in raw_tags
            if (tag.get("name") if isinstance(tag, dict) else tag)
        ]
        create_time = str(latest.get("createTime") or "").replace(" ", "T")
        items.append({
            "source": "233_leyuan",
            "source_item_id": str(latest["id"]),
            "name": latest.get("appName") or latest.get("displayName") or "未知名称",
            "package_name": latest.get("originPackageName") or latest.get("packageName"),
            "developer": latest.get("manufacturer") or latest.get("authorName"),
            "category": tags[0] if tags else None,
            "tags": tags,
            "gameplay_intro": latest.get("briefIntro") or latest.get("shortDescription"),
            "full_description": latest.get("description") or latest.get("shortDescription"),
            "icon_url": latest.get("iconUrl256") or latest.get("iconUrl"),
            "detail_url": f"https://www.233leyuan.com/game-detail/{latest['id']}",
            "rating": float(latest["rating"]) if latest.get("rating") not in (None, "") else None,
            "version_name": latest.get("appVersionName"),
            "size_bytes": latest.get("fileSize64") or latest.get("fileSize") or None,
            "event_type": "new_listing",
            "event_time": create_time,
            "status": "233 发现页最新收录",
            "raw": latest,
        })
    return items, raws


COLLECTORS = {
    "ios-cn": collect_apple_ios_cn,
    "vivo": collect_vivo,
    "xiaomi": collect_xiaomi,
    "3839": collect_3839,
    "4399": collect_4399,
    "taptap": collect_taptap,
    "233": collect_233,
    "9game": collect_9game,
    "huawei-cache": collect_huawei_cache,
    "xiaomi-cache": collect_xiaomi_cache,
    "honor-ui": collect_honor_ui,
    "oppo-ui": collect_oppo_ui,
}
