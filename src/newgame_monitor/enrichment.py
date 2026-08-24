"""对历史来源事件执行不改变事件语义的字段补全。"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewGameMonitor/1.0"}
HUAWEI_WEB_BASE = "https://web-drcn.hispace.dbankcloud.com/edge"
HUAWEI_WEB_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Referer": "https://appgallery.huawei.com/",
    "Origin": "https://appgallery.huawei.com",
}


def _plain_text(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def backfill_full_descriptions(conn: sqlite3.Connection) -> dict:
    """从已留存的来源原始数据无损回填完整游戏介绍。"""
    rows = list(conn.execute(
        """
        SELECT id, source, gameplay_intro, raw_json FROM source_items
        WHERE full_description IS NULL OR full_description=''
        """
    ))
    updated = 0
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        description = ""
        if row["source"] == "huawei_gamecenter":
            description = _plain_text(raw.get("description"))
        elif row["source"] == "233_leyuan":
            detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else raw
            description = _plain_text(detail.get("description") or detail.get("shortDescription"))
        elif row["source"] == "taptap":
            app = raw.get("app_card_info") or {}
            description = _plain_text((app.get("description") or {}).get("text"))
            if len(description) < 120 or description.endswith(("...", "…")):
                description = ""
        elif row["source"] == "4399_gamebox" and len(row["gameplay_intro"] or "") >= 80:
            description = (row["gameplay_intro"] or "").strip()
        if not description:
            continue
        conn.execute("UPDATE source_items SET full_description=? WHERE id=?", (description, row["id"]))
        updated += 1
    conn.commit()
    return {"checked": len(rows), "updated": updated}


def _parse_vivo_appointment_detail(payload: dict, package_name: str) -> dict | None:
    appointment = ((payload.get("data") or {}).get("appointment") or {})
    if payload.get("retcode") != 0 or appointment.get("pkgName") != package_name:
        return None
    tags = [
        item.get("name") for item in appointment.get("contentTags") or []
        if item.get("name")
    ]
    return {
        "name": appointment.get("name"),
        "package_name": appointment.get("pkgName"),
        "developer": appointment.get("gameDeveloper"),
        "category": appointment.get("gameType"),
        "tags": tags,
        "description": _plain_text(appointment.get("desc")),
        "brief": _plain_text(appointment.get("editorRecommend")),
        "icon_url": appointment.get("icon"),
        "screenShots": (
            (appointment.get("burstInfo") or {}).get("screenShots")
            or appointment.get("screenShots") or []
        ),
    }


def _parse_4399_info_data(content: bytes) -> dict:
    text = content.decode("utf-8", errors="replace")
    marker = re.search(r"window\.infoData\s*=\s*", text)
    if marker is None:
        return {}
    try:
        info = json.JSONDecoder().raw_decode(text[marker.end():])[0]
    except (json.JSONDecodeError, TypeError):
        return {}
    developer = info.get("devname") or ((info.get("dev") or {}).get("name"))
    developer = re.sub(r"^开发商\s*[：:]\s*", "", developer or "").strip()
    screenshot_urls = [
        f"https://f1.img4399.com/sj~{value}"
        for value in info.get("screenpath") or []
        if isinstance(value, str) and value
    ]
    return {
        "developer": developer or None,
        "category": info.get("kind") or None,
        "description": _plain_text(info.get("appinfo")),
        "brief": _plain_text(info.get("review")),
        "icon_url": info.get("pic") or None,
        "version_name": info.get("version") or None,
        "tags": [item.get("name") for item in info.get("tag") or [] if item.get("name")],
        "screenshot_urls": screenshot_urls[:10],
    }


def enrich_vivo_public_details(conn: sqlite3.Connection) -> dict:
    """用 vivo 匿名预约详情接口补完整介绍，严格校验包名。"""
    rows = list(conn.execute(
        """
        SELECT MIN(id) AS id,package_name FROM source_items
        WHERE source='vivo_gamecenter' AND package_name IS NOT NULL AND package_name<>''
          AND length(trim(COALESCE(full_description, ''))) < 120
        GROUP BY package_name
        """
    ))
    updated = unavailable = failed = 0
    url = "https://main.gamecenter.vivo.com.cn/clientRequest/queryAppointmentDetail"
    for row in rows:
        try:
            response = requests.get(
                url, params={"pkgName": row["package_name"]}, headers=HEADERS, timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            parsed = _parse_vivo_appointment_detail(payload, row["package_name"])
            if parsed is None:
                unavailable += 1
                continue
            matching = list(conn.execute(
                "SELECT id,developer,category,tags_json,gameplay_intro,full_description,icon_url,raw_json "
                "FROM source_items WHERE source='vivo_gamecenter' AND package_name=?",
                (row["package_name"],),
            ))
            for target in matching:
                try:
                    raw = json.loads(target["raw_json"] or "{}")
                except json.JSONDecodeError:
                    raw = {}
                raw["vivo_appointment_detail"] = {
                    key: value for key, value in parsed.items() if value not in (None, "", [])
                }
                try:
                    old_tags = json.loads(target["tags_json"] or "[]")
                except json.JSONDecodeError:
                    old_tags = []
                tags = list(dict.fromkeys([*old_tags, *parsed["tags"]]))
                old_description = (target["full_description"] or "").strip()
                description = (
                    parsed["description"]
                    if len(parsed["description"] or "") > len(old_description)
                    else old_description
                )
                old_intro = (target["gameplay_intro"] or "").strip()
                intro_candidate = parsed["brief"] or parsed["description"][:180]
                intro = intro_candidate if len(intro_candidate or "") > len(old_intro) else old_intro
                conn.execute(
                    """
                    UPDATE source_items SET developer=?,category=?,tags_json=?,gameplay_intro=?,
                      full_description=?,icon_url=?,raw_json=? WHERE id=?
                    """,
                    (
                        target["developer"] or parsed["developer"],
                        target["category"] or parsed["category"],
                        json.dumps(tags, ensure_ascii=False), intro or None, description or None,
                        target["icon_url"] or parsed["icon_url"],
                        json.dumps(raw, ensure_ascii=False, separators=(",", ":")), target["id"],
                    ),
                )
            conn.commit()
            updated += len(matching)
            time.sleep(0.15)
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
            failed += 1
    return {
        "checked": len(rows), "updated": updated,
        "unavailable": unavailable, "failed": failed,
    }


def _find_taptap_app(value, app_id: str) -> dict | None:
    if isinstance(value, dict):
        if str(value.get("id")) == app_id and value.get("title") and value.get("description"):
            return value
        for child in value.values():
            found = _find_taptap_app(child, app_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_taptap_app(child, app_id)
            if found:
                return found
    return None


def _find_taptap_detail_content(value) -> dict:
    """详情页将完整正文与 app 基础对象拆开，选择最长的主介绍节点。"""
    candidates = []
    if isinstance(value, dict):
        description = value.get("description")
        if isinstance(description, dict) and isinstance(description.get("text"), str):
            candidates.append(value)
        for child in value.values():
            nested = _find_taptap_detail_content(child)
            if nested:
                candidates.append(nested)
    elif isinstance(value, list):
        for child in value:
            nested = _find_taptap_detail_content(child)
            if nested:
                candidates.append(nested)
    return max(
        candidates,
        key=lambda item: len(((item.get("description") or {}).get("text") or "")),
        default={},
    )


def enrich_taptap_descriptions(conn: sqlite3.Connection, delay: float = 0.35) -> dict:
    """从 robots 允许访问的 TapTap 公开详情页补采完整介绍与图集。"""
    from .collectors import _decode_nuxt_devalue

    rows = list(conn.execute(
        """
        SELECT MIN(id) AS id, source_item_id, detail_url
        FROM source_items
        WHERE source='taptap' AND (
            full_description IS NULL OR full_description='' OR
            raw_json NOT LIKE '%"detail_screenshots"%'
          )
          AND detail_url LIKE 'https://www.taptap.cn/app/%'
        GROUP BY source_item_id, detail_url
        ORDER BY MIN(id)
        """
    ))
    updated = failed = 0
    for index, row in enumerate(rows):
        if index and delay:
            time.sleep(delay)
        try:
            response = requests.get(row["detail_url"], headers=HEADERS, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            state = soup.find("script", id="__NUXT_DATA__")
            if not state or not state.string:
                raise ValueError("详情页缺少 __NUXT_DATA__")
            root = _decode_nuxt_devalue(json.loads(state.string))
            app = _find_taptap_app(root, row["source_item_id"]) or {}
            content = _find_taptap_detail_content(root) or app
            description = _plain_text((content.get("description") or {}).get("text"))
            complete_description = (
                description
                if len(description) >= 120 and not description.endswith(("...", "…"))
                else ""
            )
            screenshots = app.get("screenshots") or []
            if not complete_description and not screenshots:
                raise ValueError("详情页未解析到完整介绍或图集")
            developer_note = _plain_text(
                ((content.get("developer_note") or app.get("developer_note") or {}).get("text"))
            )
            matching = list(conn.execute(
                "SELECT id,raw_json,full_description FROM source_items "
                "WHERE source='taptap' AND source_item_id=?",
                (row["source_item_id"],),
            ))
            for target in matching:
                try:
                    raw = json.loads(target["raw_json"] or "{}")
                except json.JSONDecodeError:
                    raw = {}
                if complete_description:
                    raw["detail_description"] = complete_description
                raw["detail_screenshots"] = screenshots
                if app.get("banner"):
                    raw["detail_banner"] = app["banner"]
                if developer_note:
                    raw["developer_note"] = developer_note
                old_description = (target["full_description"] or "").strip()
                selected_description = (
                    complete_description
                    if len(complete_description) > len(old_description)
                    else old_description
                )
                conn.execute(
                    "UPDATE source_items SET full_description=?, raw_json=? WHERE id=?",
                    (
                        selected_description or None,
                        json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                        target["id"],
                    ),
                )
            conn.commit()
            updated += len(matching)
        except (requests.RequestException, ValueError, json.JSONDecodeError, TypeError):
            failed += 1
    return {"checked": len(rows), "updated": updated, "failed": failed}


def _parse_haoyou_search_html(content: bytes, query_name: str) -> list[dict]:
    """解析好游快爆搜索页，只返回与查询名严格归一化一致的唯一产品。"""
    from .catalog import normalize_game_name

    expected = normalize_game_name(query_name)
    soup = BeautifulSoup(content, "lxml")
    found: dict[str, dict] = {}
    for node in soup.select(".sp-name"):
        anchor = node.find_parent("a", href=True)
        if anchor is None:
            continue
        name = node.get_text(" ", strip=True)
        if not name or normalize_game_name(name) != expected:
            continue
        detail_url = urljoin("https://www.3839.com/", anchor.get("href"))
        match = re.search(r"/a/(\d+)\.htm", detail_url)
        if match is None:
            continue
        mobile_url = f"https://m.3839.com/a/{match.group(1)}.htm"
        found[mobile_url] = {
            "name": name,
            "detail_url": mobile_url,
            "source_item_id": match.group(1),
        }
    return list(found.values())


def _fallback_reference_index(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """将已采集入库的 TapTap/UC 九游/好游快爆资料作为首级降级源。"""
    from .catalog import normalize_game_name

    index: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT source,source_item_id,name,developer,gameplay_intro,full_description,icon_url,detail_url
        FROM source_items
        WHERE source IN ('taptap','haoyou_kuaibao','uc_9game')
          AND (trim(COALESCE(developer,''))<>''
            OR length(trim(COALESCE(full_description,'')))>=40
            OR trim(COALESCE(icon_url,''))<>'')
        """
    ):
        key = normalize_game_name(row["name"])
        if not key:
            continue
        index.setdefault(key, []).append(dict(row))
    return index


