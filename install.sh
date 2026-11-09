#!/usr/bin/env bash
# WindoPane installer
#
# Turns a Raspberry Pi 4/5 (Raspberry Pi OS Lite) or a PC (Debian 12+ /
# Ubuntu 24.04+, minimal install without a desktop) into a boot-to-video-wall
# appliance.
#
#   sudo ./install.sh [--user NAME] [--port 8090] [--password SECRET]
#                     [--no-autostart] [--no-4k60]
#                     [--gpu auto|amd|intel|nvidia|none|skip]   (PCs only)
#
# Safe to re-run: it upgrades the app in place and keeps your streams.

set -euo pipefail

PREFIX=/opt/windopane
STATE=/var/lib/windopane
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-}"
PORT=""
PASSWORD=""
AUTOSTART=1
SET_4K60=1
GPU=auto
MARK_BEGIN="# >>> windopane >>>"
MARK_END="# <<< windopane <<<"

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user) TARGET_USER="${2:?}"; shift 2 ;;
    --port) PORT="${2:?}"; shift 2 ;;
    --password) PASSWORD="${2:?}"; shift 2 ;;
    --no-autostart) AUTOSTART=0; shift ;;
    --no-4k60) SET_4K60=0; shift ;;
    --gpu) GPU="${2:?}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) warn "unknown option: $1"; usage 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo ./install.sh"
[ -d "$SRC/app/windopane" ] || die "run this from the unpacked windopane folder"
command -v apt-get >/dev/null || die "this installer needs a Debian-based OS (Raspberry Pi OS Lite)"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
  TARGET_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
fi
[ -n "$TARGET_USER" ] && id "$TARGET_USER" >/dev/null 2>&1 || die "no login user found; pass --user NAME"
[ "$TARGET_USER" != root ] || die "refusing to run the wall as root; pass --user NAME"
case "$GPU" in auto|amd|intel|nvidia|none|skip) ;; *) die "--gpu must be auto, amd, intel, nvidia, none or skip" ;; esac
if [ -n "$PORT" ] && ! [[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]]; then
  die "--port must be a number between 1 and 65535"
fi
USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

MODEL="$( { tr -d '\0' </proc/device-tree/model; } 2>/dev/null || echo unknown)"
. /etc/os-release
say "WindoPane installer: $MODEL, $PRETTY_NAME, user '$TARGET_USER'"
case "$MODEL" in
  *"Raspberry Pi 4"*|*"Compute Module 4"*|*"Raspberry Pi 5"*|*"Compute Module 5"*) ;;
  *) warn "not a Pi 4/5 ($MODEL). Continuing, but limits and HDMI setup are tuned for those boards." ;;
esac

# --------------------------------------------------------------------------- packages
say "Installing packages (this takes a few minutes the first time)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

WANT=(
  xserver-xorg-core xserver-xorg-input-libinput xinit xauth x11-xserver-utils
  vlc-bin vlc-plugin-base vlc-plugin-video-output
  python3 python3-venv python3-pip
  python3-gi python3-gi-cairo gir1.2-gtk-3.0
  python3-vlc python3-flask python3-waitress python3-evdev
  pipewire pipewire-pulse pipewire-alsa wireplumber
  fonts-dejavu-core ca-certificates pciutils
)
if apt-cache show gir1.2-webkit2-4.1 >/dev/null 2>&1; then
  WANT+=(gir1.2-webkit2-4.1)
else
  WANT+=(gir1.2-webkit2-4.0)
fi
INSTALL=()
for p in "${WANT[@]}"; do
  if apt-cache show "$p" >/dev/null 2>&1; then INSTALL+=("$p"); else warn "package $p not available, skipping"; fi
done
apt-get install -y -qq --no-install-recommends "${INSTALL[@]}"

# --------------------------------------------------------------------------- app
say "Installing WindoPane to $PREFIX"
install -d -m 755 "$PREFIX" "$PREFIX/bin" "$PREFIX/app"
rm -rf "$PREFIX/app/windopane"
cp -a "$SRC/app/windopane" "$PREFIX/app/"
find "$PREFIX/app" -name '__pycache__' -prune -exec rm -rf {} +
for f in windopane windopane-session windopane-xinit windopane-ctl windopane-update-ytdlp windopane-gpu windopane-power; do
  install -m 755 "$SRC/system/$f" "$PREFIX/bin/$f"
