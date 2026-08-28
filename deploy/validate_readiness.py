"""比较发布前后的 readyz，拒绝新版本引入新的就绪故障。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


READY_STATUSES = {"ready", "ready_degraded"}


def validate_readiness(before: dict, after: dict) -> str:
    if after.get("status") in READY_STATUSES:
        return "readyz passed"
    before_reasons = set(before.get("reasons") or [])
    after_reasons = set(after.get("reasons") or [])
    if before.get("status") == "not_ready" and after_reasons <= before_reasons:
        return "readyz remains at accepted pre-release baseline: " + ", ".join(
            sorted(after_reasons)
        )
    raise RuntimeError(
        "readyz regressed after release: "
        f"before={sorted(before_reasons)} after={sorted(after_reasons)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    print(validate_readiness(before, after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
