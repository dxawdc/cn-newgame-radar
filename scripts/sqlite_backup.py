"""使用 SQLite Backup API 生成可校验的一致快照。"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def create_snapshot(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite quick_check 失败：{result}")
    finally:
        target.close()
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 SQLite 一致快照")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    create_snapshot(args.source, args.target)
    print(args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
