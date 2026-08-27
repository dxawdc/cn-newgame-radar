"""跨渠道产品归一化和查询模型。"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime


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
    "first_seen": "首次采集发现",
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
    r"(?:[（(](?:官服|官方(?:版)?|TapTap测试版|测试版|测试服|先遣服|体验服|预约版|"
    r"(?:PC|安卓|iOS)(?:端|版)?)[）)]|"
    r"[-—_](?:官服|官方版|TapTap测试版|测试版|测试服|先遣服|体验服|预约版?|"
    r"预下载|(?:PC|安卓|iOS)(?:端|版)?))$",
    re.IGNORECASE,
)
_ACTIVITY_CALENDAR_DATE = (
    r"(?:(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"(?:20\d{2}年)?\d{1,2}月\d{1,2}[日号]?|\d{1,2}[.·/-]\d{1,2})"
)
_ACTIVITY_DATE = (
    rf"(?:{_ACTIVITY_CALENDAR_DATE}(?:\s*\d{{1,2}}:\d{{2}})?|"
    r"(?:今日|明日)\s*\d{1,2}:\d{2})"
)
_ACTIVITY_MODIFIER = (
    r"(?:限时|限量|限号|删档|不删档|计费|不计费|付费|正式|首次|首轮|二次|三次|末次|最终|"
    r"第[一二三四五六七八九十\d]+次|今日|明日|本轮|安卓|iOS|双端|全平台|"
    r"小规模|大规模|技术|封闭|公开|先锋|共研|抢先|先行|资格)"
)
_ACTIVITY_EVENT = (
    r"(?:测试招募|抢先体验|预下载|开测|测试|内测|公测|封测|删测|首测|二测|"
    r"终测|预约|招募|预购|首发|上线|发售|开服)"
)
_ACTIVITY_PREFIX_STATE = r"(?:(?:现已|已|即将)?(?:开启|开放|启动)\s*)?"
_ACTIVITY_STATE = (
    r"(?:(?:现已|已|即将)?(?:开启|开始|开放|启动)|进行中|招募中|"
    r"定档|倒计时|中)?"
)
_ACTIVITY_SUFFIX_BODY = (
    rf"(?:{_ACTIVITY_DATE}\s*)?{_ACTIVITY_PREFIX_STATE}(?:{_ACTIVITY_MODIFIER}\s*)*"
    rf"{_ACTIVITY_PREFIX_STATE}{_ACTIVITY_EVENT}{_ACTIVITY_STATE}(?:\s*{_ACTIVITY_DATE})?"
)
_ACTIVITY_PAREN = re.compile(
    rf"[（(【]\s*{_ACTIVITY_SUFFIX_BODY}\s*[!！]*\s*[）)】]$", re.IGNORECASE,
)
_ACTIVITY_DASH = re.compile(
    rf"[-—_]\s*{_ACTIVITY_SUFFIX_BODY}\s*[!！]*\s*$", re.IGNORECASE,
)
_PROMO_REWARD = (
    r"(?:\d+|[一二三四五六七八九十百千万]+)(?:连抽|抽|元|个|份|套|枚|钻石|金币|代金券)?|"
    r"真充|礼包|代金券|时装|奖励|钻石|金币|福利|豪礼|皮肤|道具"
)
_PROMO_SUFFIX_BODY = (
    rf"(?:(?:登录|登陆|安装|预约|首发|公测|上线|开服|全员)(?:即)?"
    rf"(?:(?:赠送|领取|送|赢|锁定).{{1,32}}|(?:领|得|抽)\s*(?:{_PROMO_REWARD}).{{0,20}})|"
    rf"(?:赠送|送)\s*(?:{_PROMO_REWARD}).{{0,20}})"
)
_PROMO_PAREN = re.compile(
    rf"[（(]\s*{_PROMO_SUFFIX_BODY}\s*[）)]$", re.IGNORECASE,
)
_PROMO_DASH = re.compile(
    rf"[-—_]\s*{_PROMO_SUFFIX_BODY}\s*$", re.IGNORECASE,
)
_CAMPAIGN_DASH = re.compile(
    r"[-—_](?:[^-—_]{1,40}(?:联动开启|联动上线)|\d+周年庆|"
    r"\d+月新版本|S\d+新赛季开启)[!！]*$",
    re.IGNORECASE,
)
_TRAILING_BRACKET_QUALIFIER = re.compile(
    r"^(?P<base>.+?)(?:\((?P<paren>[^()]{1,80})\)|【(?P<bracket>[^【】]{1,80})】)$"
)
_TRAILING_SEPARATOR_QUALIFIER = re.compile(
    r"^(?P<base>.+?)[-—_](?P<separator>[^-—_]{1,80})$"
)
_MARKETING_QUALIFIER = re.compile(
    r"(?:官服|官方|体验|测试|开测|公测|内测|招募|预约|预下载|首发|上线|"
    r"手游|游戏|新游|新作|正版|授权|代言|登录|登陆|安装|赠|送|抽|赢|领|"
    r"礼包|福利|真充|低折|高爆|打金|联动|赛季|版本|周年|庆|"
    r"超变|传奇|职业|屠龙|放置|卡牌|回合制|像素|肉鸽|搜打撤|解谜|"
    r"动作格斗|模拟|沉浸式|刷宝|武道会|全明星|美食节|制霸|代号)",
    re.IGNORECASE,
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
    "一念长安(入长安开启回合新章)": "一念长安",
    "夜幕之下-全员恶人群像": "夜幕之下",
    "绝境猎人(西部狂欢热)": "绝境猎人",
    "开天英雄(超变传奇刀刀真充)": "开天英雄",
    "至尊传说(超变传奇)": "至尊传说",
}

# 无法仅靠通用营销词安全判断、但已由真实渠道数据核验的结构化别名。
# 与 _VERIFIED_NAME_ALIASES 不同，这些名称仅在有目录上下文时用于实体匹配，
# 单独出现时仍保留渠道原名作为展示名。
_VERIFIED_STRUCTURAL_BASES = {
    "土豆兄弟-Brotato": "土豆兄弟",
    "王者守卫战-经典三职业屠龙": "王者守卫战",
    "王者守卫战(经典三职业屠龙传奇)": "王者守卫战",
}


def clean_game_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", name or "").strip()
    value = _VERIFIED_NAME_ALIASES.get(value, value)
    previous = None
    while value != previous:
        previous = value
        value = _CHANNEL_SUFFIXES.sub("", value).strip()
        value = _ACTIVITY_PAREN.sub("", value).strip()
        value = _ACTIVITY_DASH.sub("", value).strip()
        value = _PROMO_PAREN.sub("", value).strip()
        value = _PROMO_DASH.sub("", value).strip()
        value = _CAMPAIGN_DASH.sub("", value).strip()
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


def _structural_base_candidates(name: str) -> list[tuple[str, str, str]]:
    """提取尾部括号或连接符文案之前的基础名，仅供全量实体匹配。"""
    initial = unicodedata.normalize("NFKC", name or "").strip()
    pending = [(initial, "")]
    seen_values = {initial}
    candidates: dict[str, tuple[str, str]] = {}
    for _ in range(3):
        next_pending = []
        for value, removed in pending:
            for pattern in (_TRAILING_BRACKET_QUALIFIER, _TRAILING_SEPARATOR_QUALIFIER):
                match = pattern.match(value)
                if not match:
                    continue
                base = clean_game_name(match.group("base")).strip()
                qualifier = next(
                    (match.group(key) for key in ("paren", "bracket", "separator")
                     if key in match.groupdict() and match.group(key)),
                    "",
                )
                qualifier = " ".join(part for part in (qualifier, removed) if part)
                normalized = normalize_game_name(base)
                # 单字符基础名误合并风险过高，不参与结构化候选匹配。
                if len(normalized) < 2:
                    continue
                current = candidates.get(normalized)
                if current is None or (len(base), base) < (len(current[0]), current[0]):
                    candidates[normalized] = (base, qualifier)
                if base not in seen_values:
                    seen_values.add(base)
                    next_pending.append((base, qualifier))
        pending = next_pending
        if not pending:
            break
    return sorted(
        (
            (normalized, display_name, qualifier)
            for normalized, (display_name, qualifier) in candidates.items()
        ),
        key=lambda item: (-len(item[0]), len(item[1]), item[1]),
    )


def _package_family(value: str | None) -> str:
    package = (value or "").strip().casefold()
    for suffix in (
        ".nearme.gamecenter", ".huawei", ".honor", ".vivo", ".xiaomi", ".mi", ".oppo",
    ):
        if package.endswith(suffix) and len(package) > len(suffix):
            return package[:-len(suffix)]
    return package


def _normalized_party(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _shares_stable_identity(left: list[sqlite3.Row], right: list[sqlite3.Row]) -> bool:
    source_ids = {
        (row["source"], row["source_item_id"])
        for row in left if row["source_item_id"]
    } & {
        (row["source"], row["source_item_id"])
        for row in right if row["source_item_id"]
    }
    packages = {
        _package_family(row["package_name"])
        for row in left if _package_family(row["package_name"])
    } & {
        _package_family(row["package_name"])
        for row in right if _package_family(row["package_name"])
    }
    detail_urls = {
        row["detail_url"] for row in left if row["detail_url"]
    } & {
        row["detail_url"] for row in right if row["detail_url"]
    }
    return bool(source_ids or packages or detail_urls)


def _has_name_merge_evidence(
    variant_rows: list[sqlite3.Row], base_rows: list[sqlite3.Row], qualifier: str,
) -> bool:
    if _shares_stable_identity(variant_rows, base_rows):
        return True
    if not _MARKETING_QUALIFIER.search(qualifier):
        return False
    variant_sources = {row["source"] for row in variant_rows}
    base_sources = {row["source"] for row in base_rows}
    # 同一渠道的不同条目不能仅凭标题前缀和开发商相同相互“自证”；这类数据
    # 可能是本体与衍生版。真正相同的条目应已由 source_item_id、包族或详情 URL
    # 在 _shares_stable_identity 中确认。
    if variant_sources & base_sources:
        return False
    variant_packages = {
        _package_family(row["package_name"])
        for row in variant_rows if _package_family(row["package_name"])
    }
    base_packages = {
        _package_family(row["package_name"])
        for row in base_rows if _package_family(row["package_name"])
    }
    if variant_packages and base_packages and not (variant_packages & base_packages):
        return False
    developers = {
        _normalized_party(row["developer"])
        for row in variant_rows if _normalized_party(row["developer"])
    } & {
        _normalized_party(row["developer"])
        for row in base_rows if _normalized_party(row["developer"])
    }
    return bool(developers)


def _flatten_key_redirects(
    conn: sqlite3.Connection, redirects: dict[str, set[str]],
) -> dict[str, str]:
    """合并新旧产品键重定向，拒绝一对多、环和自环，并压平到最终键。"""
    ambiguous = {
        old_key: targets for old_key, targets in redirects.items()
        if len(targets) > 1
    }
    if ambiguous:
        details = "; ".join(
            f"{old_key} -> {', '.join(sorted(targets))}"
            for old_key, targets in sorted(ambiguous.items())
        )
        raise RuntimeError(f"产品键出现一对多拆分，拒绝产生孤儿收藏：{details}")

    direct = {
        row["old_key"]: row["new_key"]
        for row in conn.execute(
            "SELECT old_key,new_key FROM canonical_key_redirects"
        )
    }
    for old_key, targets in redirects.items():
        if len(targets) == 1:
            target = next(iter(targets))
            if target == old_key:
                if old_key in direct:
                    raise RuntimeError(
                        "当前产品键仍在使用但同时存在历史重定向，拒绝迁移收藏："
                        f"{old_key} -> {direct[old_key]}"
                    )
                continue
            direct[old_key] = target

    flattened: dict[str, str] = {}
    for origin in sorted(direct):
        current = origin
        path: list[str] = []
        positions: dict[str, int] = {}
        while current in direct:
            if current in positions:
                cycle = path[positions[current]:] + [current]
                raise RuntimeError(
                    "产品键重定向出现环，拒绝迁移收藏：" + " -> ".join(cycle)
                )
            positions[current] = len(path)
            path.append(current)
            target = direct[current]
            if not target or target == current:
                raise RuntimeError(
                    f"产品键重定向出现自环，拒绝迁移收藏：{current} -> {target}"
                )
            current = target
        if current == origin:
            raise RuntimeError(
                f"产品键重定向出现自环，拒绝迁移收藏：{origin} -> {current}"
            )
        flattened[origin] = current
    return flattened


def _resolve_name_identities(
    rows: list[sqlite3.Row],
) -> tuple[dict[int, str], dict[str, str]]:
    """为所有来源记录生成统一产品键，并用相互印证的基础名归并营销变体。"""
    current_by_id = {
        row["id"]: canonical_key_for(row["name"], row["package_name"])
        for row in rows
    }
    observed_keys = set(current_by_id.values())
    rows_by_key: dict[str, list[sqlite3.Row]] = defaultdict(list)
    candidates_by_key: dict[str, dict[str, tuple[str, str, bool]]] = defaultdict(dict)
    for row in rows:
        rows_by_key[current_by_id[row["id"]]].append(row)

    for row in rows:
        current_key = current_by_id[row["id"]]
        raw_name = unicodedata.normalize("NFKC", row["name"] or "").strip()
        verified_base = _VERIFIED_STRUCTURAL_BASES.get(raw_name)
        if verified_base:
            base_key = f"name:{normalize_game_name(verified_base)}"
            candidates_by_key[current_key][base_key] = (verified_base, "", True)
        for normalized, display_name, qualifier in _structural_base_candidates(row["name"]):
            base_key = f"name:{normalized}"
            if base_key == current_key:
                continue
            existing = candidates_by_key[current_key].get(base_key)
            if existing is None or (len(display_name), display_name) < (len(existing[0]), existing[0]):
                candidates_by_key[current_key][base_key] = (display_name, qualifier, False)

    verified_bases = {
        base_key
        for candidates in candidates_by_key.values()
        for base_key, (_, _, verified) in candidates.items()
        if verified
    }
    parent = {key: key for key in observed_keys | verified_bases}
    for current_key, candidates in candidates_by_key.items():
        eligible = [
            key for key, (_, qualifier, verified) in candidates.items()
            if verified or (
                key in observed_keys and _has_name_merge_evidence(
                    rows_by_key[current_key], rows_by_key[key], qualifier,
                )
            )
        ]
        if eligible:
            parent[current_key] = min(
                eligible,
                key=lambda key: (-len(key.removeprefix("name:")), key),
            )

    def root_for(key: str) -> str:
        seen = set()
        while parent.get(key, key) != key and key not in seen:
            seen.add(key)
            key = parent[key]
        return key

    root_by_id = {
        row_id: root_for(current_key)
        for row_id, current_key in current_by_id.items()
    }
    display_options: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        root = root_by_id[row["id"]]
        cleaned = clean_game_name(row["name"])
        if canonical_key_for(cleaned, row["package_name"]) == root:
            display_options[root].add(cleaned)
        current_key = current_by_id[row["id"]]
        for base_key, (display_name, _, _) in candidates_by_key.get(current_key, {}).items():
            if base_key == root:
                display_options[root].add(display_name)

    display_by_key = {}
    for row in rows:
        root = root_by_id[row["id"]]
        if root in display_by_key:
            continue
        options = display_options.get(root) or {clean_game_name(row["name"])}
        display_by_key[root] = min(options, key=lambda value: (len(value), value))
    return root_by_id, display_by_key


def _migrate_game_key_references(
    conn: sqlite3.Connection, redirects: dict[str, set[str]],
) -> dict[str, str]:
    """产品键合并时迁移当前收藏，并保留可追溯的旧键重定向。"""
    flattened = _flatten_key_redirects(conn, redirects)
    migrated_at = datetime.now().astimezone().isoformat()
    for old_key, new_key in flattened.items():
        conn.execute(
            """
            INSERT INTO canonical_key_redirects(old_key,new_key,reason,created_at)
            VALUES (?,?,'normalized_name_merge',?)
            ON CONFLICT(old_key) DO UPDATE SET
                new_key=excluded.new_key,
                reason=excluded.reason
            """,
            (old_key, new_key, migrated_at),
        )
        favorites = list(conn.execute(
            "SELECT user_id,created_at,last_followed_at FROM user_favorites WHERE game_key=?",
            (old_key,),
        ))
        for favorite in favorites:
            existing = conn.execute(
                "SELECT created_at,last_followed_at FROM user_favorites WHERE user_id=? AND game_key=?",
                (favorite["user_id"], new_key),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE user_favorites SET created_at=?,last_followed_at=?
                    WHERE user_id=? AND game_key=?
                    """,
                    (
                        min(existing["created_at"], favorite["created_at"]),
                        max(existing["last_followed_at"], favorite["last_followed_at"]),
                        favorite["user_id"], new_key,
                    ),
                )
                conn.execute(
                    "DELETE FROM user_favorites WHERE user_id=? AND game_key=?",
                    (favorite["user_id"], old_key),
                )
            else:
                conn.execute(
                    "UPDATE user_favorites SET game_key=? WHERE user_id=? AND game_key=?",
                    (new_key, favorite["user_id"], old_key),
                )
    return flattened


