"""跨渠道产品归一化和查询模型。"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict


SOURCE_LABELS = {
    "taptap": "TapTap",
    "huawei_gamecenter": "华为游戏中心",
    "honor_gamecenter": "荣耀游戏中心",
    "xiaomi_gamecenter": "小米游戏中心",
    "oppo_gamecenter": "OPPO 游戏中心",
    "vivo_gamecenter": "vivo 游戏中心",
    "4399_gamebox": "4399 游戏盒",
    "233_leyuan": "233 乐园",
    "haoyou_kuaibao": "好游快爆",
    "uc_9game": "UC 九游",
    "apple_appstore_cn": "App Store 中国区",
}

SOURCE_NOTES = {
    "apple_appstore_cn": "数据不全",
}

SOURCE_ORDER = {source: index for index, source in enumerate(SOURCE_LABELS)}

EVENT_LABELS = {
    "launch": "上线/首发",
    "announcement": "定档/资讯",
    "beta": "测试",
    "limited_beta": "限量测试",
    "important_beta": "重点测试",
    "recruiting_beta": "招募测试",
    "reservation": "预约",
    "pre_download": "预下载",
    "timeline": "新游动态",
    "new_listing": "新收录",
    "first_seen": "首次发现",
}

SOURCE_QUALITY = {
    "apple_appstore_cn": 96,
    "taptap": 90,
    "vivo_gamecenter": 85,
    "233_leyuan": 82,
    "huawei_gamecenter": 78,
    "xiaomi_gamecenter": 76,
    "haoyou_kuaibao": 72,
    "uc_9game": 74,
    "honor_gamecenter": 68,
    "oppo_gamecenter": 62,
    "4399_gamebox": 55,
}

_CHANNEL_SUFFIXES = re.compile(
    r"(?:[（(](?:官服|官方|测试版|测试服|先遣服|体验服|预约版)[）)]|"
    r"[-—_](?:官服|官方版|测试版|测试服|先遣服|体验服|预约|预下载))$",
    re.IGNORECASE,
)
_EVENT_SUFFIX = re.compile(
    r"[-—_]?(?:\d{1,2}月\d{1,2}日|\d{1,2}[.·]\d{1,2})"
    r"(?:上线|首发|开测|测试开启|预下载开启|发售)$"
)
_MARKETING_PAREN = re.compile(
    r"[（(][^）)]*(?:开启|安装|登录|赠送|送|赢|超变|联动|预约|首发|官服|版本|狂欢)[^）)]*[）)]$"
)
_MARKETING_DASH = re.compile(
    r"[-—_][^-—_]*(?:登录送|全员|赠送|联动|预约|首发|上线|版本)[^-—_]*$"
)

# 仅收录已由多个来源逐项核验过的渠道营销名，避免用模糊相似度误合并同名游戏。
_VERIFIED_NAME_ALIASES = {
    "七界梦谭-代号界": "七界梦谭",
    "代号:新生-动物搜打撤新游": "代号:新生",
    "佣兵大冒险(肉鸽废土横扫尸潮)": "佣兵大冒险",
    "仙域无双-低折修仙骨折价": "仙域无双",
    "家园:梦想派对-城邦轻策略手游": "家园:梦想派对",
    "蜀山幻想志-推关解千抽": "蜀山幻想志",
    "客官里面请(删档测试)": "客官里面请",
}


def clean_game_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", name or "").strip()
    value = _VERIFIED_NAME_ALIASES.get(value, value)
    previous = None
    while value != previous:
        previous = value
        value = _CHANNEL_SUFFIXES.sub("", value).strip()
        value = _EVENT_SUFFIX.sub("", value).strip()
        value = _MARKETING_PAREN.sub("", value).strip()
        value = _MARKETING_DASH.sub("", value).strip()
        value = _VERIFIED_NAME_ALIASES.get(value, value)
    return value or (name or "未知名称").strip()


def normalize_game_name(name: str) -> str:
    value = clean_game_name(name).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def canonical_key_for(name: str, package_name: str | None = None) -> str:
    normalized = normalize_game_name(name)
    if normalized:
        return f"name:{normalized}"
    return f"package:{(package_name or name).casefold()}"


def _best(rows: list[sqlite3.Row], field: str):
    candidates = [row for row in rows if row[field] not in (None, "", "[]")]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            SOURCE_QUALITY.get(row["source"], 0),
            len(str(row[field])),
            row["last_seen_at"],
        ),
    )[field]


def rebuild_catalog(conn: sqlite3.Connection) -> int:
    """从来源事件表重建可重复生成的跨渠道产品目录。"""
    invalid_ids = []
    for row in conn.execute(
        "SELECT id, source, package_name, raw_json FROM source_items WHERE source='huawei_gamecenter' AND package_name IS NULL"
    ):
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        if raw.get("landingPageType") is not None:
            invalid_ids.append(row["id"])
    if invalid_ids:
        with conn:
            conn.executemany("DELETE FROM canonical_members WHERE source_row_id=?", [(item_id,) for item_id in invalid_ids])
            conn.executemany("DELETE FROM source_items WHERE id=?", [(item_id,) for item_id in invalid_ids])
    rows = list(conn.execute("SELECT * FROM source_items ORDER BY id"))
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = canonical_key_for(row["name"], row["package_name"])
        groups[key].append(row)

    with conn:
        conn.execute("DELETE FROM canonical_members")
        conn.execute("DELETE FROM canonical_games")
        for key, members in groups.items():
            display_name = min(
                (clean_game_name(row["name"]) for row in members),
                key=lambda value: (len(value), value),
            )
            tags = []
            for row in members:
                try:
                    tags.extend(json.loads(row["tags_json"] or "[]"))
                except json.JSONDecodeError:
                    pass
            tags = list(dict.fromkeys(str(tag) for tag in tags if tag))
            developer = _best(members, "developer")
            category = _best(members, "category")
            intro = _best(members, "gameplay_intro")
            icon = _best(members, "icon_url")
            ratings = [float(row["rating"]) for row in members if row["rating"] is not None]
            sources = {row["source"] for row in members}
            cursor = conn.execute(
                """
                INSERT INTO canonical_games (
                    canonical_key, name, normalized_name, developer, category,
                    tags_json, gameplay_intro, icon_url, rating, first_seen_at,
                    last_seen_at, source_count, event_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, display_name, normalize_game_name(display_name), developer, category,
                    json.dumps(tags, ensure_ascii=False), intro, icon,
                    round(sum(ratings) / len(ratings), 1) if ratings else None,
                    min(row["first_seen_at"] for row in members),
                    max(row["last_seen_at"] for row in members),
                    len(sources), len(members),
                ),
            )
            game_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO canonical_members(game_id, source_row_id) VALUES (?, ?)",
                [(game_id, row["id"]) for row in members],
            )
            conn.executemany(
                "UPDATE source_items SET canonical_key=? WHERE id=?",
                [(key, row["id"]) for row in members],
            )
    return len(groups)