done
ln -sf "$PREFIX/bin/windopane-ctl" /usr/local/bin/windopane-ctl

say "Setting up yt-dlp (+ Deno, which YouTube now requires)"
if [ ! -x "$PREFIX/venv/bin/python3" ]; then
  python3 -m venv --system-site-packages "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install -q --upgrade pip || true
"$PREFIX/venv/bin/pip" install -q --upgrade "yt-dlp[default]"
if ! "$PREFIX/venv/bin/pip" install -q --upgrade "deno>=2"; then
  warn "Deno could not be installed; YouTube streams may fail. Everything else works."
fi
"$PREFIX/venv/bin/yt-dlp" --version >/dev/null || warn "yt-dlp did not install correctly"

# --------------------------------------------------------------------------- state + config
say "Preparing $STATE"
install -d -m 750 -o "$TARGET_USER" -g "$TARGET_USER" "$STATE" "$STATE/logs"
PYTHONPATH="$PREFIX/app" WP_PORT="$PORT" WP_PASSWORD="$PASSWORD" \
  "$PREFIX/venv/bin/python3" - "$STATE/config.json" <<'PY'
import os, sys
from windopane import config
path = sys.argv[1]
cfg = config.load(path)
if os.environ.get("WP_PORT"):
    cfg["web"]["port"] = int(os.environ["WP_PORT"])
if os.environ.get("WP_PASSWORD"):
    cfg["web"]["password"] = os.environ["WP_PASSWORD"]
config.save(cfg, path)
print("    web UI port:", cfg["web"]["port"], "| password:", "set" if cfg["web"]["password"] else "none")
PY
chown "$TARGET_USER:$TARGET_USER" "$STATE/config.json"
chmod 640 "$STATE/config.json"

for g in video render input audio; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$TARGET_USER"
done

# --------------------------------------------------------------------------- display server
if ls /etc/X11/xorg.conf.d/*vc4* /usr/share/X11/xorg.conf.d/*vc4* >/dev/null 2>&1; then
  : # Raspberry Pi OS already tells X which GPU drives HDMI
else
  # Pi 5 exposes two DRM devices; make sure X uses the one wired to HDMI.
  install -d /etc/X11/xorg.conf.d
  cat >/etc/X11/xorg.conf.d/99-windopane-vc4.conf <<'EOF'
# Installed by WindoPane: drive the HDMI outputs (vc4) with modesetting.
Section "OutputClass"
    Identifier "vc4"
    MatchDriver "vc4"
    Driver "modesetting"
    Option "PrimaryGPU" "true"
EndSection
EOF
fi
# Rootless X from a console autologin
if [ -f /etc/X11/Xwrapper.config ]; then
  sed -i 's/^allowed_users=.*/allowed_users=anybody/' /etc/X11/Xwrapper.config
else
  printf 'allowed_users=anybody\nneeds_root_rights=auto\n' >/etc/X11/Xwrapper.config
fi

BOOTCFG=""
for f in /boot/firmware/config.txt /boot/config.txt; do [ -f "$f" ] && { BOOTCFG="$f"; break; }; done
if [ -n "$BOOTCFG" ]; then
  sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$BOOTCFG"
  if [ "$SET_4K60" = 1 ]; then
    say "Enabling 4K at 60Hz on Pi 4 HDMI ($BOOTCFG)"
    printf '\n%s\n[pi4]\nhdmi_enable_4kp60=1\n[all]\n%s\n' "$MARK_BEGIN" "$MARK_END" >>"$BOOTCFG"
  fi
fi

# --------------------------------------------------------------------------- GPU drivers (PCs)
IS_PI=0
case "$MODEL" in *"Raspberry Pi"*) IS_PI=1 ;; esac
install -d -m 755 /var/log/windopane /etc/windopane

# Let the web UI reboot / shut down (and, on PCs, switch GPU drivers) as root,
# through the two helpers only.
SUDOERS_TMP="$(mktemp)"
{
  echo "# Installed by WindoPane: the web UI may reboot, shut down and switch GPU drivers."
  echo "$TARGET_USER ALL=(root) NOPASSWD: $PREFIX/bin/windopane-power reboot, $PREFIX/bin/windopane-power poweroff"
  if [ "$IS_PI" = 0 ]; then echo "$TARGET_USER ALL=(root) NOPASSWD: $PREFIX/bin/windopane-gpu"; fi
} >"$SUDOERS_TMP"
if visudo -cf "$SUDOERS_TMP" >/dev/null; then
  install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/windopane
