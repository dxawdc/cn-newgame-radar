#!/usr/bin/env bash
# 资源隔离修复仅部署代码与服务，不在发布期间运行全库重建或媒体下载。
set -euo pipefail
release_id="${1:?缺少发布编号}"
[[ "$release_id" =~ ^[0-9]{8}-[0-9]{6}-resources$ ]]
base=/opt/newgame-monitor
incoming="$base/incoming/$release_id"
release="$base/releases/$release_id"
previous="$(readlink -f "$base/current")"
test -d "$previous"
test ! -e "$release"
(cd "$incoming" && sha256sum -c release.sha256)
mkdir -p "$release"
cp -a "$previous/." "$release/"
tar -xzf "$incoming/release.tar.gz" -C "$release"
find "$release/src" "$release/scripts" "$release/deploy" -type f -exec chmod go-w {} +
/opt/newgame-monitor/venv/bin/python -m compileall -q "$release/src"
systemd-analyze verify "$release/deploy/newgame-monitor-import@.service" \
  "$release/deploy/newgame-monitor-media.service" "$release/deploy/newgame-monitor-media.timer" \
  "$release/deploy/newgame-monitor-daily.service" \
  "$release/deploy/newgame-monitor-details.service" "$release/deploy/newgame-monitor-details.timer"

# 备份受影响的单元，便于失败时原位恢复。
mkdir -p "$incoming/units" "$incoming/dropin"
for unit in newgame-monitor-daily.service newgame-monitor-import@.service newgame-monitor-media.service newgame-monitor-media.timer newgame-monitor-details.service newgame-monitor-details.timer; do
  if test -f "/etc/systemd/system/$unit"; then cp -a "/etc/systemd/system/$unit" "$incoming/units/"; fi
done
override=/etc/systemd/system/newgame-monitor.service.d/90-resources.conf
if test -f "$override"; then cp -a "$override" "$incoming/dropin/"; fi
rollback() {
  systemctl stop newgame-monitor-media.timer newgame-monitor-details.timer || true
  ln -sfn "$previous" "$base/current.rollback"
  mv -Tf "$base/current.rollback" "$base/current"
  for unit in newgame-monitor-daily.service newgame-monitor-import@.service newgame-monitor-media.service newgame-monitor-media.timer newgame-monitor-details.service newgame-monitor-details.timer; do
    if test -f "$incoming/units/$unit"; then
      cp -a "$incoming/units/$unit" "/etc/systemd/system/$unit"
    else
      rm -f "/etc/systemd/system/$unit"
    fi
  done
  if test -f "$incoming/dropin/90-resources.conf"; then cp -a "$incoming/dropin/90-resources.conf" "$override"; else rm -f "$override"; fi
  systemctl daemon-reload
  systemctl restart newgame-monitor
}
trap rollback ERR
install -d -o newgame-monitor -g newgame-monitor /var/lib/newgame-monitor/import-jobs
install -m 0644 "$release/deploy/newgame-monitor-lock.conf" /etc/tmpfiles.d/newgame-monitor-lock.conf
systemd-tmpfiles --create /etc/tmpfiles.d/newgame-monitor-lock.conf
for unit in newgame-monitor-daily.service newgame-monitor-import@.service newgame-monitor-media.service newgame-monitor-media.timer newgame-monitor-details.service newgame-monitor-details.timer; do
  install -m 0644 "$release/deploy/$unit" "/etc/systemd/system/$unit"
done
mkdir -p /etc/systemd/system/newgame-monitor.service.d
install -m 0644 "$release/deploy/newgame-monitor-resources.conf" "$override"
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl daemon-reload
systemctl restart newgame-monitor
systemctl enable --now newgame-monitor-media.timer newgame-monitor-details.timer
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:18765/livez; then break; fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:18765/livez
systemctl is-active newgame-monitor
trap - ERR
echo "release=$release_id previous=$previous"