def audit_catalog_completeness(conn: sqlite3.Connection) -> dict:
    """以产品为粒度统计详细资料覆盖，供每日任务验收。"""
    rows = list(conn.execute(
        """
        SELECT cg.id,cg.developer,cg.category,cg.icon_url,
          MAX(length(trim(COALESCE(si.full_description, '')))) AS description_length,
          MAX(length(trim(COALESCE(si.gameplay_intro, '')))) AS intro_length,
          MAX(CASE WHEN COALESCE(si.package_name, '')<>'' THEN 1 ELSE 0 END) AS has_package
        FROM canonical_games cg
        JOIN canonical_members cm ON cm.game_id=cg.id
        JOIN source_items si ON si.id=cm.source_row_id
        GROUP BY cg.id
        """
    ))
    total = len(rows)
    known = {
        "developer": sum(bool((row["developer"] or "").strip()) for row in rows),
        "category": sum(bool((row["category"] or "").strip()) for row in rows),
        "icon": sum(bool((row["icon_url"] or "").strip()) for row in rows),
        "long_description": sum(row["description_length"] >= 120 for row in rows),
        "effective_intro": sum(
            max(row["description_length"], row["intro_length"]) >= 30 for row in rows
        ),
        "package": sum(bool(row["has_package"]) for row in rows),
    }
    return {
        "total": total,
        "coverage": {
            field: {
                "known": count,
                "missing": total - count,
                "rate": round(count * 100 / total, 1) if total else 0.0,
            }
            for field, count in known.items()
        },
    }