def _best_fallback_fields(candidates: list[dict]) -> dict:
    """TapTap 优先提供开发者，介绍选择完整度最高的来源。"""
    priority = {"taptap": 3, "uc_9game": 2, "haoyou_kuaibao": 1, "haoyou_search": 1}
    developer_candidates = [item for item in candidates if (item.get("developer") or "").strip()]
    developer_item = max(
        developer_candidates,
        key=lambda item: (priority.get(item.get("source"), 0), len(item["developer"])),
        default=None,
    )
    description_candidates = []
    for item in candidates:
        description = (item.get("full_description") or "").strip()
        if len(description) >= 40:
            description_candidates.append((len(description), priority.get(item.get("source"), 0), item, description))
    description_item = max(
        description_candidates,
        key=lambda item: (item[0], item[1]),
        default=None,
    )
    icon_candidates = [item for item in candidates if (item.get("icon_url") or "").strip()]
    icon_item = max(
        icon_candidates,
        key=lambda item: priority.get(item.get("source"), 0),
        default=None,
    )
    return {
        "developer": developer_item.get("developer") if developer_item else None,
        "developer_source": developer_item,
        "description": description_item[3] if description_item else None,
        "description_source": description_item[2] if description_item else None,
        "icon_url": icon_item.get("icon_url") if icon_item else None,
        "icon_source": icon_item,
    }


