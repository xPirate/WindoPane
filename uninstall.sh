#!/usr/bin/env bash
# Remove WindoPane. Your streams/config in /var/lib/windopane are kept
# unless you pass --purge. Installed apt packages are left in place.
set -euo pipefail
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
MARK_BEGIN="# >>> windopane >>>"
MARK_END="# <<< windopane <<<"

pkill -f "^/opt/windopane/venv/bin/python3 -m windopane" || true
systemctl disable --now windopane-ytdlp-update.timer 2>/dev/null || true
rm -f /etc/systemd/system/windopane-ytdlp-update.{service,timer}
rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
rm -f /etc/X11/xorg.conf.d/99-windopane-vc4.conf /etc/X11/xorg.conf.d/20-windopane-amdgpu.conf
rm -f /etc/sudoers.d/windopane
rm -rf /etc/windopane /var/log/windopane   # the GPU drivers themselves stay installed
for f in /home/*/.profile /home/*/.bash_profile /boot/firmware/config.txt /boot/config.txt; do
  [ -f "$f" ] && sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$f"
done
rm -f /usr/local/bin/windopane-ctl
rm -rf /opt/windopane
[ "$PURGE" = 1 ] && rm -rf /var/lib/windopane
systemctl daemon-reload
echo "WindoPane removed.$([ "$PURGE" = 1 ] || echo " Config kept in /var/lib/windopane.") Reboot to return to the console."
