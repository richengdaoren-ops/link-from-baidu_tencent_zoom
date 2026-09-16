#!/usr/bin/env bash
# 在 Ubuntu 服务器上启动一个带远程调试端口的 Chrome，供本 skill 的 CDP 脚本驱动。
#
# 用法：
#   scripts/start_chrome.sh                    # 腾讯会议/百度：headless，端口 9333
#   scripts/start_chrome.sh --headful          # Zoom：非 headless（无显示器时自动套 xvfb-run），端口 9334
#   scripts/start_chrome.sh --proxy http://127.0.0.1:7890   # 走 mihomo 等代理
#   scripts/start_chrome.sh --stop [--headful] # 关掉对应实例
#
# 要点（来自 2026-09 Ubuntu 实战）：
#   - 无 root 的 VM / AppArmor 限制 userns 时 Chrome 沙箱起不来，必须 --no-sandbox。
#     Agent 自带 browser 工具在这种环境会报笼统的 "Chromium browser is missing"，
#     不要去装浏览器，直接用本脚本。
#   - profile 固定在 ~/.cache/link-dl/<name>-profile，扫码登录一次后可复用。
#   - headless 的 UA 带 HeadlessChrome，这里改写成普通 Chrome UA。
set -euo pipefail

STATE_DIR="${LINK_DL_STATE_DIR:-$HOME/.cache/link-dl}"
HEADFUL=0
PORT=""
PROXY="${LINK_DL_CHROME_PROXY:-}"
PROFILE=""
STOP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --headful) HEADFUL=1 ;;
    --port) PORT="$2"; shift ;;
    --proxy) PROXY="$2"; shift ;;
    --profile) PROFILE="$2"; shift ;;
    --stop) STOP=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ $HEADFUL == 1 ]]; then
  NAME=zoom; PORT="${PORT:-${LINK_DL_ZOOM_CDP_PORT:-9334}}"
else
  NAME=main; PORT="${PORT:-${LINK_DL_CDP_PORT:-9333}}"
fi
PROFILE="${PROFILE:-$STATE_DIR/$NAME-profile}"
LOG="$STATE_DIR/chrome-$NAME.log"
PIDFILE="$STATE_DIR/chrome-$NAME.pid"
mkdir -p "$STATE_DIR" "$PROFILE"
chmod 700 "$STATE_DIR" "$PROFILE" 2>/dev/null || true

probe() { curl -s --noproxy '*' -m 2 "http://127.0.0.1:$PORT/json/version" 2>/dev/null; }

if [[ $STOP == 1 ]]; then
  [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null
  rm -f "$PIDFILE"
  # xvfb-run 包装时 Chrome 是子进程，按端口兜底清理
  if pkill -f -- "--remote-debugging-port=$PORT" 2>/dev/null; then sleep 1; fi
  [[ -z "$(probe)" ]] && echo "端口 $PORT 上的 $NAME Chrome 已停止" || echo "端口 $PORT 仍有 Chrome 在运行，请手动检查"
  exit 0
fi

if probe >/dev/null && [[ -n "$(probe)" ]]; then
  echo "Chrome 已在 127.0.0.1:$PORT 运行："
  probe | python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ", d.get("Browser"))'
  exit 0
fi

BIN="${CHROME_BIN:-}"
if [[ -z "$BIN" ]]; then
  for c in google-chrome-stable google-chrome chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then BIN="$(command -v "$c")"; break; fi
  done
fi
if [[ -z "$BIN" ]]; then
  cat >&2 <<'MSG'
找不到 Chrome/Chromium。推荐安装 Google Chrome（deb，非 snap）：
  curl -fLo /tmp/chrome.deb ${HTTPS_PROXY:+-x $HTTPS_PROXY} https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y /tmp/chrome.deb
或者设置 CHROME_BIN=/path/to/chrome
MSG
  exit 1
fi

VER="$("$BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
MAJOR="${VER%%.*}"; MAJOR="${MAJOR:-140}"
UA="${LINK_DL_UA:-Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${MAJOR}.0.0.0 Safari/537.36}"

ARGS=(
  "--remote-debugging-port=$PORT"
  "--remote-debugging-address=127.0.0.1"
  "--remote-allow-origins=*"
  "--user-data-dir=$PROFILE"
  "--no-sandbox"
  "--disable-dev-shm-usage"
  "--no-first-run" "--no-default-browser-check"
  "--disable-gpu"
  "--window-size=1440,900"
  "--lang=zh-CN"
  "--user-agent=$UA"
)
[[ -n "$PROXY" ]] && ARGS+=("--proxy-server=$PROXY" "--proxy-bypass-list=127.0.0.1;localhost")

LAUNCH=("$BIN")
if [[ $HEADFUL == 0 ]]; then
  ARGS+=("--headless=new")
elif [[ -z "${DISPLAY:-}" ]]; then
  if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "Zoom 需要非 headless Chrome，但没有显示器也没有 xvfb-run。安装：sudo apt-get install -y xvfb" >&2
    exit 1
  fi
  LAUNCH=(xvfb-run -a -s "-screen 0 1440x900x24" "$BIN")
fi

nohup "${LAUNCH[@]}" "${ARGS[@]}" about:blank >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 40); do
  if [[ -n "$(probe)" ]]; then
    echo "Chrome 已启动：127.0.0.1:$PORT  ($NAME, $([[ $HEADFUL == 1 ]] && echo headful || echo headless))"
    echo "  binary : $BIN $VER"
    echo "  profile: $PROFILE"
    [[ -n "$PROXY" ]] && echo "  proxy  : $PROXY"
    exit 0
  fi
  sleep 0.5
done
echo "Chrome 20 秒内没有起来，日志末尾：" >&2
tail -n 30 "$LOG" >&2
exit 1