def _apply_name_fallback(
    conn: sqlite3.Connection, normalized_name: str, fields: dict, provider: str,
) -> dict:
    """只填空值或明显过短介绍，不覆盖渠道已经提供的可靠资料。"""
    from .catalog import normalize_game_name

    rows = list(conn.execute(
        """
        SELECT id,name,developer,gameplay_intro,full_description,icon_url,raw_json
        FROM source_items
        WHERE trim(COALESCE(developer,''))=''
           OR length(trim(COALESCE(full_description,'')))<120
           OR trim(COALESCE(icon_url,''))=''
        """
    ))
    updated_rows = developer_updated = description_updated = icon_updated = 0
    now = datetime.now().astimezone().isoformat()
    for row in rows:
        if normalize_game_name(row["name"]) != normalized_name:
            continue
        old_developer = (row["developer"] or "").strip()
        old_description = (row["full_description"] or "").strip()
        old_icon = (row["icon_url"] or "").strip()
        developer = old_developer or (fields.get("developer") or "").strip()
        candidate_description = (fields.get("description") or "").strip()
        description = (
            candidate_description
            if len(candidate_description) > len(old_description)
            else old_description
        )
        icon_url = old_icon or (fields.get("icon_url") or "").strip()
        if developer == old_developer and description == old_description and icon_url == old_icon:
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        trace = {
            "provider": provider,
            "match_method": "normalized_name_exact",
            "matched_name": (
                (fields.get("developer_source") or fields.get("description_source")
                 or fields.get("icon_source") or {}).get("name")
            ),
            "updated_at": now,
        }
        for field_name, source_name in (
            ("developer", "developer_source"), ("full_description", "description_source"),
            ("icon_url", "icon_source"),
        ):
            source = fields.get(source_name) or {}
            if source:
                trace[field_name] = {
                    "source": source.get("source"),
                    "source_item_id": source.get("source_item_id"),
                    "detail_url": source.get("detail_url"),
                }
        raw["name_fallback_enrichment"] = trace
        intro = (row["gameplay_intro"] or "").strip()
        if not intro and description:
            intro = re.sub(r"\s+", " ", description)[:180]
        conn.execute(
            "UPDATE source_items SET developer=?,gameplay_intro=?,full_description=?,icon_url=?,raw_json=? WHERE id=?",
            (
                developer or None, intro or None, description or None, icon_url or None,
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), row["id"],
            ),
        )
        updated_rows += 1
        developer_updated += int(bool(developer) and not old_developer)
        description_updated += int(bool(description) and description != old_description)
        icon_updated += int(bool(icon_url) and not old_icon)
    conn.commit()
    return {
        "updated_rows": updated_rows,
        "developer_updated": developer_updated,
        "description_updated": description_updated,
        "icon_updated": icon_updated,
    }


def enrich_name_lookup_fallback(
    conn: sqlite3.Connection, delay: float = 0.35, online: bool = True,
    max_online_lookups: int = 120, negative_ttl_days: int = 7,
) -> dict:
    """按游戏名从 TapTap/UC 九游/好游快爆资料降级补全厂商和产品介绍。

    TapTap 在线搜索路径受站点 robots 规则限制，因此只复用已采集入库资料；
    在线缺口通过好游快爆搜索页和公开详情页低频补齐，并缓存查询结果。
    """
    from .catalog import normalize_game_name

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichment_lookup_cache (
          provider TEXT NOT NULL, normalized_name TEXT NOT NULL, query_name TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}', checked_at TEXT NOT NULL,
          PRIMARY KEY(provider, normalized_name)
        )
        """
    )
    conn.commit()
    totals = {
        "local_matches": 0, "online_lookups": 0, "cache_hits": 0,
        "updated_rows": 0, "developer_updated": 0, "description_updated": 0,
        "icon_updated": 0,
        "unavailable": 0, "ambiguous": 0, "failed": 0,
    }

    reference_index = _fallback_reference_index(conn)
    target_rows = list(conn.execute(
        """
        SELECT DISTINCT name FROM source_items
        WHERE trim(COALESCE(developer,''))=''
           OR length(trim(COALESCE(full_description,'')))<120
           OR trim(COALESCE(icon_url,''))=''
        ORDER BY name
        """
    ))
    target_names: dict[str, str] = {}
    for row in target_rows:
        key = normalize_game_name(row["name"])
        if key:
            target_names.setdefault(key, row["name"])

    for key in list(target_names):
        candidates = reference_index.get(key) or []
        if not candidates:
            continue
        fields = _best_fallback_fields(candidates)
        result = _apply_name_fallback(conn, key, fields, "collected_reference")
        if result["updated_rows"]:
            totals["local_matches"] += 1
            for field in ("updated_rows", "developer_updated", "description_updated", "icon_updated"):
                totals[field] += result[field]

    if not online:
        return totals

    # 本地资料复用后重新计算真正仍有缺口的产品，避免无效联网查询。
    remaining: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT DISTINCT name FROM source_items
        WHERE trim(COALESCE(developer,''))=''
           OR length(trim(COALESCE(full_description,'')))<120
           OR trim(COALESCE(icon_url,''))=''
        ORDER BY name
        """
    ):
        key = normalize_game_name(row["name"])
        if key:
            remaining.setdefault(key, row["name"])

    provider = "haoyou_search"
    session = requests.Session()
    session.headers.update(HEADERS)
    cutoff = datetime.now().astimezone() - timedelta(days=negative_ttl_days)
    for key, query_name in list(remaining.items())[:max_online_lookups]:
        cached = conn.execute(
            "SELECT status,result_json,checked_at FROM enrichment_lookup_cache "
            "WHERE provider=? AND normalized_name=?",
            (provider, key),
        ).fetchone()
        if cached:
            try:
                checked_at = datetime.fromisoformat(cached["checked_at"])
                cached_result = json.loads(cached["result_json"] or "{}")
            except (ValueError, TypeError, json.JSONDecodeError):
                checked_at, cached_result = cutoff - timedelta(days=1), {}
            if cached["status"] == "success":
                candidate = {**cached_result, "source": provider}
                fields = _best_fallback_fields([candidate])
                result = _apply_name_fallback(conn, key, fields, provider)
                totals["cache_hits"] += 1
                for field in ("updated_rows", "developer_updated", "description_updated", "icon_updated"):
                    totals[field] += result[field]
                continue
            if checked_at >= cutoff:
                totals["cache_hits"] += 1
                totals[cached["status"] if cached["status"] in totals else "unavailable"] += 1
                continue

        totals["online_lookups"] += 1
        status = "unavailable"
        cache_result: dict = {}
        try:
            search = session.get(
                "https://www.3839.com/search.php",
                params={"word": query_name}, timeout=20,
            )
            search.raise_for_status()
            matches = _parse_haoyou_search_html(search.content, query_name)
            if len(matches) > 1:
                status = "ambiguous"
            elif len(matches) == 1:
                if delay:
                    time.sleep(delay)
                detail = session.get(matches[0]["detail_url"], timeout=20)
                detail.raise_for_status()
                parsed = _parse_haoyou_detail_html(detail.content)
                if parsed.get("developer") or parsed.get("description") or parsed.get("icon_url"):
                    status = "success"
                    cache_result = {
                        **matches[0], "source": provider,
                        "developer": parsed.get("developer"),
                        "full_description": parsed.get("description"),
                        "icon_url": parsed.get("icon_url"),
                        "developer_role": parsed.get("developer_role"),
                    }
                    fields = _best_fallback_fields([cache_result])
                    result = _apply_name_fallback(conn, key, fields, provider)
                    for field in ("updated_rows", "developer_updated", "description_updated", "icon_updated"):
                        totals[field] += result[field]
            totals[status if status in totals else "unavailable"] += int(status != "success")
        except (requests.RequestException, ValueError, TypeError):
            status = "failed"
            totals["failed"] += 1
        checked_at = datetime.now().astimezone().isoformat()
        conn.execute(
            """
            INSERT INTO enrichment_lookup_cache(
              provider,normalized_name,query_name,status,result_json,checked_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(provider,normalized_name) DO UPDATE SET
              query_name=excluded.query_name,status=excluded.status,
              result_json=excluded.result_json,checked_at=excluded.checked_at
            """,
            (
                provider, key, query_name, status,
                json.dumps(cache_result, ensure_ascii=False, separators=(",", ":")), checked_at,
            ),
        )
        conn.commit()
        if delay:
            time.sleep(delay)
    return totals


