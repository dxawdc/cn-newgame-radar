#!/usr/bin/env bash
set -euo pipefail

release_id="${1:?release id is required}"
base_dir="/opt/newgame-monitor"
state_dir="${NEWGAME_STATE_DIR:-/var/lib/newgame-monitor}"
incoming_dir="$base_dir/incoming/$release_id"
release_dir="$base_dir/releases/$release_id"
current_dir="$base_dir/current"
python_bin="$base_dir/venv/bin/python"
state_db="$state_dir/data/newgame_monitor.db"
snapshot_dir="$state_dir/backups"
snapshot_db="$snapshot_dir/pre-release-$release_id.db"
previous_release="$(readlink -f "$current_dir" 2>/dev/null || true)"
switched=0

test -f "$incoming_dir/release.tar.gz"
test -f "$incoming_dir/media_manifest.json"
test -f "$incoming_dir/apply_media_manifest.py"
test ! -e "$release_dir"

install -d -m 0750 -o newgame-monitor -g newgame-monitor \
  "$state_dir" "$state_dir/data" "$state_dir/data/icons" \
  "$state_dir/data/screenshots" "$state_dir/raw" "$snapshot_dir"

# 首次切换到持久目录时，从当前活动库做在线一致快照；后续 release 只挂载该目录。
if [[ ! -f "$state_db" ]]; then
  test -f "$current_dir/data/newgame_monitor.db"
  "$python_bin" - "$current_dir/data/newgame_monitor.db" "$state_db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    result = target.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result}")
finally:
    target.close()
    source.close()
PY
  if [[ -d "$current_dir/data/icons" ]]; then
    cp -a "$current_dir/data/icons/." "$state_dir/data/icons/"
  fi
  if [[ -d "$current_dir/data/screenshots" ]]; then
    cp -a "$current_dir/data/screenshots/." "$state_dir/data/screenshots/"
  fi
  if [[ -d "$current_dir/raw" ]]; then
    cp -a "$current_dir/raw/." "$state_dir/raw/"
  fi
fi

# 每次发布保留可验证的一致快照，供人工回滚演练使用。
"$python_bin" - "$state_db" "$snapshot_db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    result = target.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result}")
finally:
    target.close()
    source.close()
PY

rollback_release() {
  if [[ "$switched" == 1 && -n "$previous_release" && -d "$previous_release" ]]; then
    ln -sfn "$previous_release" "$base_dir/current.rollback"
    mv -Tf "$base_dir/current.rollback" "$current_dir"
    systemctl restart newgame-monitor.service || true
  fi
}
trap rollback_release ERR

install -d "$release_dir"
if [[ -d "$current_dir" ]]; then
  cp -a "$current_dir/." "$release_dir/"
fi
tar -xzf "$incoming_dir/release.tar.gz" -C "$release_dir"

# release 仅承载代码；可变数据库、媒体和 raw 始终位于 state_dir。
rm -rf -- "$release_dir/data" "$release_dir/raw"
ln -s "$state_dir/data" "$release_dir/data"
ln -s "$state_dir/raw" "$release_dir/raw"
install -m 0755 "$incoming_dir/apply_media_manifest.py" "$release_dir/scripts/apply_media_manifest.py"
install -d "$release_dir/runtime"
install -m 0644 "$incoming_dir/media_manifest.json" "$release_dir/runtime/media_manifest.json"

"$python_bin" "$release_dir/scripts/apply_media_manifest.py" \
  --db "$state_db" \
  --manifest "$release_dir/runtime/media_manifest.json"

PYTHONPATH="$release_dir/src" "$python_bin" - "$state_dir" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

from newgame_monitor.catalog import audit_catalog_completeness, rebuild_catalog
from newgame_monitor.db import connect
from newgame_monitor.gallery import cache_remote_screenshots

root = Path(sys.argv[1])
conn = connect(root / "data/newgame_monitor.db")
try:
    result = {
        "games": rebuild_catalog(conn),
        "screenshots": cache_remote_screenshots(conn, root / "data/screenshots"),
        "catalog": audit_catalog_completeness(conn),
        "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
    }
    if result["quick_check"] != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {result['quick_check']}")
finally:
    conn.close()
print(json.dumps(result, ensure_ascii=False))
PY

chown -R newgame-monitor:newgame-monitor "$state_dir"
chown -R newgame-monitor:newgame-monitor "$release_dir"
ln -sfn "$release_dir" "$base_dir/current.next"
mv -Tf "$base_dir/current.next" "$current_dir"
switched=1

install -m 0644 "$release_dir/deploy/newgame-monitor-daily.service" /etc/systemd/system/newgame-monitor-daily.service
install -m 0644 "$release_dir/deploy/newgame-monitor-daily.timer" /etc/systemd/system/newgame-monitor-daily.timer
systemctl daemon-reload
systemctl restart newgame-monitor.service
systemctl restart newgame-monitor-daily.timer

systemctl is-active newgame-monitor.service
systemctl is-active newgame-monitor-daily.timer
curl --fail --silent --show-error "http://127.0.0.1:18765/livez"
curl --fail --silent --show-error "http://127.0.0.1:18765/readyz"
trap - ERR
echo "release=$release_id state=$state_dir snapshot=$snapshot_db"
