#!/usr/bin/env bash
# Install the WindoPane OLED ticker (SSD1306 128x64 on I2C, e.g. the OLED in
# Freenove's Raspberry Pi 5 cases). Safe to re-run to change options.
#
#   sudo ./install-oled.sh [--rotate auto|0|180] [--contrast 0-255]
#                          [--sleep HH:MM-HH:MM] [--user NAME]
#   sudo ./install-oled.sh --uninstall
#
# If Freenove's case software is installed, its OLED screen is switched off
# (so the two don't fight over the display); its fan and LED control keep
# running untouched. --uninstall gives the OLED back to Freenove.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN=/opt/windopane/bin/windopane-oled
UNIT=/etc/systemd/system/windopane-oled.service
STATE_DIR=/etc/windopane
FREENOVE_NOTE=$STATE_DIR/oled-freenove-config
TARGET_USER="${SUDO_USER:-}"
ROTATE=auto
CONTRAST=96
SLEEP=""
UNINSTALL=0

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --rotate) ROTATE="${2:?}"; shift 2 ;;
    --contrast) CONTRAST="${2:?}"; shift 2 ;;
    --sleep) SLEEP="${2:?}"; shift 2 ;;
    --user) TARGET_USER="${2:?}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run with sudo: sudo $0"
case "$ROTATE" in auto|0|180) ;; *) die "--rotate must be auto, 0 or 180" ;; esac
[[ "$CONTRAST" =~ ^[0-9]+$ ]] && [ "$CONTRAST" -le 255 ] || die "--contrast must be 0-255"
if [ -n "$SLEEP" ] && ! [[ "$SLEEP" =~ ^[0-9]{1,2}:[0-9]{2}-[0-9]{1,2}:[0-9]{2}$ ]]; then
  die "--sleep must look like 23:00-07:00"
fi

# Freenove's case app keeps its settings in app_config.json next to its code.
freenove_config() {
  local unit=/etc/systemd/system/my_app_running.service dir
  [ -f "$unit" ] || return 1
  dir="$(sed -n 's/^ExecStart=.* \(\/[^ ]*\)\/task_manager\.py.*/\1/p' "$unit" | head -1)"
  [ -n "$dir" ] && [ -f "$dir/app_config.json" ] || return 1
  echo "$dir/app_config.json"
}

set_freenove_oled() {  # $1 = config path, $2 = true|false
  python3 - "$1" "$2" <<'PY'
import json, sys
path, value = sys.argv[1], sys.argv[2] == "true"
with open(path) as fh:
    cfg = json.load(fh)
section = cfg.setdefault("OLED", {})
old = section.get("is_run_on_startup", True)
section["is_run_on_startup"] = value
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=4)
print("on" if old else "off")
PY
}

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  systemctl disable --now windopane-oled.service 2>/dev/null || true
  rm -f "$UNIT" "$BIN"
  systemctl daemon-reload || true
  if [ -f "$FREENOVE_NOTE" ]; then
    cfg="$(cat "$FREENOVE_NOTE")"
    if [ -f "$cfg" ]; then
      set_freenove_oled "$cfg" true >/dev/null
      say "Gave the OLED back to Freenove's case software (takes effect at next boot)."
    fi
    rm -f "$FREENOVE_NOTE"
  fi
  say "OLED ticker removed."
  exit 0
fi

# ---------------------------------------------------------------- checks
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
  TARGET_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
fi
id "$TARGET_USER" >/dev/null 2>&1 || die "no user to run as; pass --user NAME"
[ -d /opt/windopane ] || die "WindoPane isn't installed here (no /opt/windopane); run its install.sh first"
MODEL="$( { tr -d '\0' </proc/device-tree/model; } 2>/dev/null || echo unknown)"
case "$MODEL" in
  *"Raspberry Pi"*) ;;
  *) warn "not a Raspberry Pi ($MODEL); continuing, the OLED must be on I2C bus 1" ;;
esac

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PKGS=(python3-pil i2c-tools fonts-dejavu-core)
if apt-cache show python3-smbus2 >/dev/null 2>&1; then PKGS+=(python3-smbus2); else PKGS+=(python3-smbus); fi
apt-get install -y -qq --no-install-recommends "${PKGS[@]}"

# ---------------------------------------------------------------- I2C
NEED_REBOOT=0
if [ ! -e /dev/i2c-1 ]; then
  say "Enabling I2C"
  if command -v raspi-config >/dev/null; then
    raspi-config nonint do_i2c 0
  else
    BOOTCFG=""
    for f in /boot/firmware/config.txt /boot/config.txt; do [ -f "$f" ] && { BOOTCFG="$f"; break; }; done
    if [ -n "$BOOTCFG" ] && ! grep -q '^dtparam=i2c_arm=on' "$BOOTCFG"; then
      echo "dtparam=i2c_arm=on" >>"$BOOTCFG"
    fi
    echo i2c-dev >/etc/modules-load.d/windopane-i2c.conf
  fi
  modprobe i2c-dev 2>/dev/null || true
  [ -e /dev/i2c-1 ] || NEED_REBOOT=1
fi
getent group i2c >/dev/null || groupadd -r i2c
usermod -aG i2c "$TARGET_USER"

# ---------------------------------------------------------------- Freenove
if cfg="$(freenove_config)"; then
  [ -f "$cfg.windopane-bak" ] || cp -p "$cfg" "$cfg.windopane-bak"
  previous="$(set_freenove_oled "$cfg" false)"
  if [ "$previous" = on ]; then
    echo "$cfg" >"$FREENOVE_NOTE"
    say "Freenove case software found: switched off its OLED screen only (fans and LEDs unchanged)"
  fi
  pkill -f "task_oled\.py" 2>/dev/null && sleep 1 || true
elif pgrep -f "task_oled\.py" >/dev/null; then
  warn "Freenove's OLED program is running but its settings weren't found; stopping it for now."
  warn "If it comes back after a reboot, turn its OLED off in the Freenove app."
  pkill -f "task_oled\.py" || true
fi

if [ "$NEED_REBOOT" = 0 ]; then
  if i2cdetect -y 1 0x3c 0x3c 2>/dev/null | grep -qi '3c'; then
    say "OLED found at 0x3C on I2C bus 1"
  else
    warn "nothing answered at 0x3C on I2C bus 1. Check the OLED cable; installing anyway."
  fi
fi

# ---------------------------------------------------------------- service
say "Installing the ticker"
install -d -m 755 /opt/windopane/bin "$STATE_DIR"
install -m 755 "$SRC/windopane-oled.py" "$BIN"
ARGS="--rotate $ROTATE --contrast $CONTRAST"
[ -n "$SLEEP" ] && ARGS="$ARGS --sleep $SLEEP"
cat >"$UNIT" <<EOF
[Unit]
Description=WindoPane OLED ticker
After=network.target

[Service]
Type=simple
User=$TARGET_USER
SupplementaryGroups=i2c
ExecStart=/usr/bin/python3 $BIN $ARGS
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload || warn "systemctl daemon-reload failed"
systemctl enable windopane-oled.service >/dev/null || warn "could not enable the service"

if [ "$NEED_REBOOT" = 1 ]; then
  say "I2C was just enabled: reboot to start the OLED ticker (sudo reboot)."
else
  systemctl restart windopane-oled.service
  sleep 3
  if systemctl is-active --quiet windopane-oled.service; then
    say "OLED ticker running. Logs: journalctl -u windopane-oled -f"
  else
    warn "the ticker didn't start; see: journalctl -u windopane-oled -n 30"
  fi
fi