def enrich_9game_screenshots(
    conn: sqlite3.Connection, raw_dir: Path | None = None, delay: float = 0.15,
) -> dict:
    """从九游专区详情回填商店截图，优先复用已留存 HTML，缺失时才低频联网。"""
    from .collectors import _parse_9game_detail_html
    from .gallery import extract_gallery_urls

    cached_pages: dict[str, Path] = {}
    if raw_dir and raw_dir.is_dir():
        for path in raw_dir.rglob("*.raw"):
            if path.parent.name != "9game":
                continue
            match = re.search(r"detail-([^.]+)\.raw$", path.name)
            if match:
                previous = cached_pages.get(match.group(1))
                if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
                    cached_pages[match.group(1)] = path

    rows = list(conn.execute(
        """
        SELECT source_item_id, detail_url, raw_json
        FROM source_items
        WHERE source='uc_9game' AND detail_url LIKE 'https://www.9game.cn/%'
        GROUP BY source_item_id, detail_url
        ORDER BY source_item_id
        """
    ))
    checked = updated = unavailable = failed = raw_hits = fetched = 0
    for row in rows:
        if extract_gallery_urls("uc_9game", row["raw_json"]):
            continue
        checked += 1
        try:
            stored = cached_pages.get(str(row["source_item_id"]))
            if stored and stored.is_file():
                content = stored.read_bytes()
                raw_hits += 1
            else:
                response = requests.get(row["detail_url"], headers=HEADERS, timeout=20)
                response.raise_for_status()
                content = response.content
                fetched += 1
                if delay:
                    time.sleep(delay)
            parsed = _parse_9game_detail_html(content)
            screenshots = parsed.get("screenshot_urls") or []
            if not screenshots:
                unavailable += 1
                continue
            targets = list(conn.execute(
                "SELECT id,raw_json FROM source_items WHERE source='uc_9game' AND source_item_id=?",
                (row["source_item_id"],),
            ))
            for target in targets:
                try:
                    raw = json.loads(target["raw_json"] or "{}")
                except json.JSONDecodeError:
                    raw = {}
                detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
                detail.update({
                    key: value for key, value in parsed.items()
                    if value not in (None, "", [])
                })
                raw["detail"] = detail
                conn.execute(
                    "UPDATE source_items SET raw_json=? WHERE id=?",
                    (json.dumps(raw, ensure_ascii=False, separators=(",", ":")), target["id"]),
                )
            conn.commit()
            updated += len(targets)
        except (OSError, requests.RequestException, ValueError, TypeError):
            failed += 1
    return {
        "checked": checked, "updated": updated, "raw_hits": raw_hits,
        "fetched": fetched, "unavailable": unavailable, "failed": failed,
    }


def _parse_haoyou_detail_html(content: bytes) -> dict:
    """解析好游快爆公开详情页中的介绍、厂商与 Icon。"""
    soup = BeautifulSoup(content, "lxml")
    icon_url = None
    icon = soup.select_one('img[alt$="下载"]')
    if icon is not None:
        value = icon.get("lz_src") or icon.get("data-src") or icon.get("src")
        if value and "placeholder" not in value:
            icon_url = urljoin("https://m.3839.com/", value)
    description = ""
    intro_heading = next(
        (node for node in soup.select(".titHd") if "游戏介绍" in node.get_text(" ", strip=True)),
        None,
    )
    if intro_heading is not None:
        section = intro_heading.find_parent("div", class_="wrap")
        intro_node = section.select_one(".game-desc") if section is not None else None
        if intro_node is not None:
            # “更多”是页面展开按钮，不属于正文。
            for more in intro_node.select(".more, .morebtn, [id^='btn_zhan']"):
                more.decompose()
            description = _plain_text(str(intro_node))
            description = re.sub(r"(?:\n)?更多$", "", description).strip()

    metadata = {}
    for cell in soup.select("ul.game-data td"):
        label_node = cell.select_one(".sp1")
        value_node = cell.select_one(".sp2")
        if label_node is None or value_node is None:
            continue
        label = label_node.get_text(" ", strip=True).rstrip("：:")
        value = value_node.get_text(" ", strip=True)
        if label and value:
            metadata[label] = value
    developer_role = next(
        (label for label in ("开发商", "开发者", "研发商", "发行商") if metadata.get(label)),
        None,
    )
    developer = metadata.get(developer_role) if developer_role else None
    screenshot_urls = []
    for node in soup.select('img[alt*="截图"]'):
        value = node.get("lz_src") or node.get("data-src") or node.get("src")
        if not value or "placeholder" in value:
            continue
        value = value.replace("~thumb?", "?")
        screenshot_urls.append(urljoin("https://m.3839.com/", value))
    return {
        "description": description if len(description) >= 40 else None,
        "developer": developer,
        "developer_role": developer_role,
        "icon_url": icon_url,
        "metadata": metadata,
        "screenshot_urls": list(dict.fromkeys(screenshot_urls))[:10],
    }


