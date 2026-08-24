"""来源事件的业务口径校验与历史误报清理。"""
from __future__ import annotations

import json
import re
import sqlite3


_SERVICE_VARIANT_NAME = re.compile(
    r"(?:体验服|先遣服|共研服|先锋服|怀旧服|渠道服|测试服)",
    re.IGNORECASE,
)
_BETA_EVENT = re.compile(
    r"(?:删档|不删档|不限量|限量|抢注|海外)?(?:首测|终测|测试|开测)"
)
_RECRUITING_BETA = re.compile(
    r"(?:招募[^，。；]{0,24}(?:试玩|测试|首测)资格|"
    r"(?:试玩|测试|首测)资格[^，。；]{0,24}招募|(?:首测|测试)招募)"
)
_PRE_DOWNLOAD = re.compile(r"预下载")
_PRODUCT_LAUNCH = re.compile(
    r"^(?:\d{1,2}:\d{2}\s*)?(?:(?:正式|已|现已)\s*)?上线(?:试玩)?(?:[，,。!！]|$)|"
    r"^(?:\d{1,2}:\d{2}\s*)?(?:正式|已|现已)上线(?:[，,。!！]|$)|"
    r"(?:PC端|移动端|安卓(?:版)?|iOS|海外|全平台|双端|国服)"
    r"[^，。；]{0,16}(?:正式)?上线"
)
_RESERVATION_EVENT = re.compile(
    r"(?:正式)?(?:开启|开放)预约|"
    r"^(?:\d{1,2}:\d{2}\s*)?(?:正式)?开启预购(?:[，,。!！]|$)"
)
_OPERATIONAL_RELEASE = re.compile(
    r"(?:玩法|皮肤|武器|角色|英雄|副本|地图|区域|国度|DLC|曲包|套装|"
    r"卡牌?|活动|联动|赛季|战令|版本|主线|时装|外观|宠物|伙伴|礼包|"
    r"军需|捆绑包|系列|关卡|忍者|战员)"
    r"[^，。；]{0,18}(?:上线|开放|开启|登场|返场|更新)",
    re.IGNORECASE,
)


def classify_haoyou_event(name: str, summary: str) -> str | None:
    """将好游快爆混合时间轴收紧为整款产品的上线、测试或预约事件。

    页面还包含皮肤、赛季、联动、版本更新和体验服开服等老游戏运营内容；
    这些内容不属于本项目的“新游动态”，返回 ``None``。
    """
    game_name = (name or "").strip()
    text = re.sub(r"\s+", " ", summary or "").strip()
    if not game_name or not text or _SERVICE_VARIANT_NAME.search(game_name):
        return None

    # “测试预下载”发生在正式开测前，应作为独立预下载事件保留。
    if _PRE_DOWNLOAD.search(text) and re.search(r"(?:上线|开测|测试|首测)", text):
        return "pre_download"

    if _RECRUITING_BETA.search(text):
        return "recruiting_beta"
    if _BETA_EVENT.search(text):
        return "limited_beta" if re.search(r"(?:限量|抢注)", text) else "beta"

    # 先识别内容/版本对象，避免“新皮肤上线”等被通用上线规则接纳。
    if _OPERATIONAL_RELEASE.search(text):
        return None
    if _PRODUCT_LAUNCH.search(text):
        return "launch"
    if _RESERVATION_EVENT.search(text):
        return "reservation"
    return None


def prune_legacy_haoyou_timeline(conn: sqlite3.Connection) -> dict[str, int]:
    """清理旧版本统一标记为 timeline 的好游快爆历史记录。"""
    removed = reclassified = duplicates = 0
    rows = list(conn.execute(
        """
        SELECT id, source_item_id, name, event_time, gameplay_intro, raw_json
        FROM source_items
        WHERE source='haoyou_kuaibao' AND event_type='timeline'
        ORDER BY id
        """
    ))
    with conn:
        for row in rows:
            summary = row["gameplay_intro"] or ""
            if not summary:
                try:
                    summary = json.loads(row["raw_json"] or "{}").get("text", "")
                except (json.JSONDecodeError, AttributeError):
                    summary = ""
            event_type = classify_haoyou_event(row["name"], summary)
            if event_type is None:
                conn.execute("DELETE FROM canonical_members WHERE source_row_id=?", (row["id"],))
                conn.execute("DELETE FROM source_items WHERE id=?", (row["id"],))
                removed += 1
                continue
            existing = conn.execute(
                """
                SELECT id FROM source_items
                WHERE source='haoyou_kuaibao' AND source_item_id=?
                  AND event_type=? AND event_time=? AND id<>?
                """,
                (row["source_item_id"], event_type, row["event_time"], row["id"]),
            ).fetchone()
            if existing:
                conn.execute("DELETE FROM canonical_members WHERE source_row_id=?", (row["id"],))
                conn.execute("DELETE FROM source_items WHERE id=?", (row["id"],))
                duplicates += 1
            else:
                conn.execute(
                    "UPDATE source_items SET event_type=?, status=? WHERE id=?",
                    (event_type, summary, row["id"]),
                )
                reclassified += 1
    return {
        "checked": len(rows),
        "removed": removed,
        "reclassified": reclassified,
        "duplicates": duplicates,
    }
