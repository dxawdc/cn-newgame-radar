"""低频真实站点契约冒烟，只读取公开列表页，不写入数据库。"""

from __future__ import annotations

import json
from datetime import datetime

from newgame_monitor.collectors import (
    _get,
    _parse_9game_schedule_html,
    collect_taptap,
)


def main() -> int:
    results = {}
    failures = []
    try:
        items, _ = collect_taptap()
        results["taptap"] = {"status": "success", "items": len(items)}
        if not items:
            failures.append("taptap: empty")
    except Exception as exc:
        results["taptap"] = {"status": "failed", "error": str(exc)}
        failures.append("taptap: failed")

    try:
        response = _get(
            "https://main.gamecenter.vivo.com.cn/clientRequest/newGameZone/firstPublishList",
            params={"pageIndex": 1, "pageSize": 20},
        )
        data = (response.json() or {}).get("data") or {}
        games = data.get("listData") or []
        contract_ok = isinstance(data.get("hasNext"), bool) and isinstance(games, list)
        results["vivo"] = {
            "status": "success" if contract_ok and games else "failed",
            "items": len(games),
            "has_next": data.get("hasNext"),
            "contract_ok": contract_ok,
        }
        if not contract_ok or not games:
            failures.append("vivo: page contract")
    except Exception as exc:
        results["vivo"] = {"status": "failed", "error": str(exc)}
        failures.append("vivo: failed")

    try:
        response = _get("https://www.9game.cn/kc/")
        items = _parse_9game_schedule_html(response.content)
        results["9game"] = {"status": "success", "items": len(items)}
        if not items:
            failures.append("9game: empty")
    except Exception as exc:
        results["9game"] = {"status": "failed", "error": str(exc)}
        failures.append("9game: failed")

    print(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": "failed" if failures else "success",
        "failures": failures,
        "sources": results,
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
