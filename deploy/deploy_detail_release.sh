#!/usr/bin/env bash
# 仅发布详情接口和静态页面，失败时自动恢复上一版本。
set -euo pipefail
release_id="${1:?缺少发布编号}"
expected_previous="${2:?缺少预期的上一版本目录}"
[[ "$release_id" =~ ^[0-9]{8}-[0-9]{6}-detail-performance$ ]]
base=/opt/newgame-monitor
incoming="$base/incoming/$release_id"
release="$base/releases/$release_id"
previous="$(readlink -f "$base/current")"
test "$previous" = "$expected_previous"
test ! -e "$release"

# 与入库、媒体和采集任务互斥；不停止或重新配置其他服务。
exec 9</run/lock/newgame-monitor-collector.lock
flock -w 60 9
(cd "$incoming" && sha256sum -c release.sha256)
mkdir -p "$release"
cp -a "$previous/." "$release/"
tar -xzf "$incoming/release.tar.gz" -C "$release"
chmod go-w "$release/src/newgame_monitor/webapp.py" "$release/src/newgame_monitor/static/"{app.js,app.css,index.html}
/opt/newgame-monitor/venv/bin/python -m py_compile "$release/src/newgame_monitor/webapp.py"
env PYTHONPATH="$release/src" NEWGAME_DB=/var/lib/newgame-monitor/data/newgame_monitor.db \
  NEWGAME_ICON_DIR=/var/lib/newgame-monitor/data/icons NEWGAME_SCREENSHOT_DIR=/var/lib/newgame-monitor/data/screenshots \
  NEWGAME_BASE_PATH=/newgame /opt/newgame-monitor/venv/bin/python "$incoming/validate_detail_release.py" \
  > "$incoming/detail-validation.json"

curl -sS --max-time 15 http://127.0.0.1:18765/readyz > "$incoming/ready-before.json"
/opt/newgame-monitor/venv/bin/python "$previous/scripts/sqlite_backup.py" \
  /var/lib/newgame-monitor/data/newgame_monitor.db "/var/backups/newgame-monitor/pre-$release_id.db"

rollback() {
  trap - ERR
  echo "发布验证失败，恢复 $previous" >&2
  ln -sfn "$previous" "$base/current.rollback"
  mv -Tf "$base/current.rollback" "$base/current"
  systemctl restart newgame-monitor
}
trap rollback ERR
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl restart newgame-monitor
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:18765/livez > /dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:18765/livez
curl -sS --max-time 15 http://127.0.0.1:18765/readyz > "$incoming/ready-after.json"
/opt/newgame-monitor/venv/bin/python "$release/deploy/validate_readiness.py" \
  "$incoming/ready-before.json" "$incoming/ready-after.json"
# 校验正式服务确实提供新前端及可用的详情接口。
curl -fsS --max-time 5 http://127.0.0.1:18765/ > "$incoming/index-after.html"
grep -q '20261012-detail-fast' "$incoming/index-after.html"
curl -fsS --max-time 5 http://127.0.0.1:18765/api/v2/games/b69fa085-6ac3-5113-8253-b17afe038dad \
  | /opt/newgame-monitor/venv/bin/python -c 'import json,sys; p=json.load(sys.stdin); assert p.get("uuid") and "latest_intro" in p and "gallery" in p'
systemctl is-active newgame-monitor
trap - ERR
echo "release=$release_id previous=$previous"
