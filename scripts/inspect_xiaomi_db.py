"""只读查看小米游戏中心非账号业务缓存表。"""
import argparse
import subprocess
from pathlib import Path


DB = "/data/data/com.xiaomi.gamecenter/databases/gamecenter_v2"
SAFE_TABLES = ("GCDATA", "CATEGORY_TAB_INFO", "DISCOVERY")


def query(adb: Path, serial: str, sql: str) -> str:
    command = f"su -c 'sqlite3 {DB} \"{sql}\"'"
    result = subprocess.check_output([str(adb), "-s", serial, "shell", command], stderr=subprocess.DEVNULL)
    return result.decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", default="127.0.0.1:16384")
    args = parser.parse_args()
    for table in SAFE_TABLES:
        print(f"[{table} schema]")
        print(query(args.adb, args.serial, f"pragma table_info({table});"))
        print(f"[{table} sample]")
        if table == "GCDATA":
            print(query(args.adb, args.serial, "select TYPE,length(DATA) from GCDATA order by TYPE;"))
        else:
            print(query(args.adb, args.serial, f"select ID,length(DATA) from {table} limit 20;"))


if __name__ == "__main__":
    main()