else
  warn "could not validate the sudo rule; reboot/shut down from the web UI is disabled"
fi
rm -f "$SUDOERS_TMP"

if [ "$IS_PI" = 0 ]; then
  say "Installing mpv (the PC video player, with VA-API GPU decoding)"
  apt-get install -y -qq --no-install-recommends mpv || warn "mpv failed to install; videos will play with VLC"

  if [ "$GPU" = auto ]; then
    VENDORS_FOUND=""
    for d in /sys/bus/pci/devices/*; do
      case "$(cat "$d/class" 2>/dev/null)" in 0x03*) ;; *) continue ;; esac
      case "$(cat "$d/vendor" 2>/dev/null)" in
        0x1002) VENDORS_FOUND="$VENDORS_FOUND amd" ;;
        0x8086) VENDORS_FOUND="$VENDORS_FOUND intel" ;;
        0x10de) VENDORS_FOUND="$VENDORS_FOUND nvidia" ;;
      esac
    done
    case "$VENDORS_FOUND" in
      *amd*) GPU=amd ;;
      *intel*) GPU=intel ;;
      *nvidia*)
        warn "NVIDIA GPU found. Its proprietary driver isn't installed automatically;"
        warn "re-run with --gpu nvidia or pick NVIDIA under Graphics in the web UI."
        GPU=skip ;;
      *) GPU=skip ;;
    esac
  fi
  if [ "$GPU" != skip ]; then
    say "Installing video drivers: $GPU"
    "$PREFIX/bin/windopane-gpu" run "$GPU" >/dev/null || warn "driver install had problems; see /var/log/windopane/gpu.log"
  fi
fi

# --------------------------------------------------------------------------- yt-dlp updates
cat >/etc/systemd/system/windopane-ytdlp-update.service <<EOF
[Unit]
Description=Update yt-dlp for WindoPane
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$PREFIX/bin/windopane-update-ytdlp
EOF
cat >/etc/systemd/system/windopane-ytdlp-update.timer <<'EOF'
[Unit]
Description=Daily yt-dlp update for WindoPane

[Timer]
OnCalendar=*-*-* 04:20
RandomizedDelaySec=45min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload || warn "systemctl daemon-reload failed"
systemctl enable --now windopane-ytdlp-update.timer >/dev/null || warn "could not enable the yt-dlp update timer"

# --------------------------------------------------------------------------- autostart
PROFILE="$USER_HOME/.profile"
[ -f "$USER_HOME/.bash_profile" ] && PROFILE="$USER_HOME/.bash_profile"
touch "$PROFILE"
sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$PROFILE"
if [ "$AUTOSTART" = 1 ]; then
  say "Enabling boot straight into the wall on HDMI (console autologin on tty1)"
  cat >>"$PROFILE" <<EOF
$MARK_BEGIN
# Start the video wall on the HDMI console. SSH logins are unaffected.
if [ -z "\${DISPLAY:-}" ] && [ "\$(tty)" = /dev/tty1 ] && [ ! -e $STATE/disabled ] && [ -x $PREFIX/bin/windopane-session ]; then
  exec $PREFIX/bin/windopane-session
fi
$MARK_END
EOF
  chown "$TARGET_USER:$TARGET_USER" "$PROFILE"
  install -d /etc/systemd/system/getty@tty1.service.d
  cat >/etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TARGET_USER --noclear %I \$TERM
EOF
  systemctl set-default multi-user.target >/dev/null || true
  # keep PipeWire running for the user even before the first login
  loginctl enable-linger "$TARGET_USER" || true
  systemctl daemon-reload || true
fi

PORT_NOW="$(PYTHONPATH="$PREFIX/app" "$PREFIX/venv/bin/python3" -c \
  "from windopane import config; print(config.load('$STATE/config.json')['web']['port'])")"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Done."
echo "    Web UI:   http://${IP:-$(hostname)}:$PORT_NOW   (or http://$(hostname).local:$PORT_NOW)"
echo "    Control:  windopane-ctl status | restart | stop | start | logs"
if [ "$AUTOSTART" = 1 ]; then
  echo "    Reboot to start the wall:  sudo reboot"
else
  echo "    Autostart is off. Start it from the HDMI console with: $PREFIX/bin/windopane-session"
fi