def enrich_haoyou_details(conn: sqlite3.Connection, delay: float = 0.25) -> dict:
    """低频访问 robots 允许的好游快爆详情页，补全介绍与厂商。"""
    rows = list(conn.execute(
        """
        SELECT MIN(id) AS id, detail_url
        FROM source_items
        WHERE source='haoyou_kuaibao'
          AND detail_url LIKE 'https://m.3839.com/a/%.htm'
          AND (
            developer IS NULL OR developer='' OR
            icon_url IS NULL OR icon_url='' OR
            length(trim(COALESCE(full_description, ''))) < 120 OR
            raw_json NOT LIKE '%"detail_screenshot_urls"%'
          )
        GROUP BY detail_url
        ORDER BY MIN(id)
        """
    ))
    updated = failed = 0
    for index, row in enumerate(rows):
        if index and delay:
            time.sleep(delay)
        try:
            response = requests.get(row["detail_url"], headers=HEADERS, timeout=20)
            response.raise_for_status()
            parsed = _parse_haoyou_detail_html(response.content)
            if not parsed["description"] and not parsed["developer"] and not parsed["icon_url"]:
                raise ValueError("详情页未解析到介绍、厂商或 Icon")
            matching = list(conn.execute(
                "SELECT id,developer,full_description,icon_url,raw_json FROM source_items "
                "WHERE source='haoyou_kuaibao' AND detail_url=?",
                (row["detail_url"],),
            ))
            for target in matching:
                try:
                    raw = json.loads(target["raw_json"] or "{}")
                except json.JSONDecodeError:
                    raw = {}
                raw["detail_metadata"] = parsed["metadata"]
                if parsed["developer_role"]:
                    raw["developer_role"] = parsed["developer_role"]
                if parsed["description"]:
                    raw["detail_description"] = parsed["description"]
                if parsed["icon_url"]:
                    raw["detail_icon_url"] = parsed["icon_url"]
                raw["detail_screenshot_urls"] = parsed["screenshot_urls"]
                developer = target["developer"] or parsed["developer"]
                old_description = (target["full_description"] or "").strip()
                description = (
                    parsed["description"]
                    if len(parsed["description"] or "") > len(old_description)
                    else old_description
                )
                conn.execute(
                    "UPDATE source_items SET developer=?, full_description=?, icon_url=?, raw_json=? WHERE id=?",
                    (
                        developer or None,
                        description or None,
                        target["icon_url"] or parsed["icon_url"],
                        json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                        target["id"],
                    ),
                )
            conn.commit()
            updated += len(matching)
        except (requests.RequestException, ValueError, TypeError):
            failed += 1
    return {"checked": len(rows), "updated": updated, "failed": failed}