def _record_game_id_redirects(
    conn: sqlite3.Connection,
    key_redirects: dict[str, str],
    previous_game_ids: dict[str, int],
    current_game_ids: dict[str, int],
) -> None:
    """记录改键前的公开数字 ID，并把历史 ID 链压平到当前产品。"""
    direct = {
        int(row["old_game_id"]): int(row["new_game_id"])
        for row in conn.execute(
            "SELECT old_game_id,new_game_id FROM canonical_game_id_redirects"
        )
    }
    for old_key, new_key in key_redirects.items():
        old_id = previous_game_ids.get(old_key)
        new_id = current_game_ids.get(new_key)
        if old_id is not None and new_id is not None and old_id != new_id:
            direct[old_id] = new_id

    flattened: dict[int, int] = {}
    for origin in sorted(direct):
        current = origin
        path: list[int] = []
        positions: dict[int, int] = {}
        while current in direct:
            if current in positions:
                cycle = path[positions[current]:] + [current]
                raise RuntimeError(
                    "产品 ID 重定向出现环，拒绝更新目录："
                    + " -> ".join(str(item) for item in cycle)
                )
            positions[current] = len(path)
            path.append(current)
            target = direct[current]
            if target <= 0 or target == current:
                raise RuntimeError(
                    f"产品 ID 重定向无效，拒绝更新目录：{current} -> {target}"
                )
            current = target
        flattened[origin] = current

    migrated_at = datetime.now().astimezone().isoformat()
    for old_id, new_id in flattened.items():
        conn.execute(
            """
            INSERT INTO canonical_game_id_redirects(
                old_game_id,new_game_id,reason,created_at
            ) VALUES (?,?,'normalized_name_merge',?)
            ON CONFLICT(old_game_id) DO UPDATE SET
                new_game_id=excluded.new_game_id,
                reason=excluded.reason
            """,
            (old_id, new_id, migrated_at),
        )


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
    if conn.in_transaction:
        raise RuntimeError("重建产品目录前存在未提交事务")
    conn.execute("BEGIN IMMEDIATE")
    try:
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
            conn.executemany(
                "DELETE FROM canonical_members WHERE source_row_id=?",
                [(item_id,) for item_id in invalid_ids],
            )
            conn.executemany(
                "DELETE FROM source_items WHERE id=?",
                [(item_id,) for item_id in invalid_ids],
            )

        rows = list(conn.execute("SELECT * FROM source_items ORDER BY id"))
        previous_game_ids = {
            row["canonical_key"]: row["id"]
            for row in conn.execute("SELECT id,canonical_key FROM canonical_games")
        }
        key_by_id, display_by_key = _resolve_name_identities(rows)
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        redirects: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            key = key_by_id[row["id"]]
            groups[key].append(row)
            if row["canonical_key"]:
                redirects[row["canonical_key"]].add(key)

        key_redirects = _migrate_game_key_references(conn, redirects)
        conn.execute("DELETE FROM canonical_members")
        current_game_ids: dict[str, int] = {}
        for key, members in groups.items():
            display_name = display_by_key[key]
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
            event_keys = {
                (row["source"], row["event_type"])
                for row in members
            }
            conn.execute(
                """
                INSERT INTO canonical_games (
                    canonical_key, name, normalized_name, developer, category,
                    tags_json, gameplay_intro, icon_url, rating, first_seen_at,
                    last_seen_at, source_count, event_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    name=excluded.name,
                    normalized_name=excluded.normalized_name,
                    developer=excluded.developer,
                    category=excluded.category,
                    tags_json=excluded.tags_json,
                    gameplay_intro=excluded.gameplay_intro,
                    icon_url=excluded.icon_url,
                    rating=excluded.rating,
                    first_seen_at=excluded.first_seen_at,
                    last_seen_at=excluded.last_seen_at,
                    source_count=excluded.source_count,
                    event_count=excluded.event_count
                """,
                (
                    key, display_name, key.removeprefix("name:") if key.startswith("name:")
                    else normalize_game_name(display_name), developer, category,
                    json.dumps(tags, ensure_ascii=False), intro, icon,
                    round(sum(ratings) / len(ratings), 1) if ratings else None,
                    min(row["first_seen_at"] for row in members),
                    max(row["last_seen_at"] for row in members),
                    len(sources), len(event_keys),
                ),
            )
            game_id = conn.execute(
                "SELECT id FROM canonical_games WHERE canonical_key=?", (key,)
            ).fetchone()[0]
            current_game_ids[key] = game_id
            conn.executemany(
                "INSERT INTO canonical_members(game_id, source_row_id) VALUES (?, ?)",
                [(game_id, row["id"]) for row in members],
            )
            conn.executemany(
                "UPDATE source_items SET canonical_key=? WHERE id=?",
                [(key, row["id"]) for row in members],
            )
        _record_game_id_redirects(
            conn, key_redirects, previous_game_ids, current_game_ids,
        )
        conn.execute(
            "DELETE FROM canonical_games WHERE id NOT IN (SELECT DISTINCT game_id FROM canonical_members)"
        )
        conn.commit()
        return len(groups)
    except Exception:
        conn.rollback()
        raise


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
