#!/usr/bin/env bash
set -euo pipefail

release_id="${1:?release id is required}"
base_dir="/opt/newgame-monitor"
incoming_dir="$base_dir/incoming/$release_id"
release_dir="$base_dir/releases/$release_id"
current_dir="$base_dir/current"
python_bin="$base_dir/venv/bin/python"

test -f "$incoming_dir/release.tar.gz"
test -f "$incoming_dir/media_manifest.json"
test -f "$incoming_dir/apply_media_manifest.py"
test ! -e "$release_dir"

install -d "$release_dir"
cp -a "$current_dir/." "$release_dir/"

# 从正在服务的数据库做 SQLite 一致性快照，保留账号、关注和 API Key。
"$python_bin" - "$current_dir/data/newgame_monitor.db" "$release_dir/data/newgame_monitor.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY

tar -xzf "$incoming_dir/release.tar.gz" -C "$release_dir"
install -m 0755 "$incoming_dir/apply_media_manifest.py" "$release_dir/scripts/apply_media_manifest.py"
install -d "$release_dir/runtime"
install -m 0644 "$incoming_dir/media_manifest.json" "$release_dir/runtime/media_manifest.json"

"$python_bin" "$release_dir/scripts/apply_media_manifest.py" \
  --db "$release_dir/data/newgame_monitor.db" \
  --manifest "$release_dir/runtime/media_manifest.json"

PYTHONPATH="$release_dir/src" "$python_bin" - "$release_dir" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

from newgame_monitor.catalog import audit_catalog_completeness, rebuild_catalog
from newgame_monitor.gallery import cache_remote_screenshots

root = Path(sys.argv[1])
conn = sqlite3.connect(root / "data/newgame_monitor.db")
conn.row_factory = sqlite3.Row
try:
    result = {
        "games": rebuild_catalog(conn),
        "screenshots": cache_remote_screenshots(conn, root / "data/screenshots"),
        "catalog": audit_catalog_completeness(conn),
    }
finally:
    conn.close()
print(json.dumps(result, ensure_ascii=False))
PY

chown -R newgame-monitor:newgame-monitor "$release_dir"
ln -sfn "$release_dir" "$base_dir/current.next"
mv -Tf "$base_dir/current.next" "$current_dir"

install -m 0644 "$release_dir/deploy/newgame-monitor-daily.service" /etc/systemd/system/newgame-monitor-daily.service
install -m 0644 "$release_dir/deploy/newgame-monitor-daily.timer" /etc/systemd/system/newgame-monitor-daily.timer
systemctl daemon-reload
systemctl restart newgame-monitor.service
systemctl restart newgame-monitor-daily.timer

systemctl is-active newgame-monitor.service
systemctl is-active newgame-monitor-daily.timer
curl --fail --silent --show-error "http://127.0.0.1:18765/api/health"