def _parse_huawei_public_detail(payload: dict, app_id: str) -> dict | None:
    """仅接受包含指定 App ID 详情卡的华为官方网页响应。"""
    layouts = payload.get("layoutData") or []
    by_name = {item.get("layoutName"): item.get("dataList") or [] for item in layouts}
    hidden = next(
        (
            item for item in by_name.get("detailhiddencard", [])
            if str(item.get("appid") or "") == app_id
        ),
        None,
    )
    if hidden is None:
        return None
    info = next(iter(by_name.get("detailappinfocard", [])), {})
    pc_info = next(iter(by_name.get("pcappinfocard", [])), {})
    intro = next(iter(by_name.get("detailappintrocard", [])), {})
    head = next(iter(by_name.get("detailheadcard", [])), {})
    screenshots = []
    for layout_name, values in by_name.items():
        if "screenshot" not in (layout_name or "").lower():
            continue
        for value in values:
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                screenshots.append(value)
            elif isinstance(value, dict):
                for key in ("url", "imgUrl", "imageUrl", "screenshot"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                        screenshots.append(candidate)
    return {
        "name": hidden.get("name") or head.get("name"),
        "package_name": hidden.get("package") or info.get("package"),
        "developer": info.get("developer") or pc_info.get("developer"),
        "description": _plain_text(intro.get("appIntro")),
        "icon_url": hidden.get("icon") or head.get("icoUri"),
        "version_name": hidden.get("versionName") or info.get("version"),
        "release_date": info.get("releaseDate") or pc_info.get("releaseDate"),
        "screenshots": list(dict.fromkeys(screenshots))[:10],
        "detail_url": f"https://appgallery.huawei.com/app/{app_id}",
    }


def _huawei_public_detail(
    session: requests.Session, interface_code: str, app_id: str,
) -> tuple[dict | None, str]:
    headers = {
        **HUAWEI_WEB_HEADERS,
        "Interface-Code": f"{interface_code}_{int(time.time() * 1000)}",
    }
    response = session.get(
        f"{HUAWEI_WEB_BASE}/uowap/index",
        params={
            "method": "internal.getTabDetail",
            "serviceType": 20,
            "reqPageNum": 1,
            "uri": f"app|{app_id}",
            "maxResults": 25,
            "locale": "zh_CN",
            "zone": "CN",
        },
        headers=headers,
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("rtnCode") == 1002:
        code_response = session.post(
            f"{HUAWEI_WEB_BASE}/webedge/getInterfaceCode",
            headers=HUAWEI_WEB_HEADERS,
            timeout=20,
        )
        code_response.raise_for_status()
        interface_code = code_response.json()
        return _huawei_public_detail(session, interface_code, app_id)[0], interface_code
    return _parse_huawei_public_detail(payload, app_id), interface_code


def _preferred_huawei_official_name(current_name: str | None, official_name: str | None) -> str:
    """官方名带营销括注时保留较干净的现名；活动标题则改用官方产品名。"""
    from .catalog import clean_game_name

    current = str(current_name or "").strip()
    official = str(official_name or "").strip()
    if not official:
        return current
    if "HUAWEI" in official.upper():
        official = clean_game_name(official)
    if current and official.startswith(current) and len(official) > len(current):
        suffix = official[len(current):].lstrip()
        if suffix.startswith(("（", "(", "【", "[", "-", "—", "_")):
            return current
    return official


def _repair_huawei_names_from_official_details(conn: sqlite3.Connection) -> int:
    """复用已验证的同 App ID 官方详情名，修正被活动标题污染的产品名。"""
    repaired = 0
    rows = conn.execute(
        "SELECT id,name,raw_json FROM source_items WHERE source='huawei_gamecenter'"
    )
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        public_detail = raw.get("official_public_detail") or {}
        official_name = _preferred_huawei_official_name(row["name"], public_detail.get("name"))
        if not official_name or official_name == (row["name"] or "").strip():
            continue
        conn.execute("UPDATE source_items SET name=? WHERE id=?", (official_name, row["id"]))
        repaired += 1
    if repaired:
        conn.commit()
    return repaired


def _repair_huawei_event_cards(conn: sqlite3.Connection) -> int:
    """将已入库的华为 GameEvent 活动卡修正为测试事件和活动开始时间。"""
    from .app_cache_collectors import _huawei_event_fields

    repaired = 0
    now_ms = int(datetime.now().timestamp() * 1000)
    rows = list(conn.execute(
        "SELECT id,source_item_id,event_type,event_time,status,raw_json "
        "FROM source_items WHERE source='huawei_gamecenter'"
    ))
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if raw.get("gcode") != "GameEvent":
            continue
        event_type, event_time, status = _huawei_event_fields(raw, now_ms)
        if (event_type, event_time, status) == (row["event_type"], row["event_time"], row["status"]):
            continue
        duplicate = conn.execute(
            "SELECT id FROM source_items WHERE source='huawei_gamecenter' AND source_item_id=? "
            "AND event_type=? AND event_time=? AND id<>?",
            (row["source_item_id"], event_type, event_time, row["id"]),
        ).fetchone()
        if duplicate:
            conn.execute("DELETE FROM source_items WHERE id=?", (row["id"],))
        else:
            conn.execute(
                "UPDATE source_items SET event_type=?,event_time=?,status=? WHERE id=?",
                (event_type, event_time, status, row["id"]),
            )
        repaired += 1
    if repaired:
        conn.commit()
    return repaired


def enrich_huawei_public_details(conn: sqlite3.Connection, delay: float = 0.2) -> dict:
    """通过华为 AppGallery 官方匿名网页接口补全已上架产品详情。"""
    corrected_names = _repair_huawei_names_from_official_details(conn)
    corrected_events = _repair_huawei_event_cards(conn)
    rows = list(conn.execute(
        """
        SELECT MIN(id) AS id, source_item_id
        FROM source_items
        WHERE source='huawei_gamecenter'
          AND source_item_id LIKE 'C%'
          AND (
            developer IS NULL OR developer='' OR
            length(trim(COALESCE(full_description, ''))) < 120
          )
        GROUP BY source_item_id
        ORDER BY MIN(id)
        """
    ))
    session = requests.Session()
    try:
        code_response = session.post(
            f"{HUAWEI_WEB_BASE}/webedge/getInterfaceCode",
            headers=HUAWEI_WEB_HEADERS,
            timeout=20,
        )
        code_response.raise_for_status()
        interface_code = code_response.json()
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return {
            "checked": len(rows), "updated": 0, "corrected_names": corrected_names,
            "corrected_events": corrected_events,
            "unavailable": 0, "failed": len(rows),
        }

    updated = unavailable = failed = 0
    for index, row in enumerate(rows):
        if index and delay:
            time.sleep(delay)
        try:
            parsed, interface_code = _huawei_public_detail(
                session, interface_code, row["source_item_id"]
            )
            if parsed is None:
                unavailable += 1
                continue
            matching = list(conn.execute(
                "SELECT id,name,developer,full_description,raw_json FROM source_items "
                "WHERE source='huawei_gamecenter' AND source_item_id=?",
                (row["source_item_id"],),
            ))
            for target in matching:
                official_name = _preferred_huawei_official_name(target["name"], parsed["name"])
                try:
                    raw = json.loads(target["raw_json"] or "{}")
                except json.JSONDecodeError:
                    raw = {}
                raw["official_public_detail"] = {
                    key: value for key, value in parsed.items() if value not in (None, "")
                }
                old_description = (target["full_description"] or "").strip()
                description = (
                    parsed["description"]
                    if len(parsed["description"] or "") > len(old_description)
                    else old_description
                )
                conn.execute(
                    """
                    UPDATE source_items SET
                      name=COALESCE(NULLIF(?, ''), name), developer=?, full_description=?,
                      icon_url=COALESCE(NULLIF(icon_url, ''), ?),
                      package_name=COALESCE(NULLIF(package_name, ''), ?),
                      version_name=COALESCE(NULLIF(version_name, ''), ?),
                      detail_url=?, raw_json=?
                    WHERE id=?
                    """,
                    (
                        official_name or None,
                        target["developer"] or parsed["developer"] or None,
                        description or None,
                        parsed["icon_url"], parsed["package_name"], parsed["version_name"],
                        parsed["detail_url"],
                        json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                        target["id"],
                    ),
                )
            conn.commit()
            updated += len(matching)
        except (requests.RequestException, ValueError, json.JSONDecodeError, TypeError):
            failed += 1
    return {
        "checked": len(rows), "updated": updated,
        "corrected_names": corrected_names,
        "corrected_events": corrected_events,
        "unavailable": unavailable, "failed": failed,
    }


def _parse_xiaomi_public_detail(content: bytes, game_id: str) -> dict | None:
    """解析小米游戏中心官方详情页的服务端渲染状态。"""
    soup = BeautifulSoup(content, "lxml")
    state_script = next(
        (
            node.string for node in soup.select("script")
            if node.string and "window.__INITIAL_STATE__" in node.string
        ),
        None,
    )
    if not state_script or "=" not in state_script:
        return None
    state = json.loads(state_script.split("=", 1)[1].strip().rstrip(";"))
    game = (state.get("game") or {}).get("gameInfo") or {}
    detail = game.get("detail") or {}
    info = game.get("gameInfo") or {}
    if str(info.get("gameId") or "") != game_id:
        return None
    domain = str(state.get("domain") or "https://t1.g.mi.com").rstrip("/")
    screenshots = []
    for item in info.get("screenShot") or []:
        value = item.get("url") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(("http://", "https://")):
            screenshots.append(value)
        else:
            screenshots.append(f"{domain}/thumbnail/jpeg/w1200q90/{value.lstrip('/')}")
    return {
        "name": info.get("displayName"),
        "package_name": info.get("packageName"),
        "developer": detail.get("developerCompanyName") or info.get("publisherName"),
        "publisher": info.get("publisherName"),
        "description": _plain_text(info.get("introduction")),
        "version_name": info.get("versionName"),
        "screenshots": list(dict.fromkeys(screenshots))[:10],
        "detail_url": f"https://game.xiaomi.com/game/{game_id}",
    }


def enrich_xiaomi_public_details(conn: sqlite3.Connection, delay: float = 0.2) -> dict:
    """通过 robots 允许的小米官方详情页补全开发商与完整介绍。"""
    rows = list(conn.execute(
        """
        SELECT MIN(id) AS id, source_item_id
        FROM source_items
        WHERE source='xiaomi_gamecenter'
          AND source_item_id GLOB '[0-9]*'
          AND (
            developer IS NULL OR developer='' OR
            length(trim(COALESCE(full_description, ''))) < 120 OR
            raw_json NOT LIKE '%"screenshots"%'
          )
        GROUP BY source_item_id
        ORDER BY MIN(id)
        """
    ))
    updated = unavailable = failed = 0
    for index, row in enumerate(rows):
        if index and delay:
            time.sleep(delay)
        url = f"https://game.xiaomi.com/game/{row['source_item_id']}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            if response.status_code == 404:
                unavailable += 1
                continue
            response.raise_for_status()
            parsed = _parse_xiaomi_public_detail(response.content, row["source_item_id"])
            if parsed is None:
                unavailable += 1
                continue
            matching = list(conn.execute(
                "SELECT id,developer,full_description,raw_json FROM source_items "
                "WHERE source='xiaomi_gamecenter' AND source_item_id=?",
                (row["source_item_id"],),
            ))
            for target in matching:
                try:
                    raw = json.loads(target["raw_json"] or "{}")
                except json.JSONDecodeError:
                    raw = {}
                raw["official_public_detail"] = {
                    key: value for key, value in parsed.items() if value not in (None, "")
                }
                old_description = (target["full_description"] or "").strip()
                description = (
                    parsed["description"]
                    if len(parsed["description"] or "") > len(old_description)
                    else old_description
                )
                conn.execute(
                    """
                    UPDATE source_items SET
                      developer=?, full_description=?,
                      package_name=COALESCE(NULLIF(package_name, ''), ?),
                      version_name=COALESCE(NULLIF(version_name, ''), ?),
                      detail_url=?, raw_json=?
                    WHERE id=?
                    """,
                    (
                        target["developer"] or parsed["developer"] or None,
                        description or None,
                        parsed["package_name"], parsed["version_name"], parsed["detail_url"],
                        json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                        target["id"],
                    ),
                )
            conn.commit()
            updated += len(matching)
        except (requests.RequestException, ValueError, json.JSONDecodeError, TypeError):
            failed += 1
    return {
        "checked": len(rows), "updated": updated,
        "unavailable": unavailable, "failed": failed,
    }


def _honor_cache_metadata(payload: dict) -> dict[str, list[dict]]:
    """从荣耀公开业务缓存提取游戏元数据，保留重名候选供调用方判定。"""
    from .catalog import normalize_game_name

    found: dict[str, list[dict]] = {}
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            name = value.get("name")
            banner_info = value.get("bannerInfo") or {}
            immersive = ((value.get("immersiveDetail") or {}).get("hImgConfig") or {})
            screenshot_urls = list(dict.fromkeys(filter(None, (
                banner_info.get("banner"), value.get("headImageUrl"), immersive.get("hImgUrl"),
            ))))
            if name and (value.get("company") or value.get("description") or screenshot_urls):
                key = normalize_game_name(name)
                candidate = {
                    "name": name,
                    "developer": value.get("company"),
                    "description": _plain_text(value.get("description")),
                    "brief": _plain_text(value.get("brief")),
                    "package_name": value.get("pName") or value.get("packageName"),
                    "version_name": value.get("verName"),
                    "screenshot_urls": screenshot_urls,
                }
                if candidate not in found.setdefault(key, []):
                    found[key].append(candidate)
            stack.extend(child for child in value.values() if isinstance(child, (dict, list)))
        elif isinstance(value, list):
            stack.extend(value)
    return found


def enrich_honor_cache_metadata(conn: sqlite3.Connection) -> dict:
    """只用荣耀缓存补已确认的新游记录，不把首页推荐误当成新游。"""
    import subprocess

    from .app_cache_collectors import _read_root_file
    from .catalog import normalize_game_name

    path = "/data/data/com.hihonor.gamecenter/cache/main_content_data/main_content_data_CN_zh"
    try:
        payload = json.loads(_read_root_file(path))
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {"available": False, "checked": 0, "updated": 0, "ambiguous": 0}
    metadata = _honor_cache_metadata(payload)
    rows = list(conn.execute(
        "SELECT id,name,developer,gameplay_intro,full_description,raw_json "
        "FROM source_items WHERE source='honor_gamecenter'"
    ))
    updated = ambiguous = 0
    for row in rows:
        candidates = metadata.get(normalize_game_name(row["name"]), [])
        if len(candidates) != 1:
            ambiguous += 1 if len(candidates) > 1 else 0
            continue
        parsed = candidates[0]
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        raw["honor_cache_detail"] = {
            key: value for key, value in parsed.items() if value not in (None, "")
        }
        old_description = (row["full_description"] or "").strip()
        description = (
            parsed["description"]
            if len(parsed["description"] or "") > len(old_description)
            else old_description
        )
        old_intro = (row["gameplay_intro"] or "").strip()
        candidate_intro = parsed["brief"] or parsed["description"]
        intro = candidate_intro if len(candidate_intro or "") > len(old_intro) else old_intro
        conn.execute(
            """
            UPDATE source_items SET developer=?, gameplay_intro=?, full_description=?,
              package_name=COALESCE(NULLIF(package_name, ''), ?),
              version_name=COALESCE(NULLIF(version_name, ''), ?), raw_json=?
            WHERE id=?
            """,
            (
                row["developer"] or parsed["developer"] or None,
                intro or None, description or None,
                parsed["package_name"], parsed["version_name"],
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), row["id"],
            ),
        )
        updated += 1
    conn.commit()
    return {
        "available": True, "checked": len(rows), "updated": updated,
        "ambiguous": ambiguous, "cache_games": len(metadata),
    }


def enrich_oppo_ui_snapshots(conn: sqlite3.Connection) -> dict:
    """恢复 OPPO 详情页已展开的 UI 快照，避免分区跳转失败丢数据。"""
    import hashlib
    import subprocess
    import xml.etree.ElementTree as ET

    from .app_cache_collectors import _adb

    rows = list(conn.execute(
        "SELECT id,name,gameplay_intro,full_description,raw_json "
        "FROM source_items WHERE source='oppo_gamecenter'"
    ))
    names_by_hash: dict[str, set[str]] = {}
    for row in rows:
        key = hashlib.sha1(row["name"].encode()).hexdigest()[:10]
        names_by_hash.setdefault(key, set()).add(row["name"])
    try:
        output = _adb(
            "shell", "ls /sdcard/oppo-detail-*-expanded.xml 2>/dev/null"
        ).decode("utf-8", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "snapshots": 0, "matched": 0, "updated": 0}
    paths = [path.strip() for path in output.splitlines() if path.strip().endswith(".xml")]
    details: dict[str, str] = {}
    for path in paths:
        match = re.search(r"oppo-detail-([0-9a-f]{10})-expanded\.xml$", path)
        if not match or len(names_by_hash.get(match.group(1), set())) != 1:
            continue
        try:
            root = ET.fromstring(_adb("exec-out", "cat", path))
        except (OSError, subprocess.SubprocessError, ET.ParseError):
            continue
        node = next(
            (
                item for item in root.iter("node")
                if item.get("resource-id", "").endswith("/introduction_tv") and item.get("text")
            ),
            None,
        )
        if node is None:
            continue
        from .app_cache_collectors import _clean_ui_text
        description = _clean_ui_text(node.get("text"))
        if description:
            details[next(iter(names_by_hash[match.group(1)]))] = description
    updated = 0
    for row in rows:
        description = details.get(row["name"])
        if not description:
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        raw["ui_detail"] = {
            "name": row["name"], "description": description,
            "source": "oppo_gamecenter_app_snapshot",
            "captured_at": datetime.now().astimezone().isoformat(),
        }
        old_description = (row["full_description"] or "").strip()
        full_description = description if len(description) > len(old_description) else old_description
        old_intro = (row["gameplay_intro"] or "").strip()
        gameplay_intro = old_intro or description[:180]
        conn.execute(
            "UPDATE source_items SET gameplay_intro=?,full_description=?,raw_json=? WHERE id=?",
            (
                gameplay_intro or None, full_description or None,
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), row["id"],
            ),
        )
        updated += 1
    conn.commit()
    return {
        "available": True, "snapshots": len(paths),
        "matched": len(details), "updated": updated,
    }


