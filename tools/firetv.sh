#!/usr/bin/env bash
# Helper CLI for managing the Fire TV Stick 4K (192.168.1.94:5555) and Stream (tv.stream.app).
# Usage:
#   tools/firetv.sh status              # Show RAM, swap, storage, and Stream status
#   tools/firetv.sh boost               # Free RAM by stopping idle background hogs
#   tools/firetv.sh launch              # Wake stick and open Stream
#   tools/firetv.sh restart             # Restart Stream (tv.stream.app)
#   tools/firetv.sh type "text"         # Type text into focused Fire TV input/search box
#   tools/firetv.sh shot [out.png]      # Capture a screenshot from Fire TV
#   tools/firetv.sh install-smarttube   # Download and sideload latest SmartTube Stable

set -euo pipefail

TARGET="${FIRETV_TARGET:-192.168.1.94:5555}"
[[ "$TARGET" == *:* ]] || TARGET="$TARGET:5555"

ADB=$(command -v adb || ls /opt/homebrew/bin/adb /usr/local/bin/adb "$HOME/Library/Android/sdk/platform-tools/adb" 2>/dev/null | head -1)
if [[ -z "${ADB:-}" || ! -x "$ADB" ]]; then
  echo "adb not found. Install with: brew install android-platform-tools" >&2
  exit 1
fi

connect() {
  if ! "$ADB" devices | grep -q "^${TARGET}[[:space:]]*device"; then
    "$ADB" connect "$TARGET" >/dev/null
    sleep 1
  fi
  local state
  state=$("$ADB" -s "$TARGET" get-state 2>/dev/null || true)
  if [[ "$state" != "device" ]]; then
    echo "Fire TV ($TARGET) not ready (state: ${state:-disconnected})." >&2
    exit 1
  fi
}

A() { "$ADB" -s "$TARGET" "$@"; }

cmd="${1:-help}"
shift || true

case "$cmd" in
  status)
    connect
    A shell '
      echo "Model: $(getprop ro.product.model) (Android $(getprop ro.build.version.release))"
      echo "--- Memory ---"
      free -h
      echo "--- Storage ---"
      df -h /data | tail -n 1
      echo "--- Animations ---"
      echo "window=$(settings get global window_animation_scale) transition=$(settings get global transition_animation_scale) animator=$(settings get global animator_duration_scale)"
      echo "--- Stream ---"
      pid=$(pidof tv.stream.app 2>/dev/null || true)
      if [ -n "$pid" ]; then echo "tv.stream.app running (PID $pid)"; else echo "tv.stream.app not running"; fi
    '
    ;;
  boost)
    connect
    A shell '
      for pkg in com.amazon.firebat com.amazon.tv.livetv com.netflix.ninja com.amazon.firetv.youtube com.amazon.wirelessmetrics.service com.amazon.adep com.amazon.ceviche com.amazon.diode com.amazon.venezia; do
        am force-stop "$pkg" 2>/dev/null || true
      done
      free -h
    '
    ;;
  launch)
    connect
    A shell input keyevent KEYCODE_WAKEUP
    A shell am start -n tv.stream.app/.Splash >/dev/null
    echo "Launched Stream on $TARGET"
    ;;
  restart)
    connect
    A shell am force-stop tv.stream.app
    sleep 1
    A shell am start -n tv.stream.app/.Splash >/dev/null
    echo "Restarted Stream on $TARGET"
    ;;
  type)
    if [[ $# -lt 1 ]]; then
      echo "Usage: $0 type \"search text\"" >&2
      exit 1
    fi
    connect
    # Android `input text` treats %s as space
    encoded="${*// /%s}"
    A shell input text "$encoded"
    echo "Sent text to $TARGET: $*"
    ;;
  shot)
    connect
    out="${1:-/tmp/firetv-$(date +%Y%m%d-%H%M%S).png}"
    A exec-out screencap -p > "$out"
    echo "Saved screenshot to $out"
    ;;
  install-smarttube)
    connect
    tmp_apk="/tmp/smarttube-stable.apk"
    echo "Downloading latest SmartTube Stable APK..."
    curl -fsSL -L "https://github.com/yuliskov/SmartTube/releases/latest/download/SmartTube_stable.apk" -o "$tmp_apk" \
      || {
        # Fallback: query GitHub API for latest stable armv7a/universal APK
        url=$(curl -fsSL "https://api.github.com/repos/yuliskov/SmartTube/releases/latest" \
          | python3 -c 'import sys,json; assets=json.load(sys.stdin).get("assets",[]); print(next((a["browser_download_url"] for a in assets if a["name"].endswith(".apk") and "armeabi-v7a" in a["name"]), next((a["browser_download_url"] for a in assets if a["name"].endswith(".apk")), "")))')
        [[ -n "$url" ]] || { echo "Could not find SmartTube APK asset" >&2; exit 1; }
        curl -fsSL -L "$url" -o "$tmp_apk"
      }
    echo "Installing SmartTube on $TARGET..."
    A install -r "$tmp_apk"
    rm -f "$tmp_apk"
    echo "SmartTube installed."
    ;;
  *)
    sed -n '2,10p' "$0"
    ;;
esac