def enrich_oppo_offline_media(conn: sqlite3.Connection) -> dict:
    """从 OPPO 已登录 App 的公开离线响应回填可归属到具体游戏的商店截图。"""
    from .app_cache_collectors import _attach_oppo_offline_metadata, _oppo_offline_blobs

    rows = list(conn.execute(
        "SELECT id,name,package_name,icon_url,raw_json FROM source_items "
        "WHERE source='oppo_gamecenter'"
    ))
    blobs = _oppo_offline_blobs()
    if not blobs:
        return {"available": False, "checked": len(rows), "matched": 0, "updated": 0}
    items = [{"name": row["name"], "raw": {}} for row in rows]
    _attach_oppo_offline_metadata(items, blobs)
    updated = matched = 0
    for row, item in zip(rows, items):
        detail = (item.get("raw") or {}).get("oppo_offline_detail") or {}
        if not detail:
            continue
        matched += 1
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        existing = raw.get("oppo_offline_detail") or {}
        merged = {**existing, **{key: value for key, value in detail.items() if value}}
        raw["oppo_offline_detail"] = merged
        conn.execute(
            "UPDATE source_items SET package_name=COALESCE(NULLIF(package_name,''),?), "
            "icon_url=COALESCE(NULLIF(icon_url,''),?), raw_json=? WHERE id=?",
            (
                detail.get("package_name"), detail.get("icon_url"),
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), row["id"],
            ),
        )
        updated += 1
    conn.commit()
    return {
        "available": True, "checked": len(rows), "matched": matched,
        "updated": updated, "cache_blobs": len(blobs),
    }


def enrich_missing_4399(conn: sqlite3.Connection) -> dict:
    rows = list(conn.execute(
        """
        SELECT id, name, detail_url, raw_json FROM source_items
        WHERE source='4399_gamebox' AND detail_url IS NOT NULL
          AND (icon_url IS NULL OR icon_url='' OR developer IS NULL OR category IS NULL
            OR gameplay_intro IS NULL OR full_description IS NULL
            OR raw_json NOT LIKE '%"screenshot_urls"%')
        """
    ))
    updated = 0
    for row in rows:
        try:
            response = requests.get(row["detail_url"], headers=HEADERS, timeout=20)
            if response.status_code != 200:
                continue
        except requests.RequestException:
            continue
        soup = BeautifulSoup(response.content, "lxml")
        structured = _parse_4399_info_data(response.content)
        icon = next(
            (
                node for node in soup.select("img.m_icon[src]")
                if not node.get("alt") or node.get("alt").strip() == row["name"]
            ),
            soup.select_one("img.m_icon[src]"),
        )
        developer_node = soup.select_one("li.m_game_dev")
        intro_node = soup.select_one("#j-game-summary")
        icon_url = urljoin(row["detail_url"], icon.get("src")) if icon else None
        developer = re.sub(
            r"^厂商[：:]\s*", "", developer_node.get_text(" ", strip=True)
        ) if developer_node else None
        intro = intro_node.get_text(" ", strip=True) if intro_node else None
        icon_url = structured.get("icon_url") or icon_url
        developer = structured.get("developer") or developer
        intro = structured.get("brief") or intro
        description = structured.get("description") or intro
        if not any((icon_url, developer, intro, description, structured.get("category"))):
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        raw["detail_title"] = soup.title.get_text(strip=True) if soup.title else None
        if structured:
            raw["info_data_detail"] = {
                key: value for key, value in structured.items() if value not in (None, "", [])
            }
        conn.execute(
            """
            UPDATE source_items SET
                icon_url=COALESCE(?, icon_url), developer=COALESCE(?, developer),
                category=COALESCE(?, category), gameplay_intro=COALESCE(?, gameplay_intro),
                full_description=CASE WHEN length(COALESCE(?, '')) > length(COALESCE(full_description, ''))
                  THEN ? ELSE full_description END,
                version_name=COALESCE(?, version_name), raw_json=?
            WHERE id=?
            """,
            (
                icon_url, developer or None, structured.get("category"), intro or None,
                description or None, description or None, structured.get("version_name"),
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")), row["id"],
            ),
        )
        updated += 1
    conn.commit()
    return {"checked": len(rows), "updated": updated}
