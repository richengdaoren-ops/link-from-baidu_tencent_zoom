#!/usr/bin/env bash
# 环境自检：跑任何下载前先执行一次。只读，不安装任何东西。
#   scripts/check_env.sh
set -uo pipefail
ok(){ printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
bad(){ printf '  \033[31m✘\033[0m %s\n' "$*"; }
ROOT="${MEETING_DOWNLOAD_ROOT:-$HOME/Downloads/会议录制}"
PORT="${LINK_DL_CDP_PORT:-9333}"
ZPORT="${LINK_DL_ZOOM_CDP_PORT:-9334}"

echo "== 基础 =="
. /etc/os-release 2>/dev/null && ok "系统: $PRETTY_NAME"
command -v python3 >/dev/null && ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)" || bad "缺 python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' || bad "python3 需 ≥ 3.8"
command -v curl >/dev/null && ok "curl" || bad "缺 curl（sudo apt-get install -y curl）"
python3 -c 'import openpyxl' 2>/dev/null && ok "openpyxl（录制目录.xlsx）" || warn "无 openpyxl：批量下载不导出 xlsx（pip install --user openpyxl）"
python3 -c 'import requests' 2>/dev/null && ok "requests（旧 Zoom API 脚本）" || warn "无 requests：仅旧 Zoom API 脚本需要"

echo "== 浏览器 =="
BIN="${CHROME_BIN:-}"
for c in google-chrome-stable google-chrome chromium chromium-browser; do
  [[ -z "$BIN" ]] && command -v "$c" >/dev/null && BIN="$(command -v "$c")"
done
if [[ -n "$BIN" ]]; then
  ok "Chrome: $BIN ($("$BIN" --version 2>/dev/null))"
  [[ "$BIN" == /snap/* ]] && warn "snap 版 Chromium 对 profile 路径有限制，建议装 Google Chrome deb"
else
  bad "没有 Chrome/Chromium（见 start_chrome.sh 里的安装提示）"
fi
probe(){ curl -s --noproxy '*' -m 2 "http://127.0.0.1:$1/json/version" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Browser"])' 2>/dev/null; }
v="$(probe "$PORT")" && [[ -n "$v" ]] && ok "headless Chrome 在 $PORT 运行（$v）" || warn "端口 $PORT 没有 Chrome → scripts/start_chrome.sh"
v="$(probe "$ZPORT")" && [[ -n "$v" ]] && ok "Zoom 用 headful Chrome 在 $ZPORT 运行（$v）" || warn "端口 $ZPORT 没有 Chrome（只有 Zoom 需要 → scripts/start_chrome.sh --headful）"
if [[ -n "${DISPLAY:-}" ]]; then ok "DISPLAY=$DISPLAY"; elif command -v xvfb-run >/dev/null; then ok "xvfb-run（Zoom 非 headless 用）"; else warn "无 DISPLAY 也无 xvfb-run：Zoom 下载不可用（sudo apt-get install -y xvfb）"; fi

echo "== Zoom / 媒体工具 =="
if command -v node >/dev/null; then
  NV="$(node -v)"; M="${NV#v}"; M="${M%%.*}"
  [[ "$M" -ge 22 ]] && ok "node $NV" || warn "node $NV < 22：Zoom 浏览器脚本需要 Node ≥ 22"
else warn "无 node：Zoom 浏览器脚本不可用"; fi
command -v yt-dlp >/dev/null && ok "yt-dlp $(yt-dlp --version)" || warn "无 yt-dlp：Zoom 视频需要（python3 -m pip install --user -U --pre yt-dlp）"
command -v ffprobe >/dev/null && ok "ffprobe（下载校验）" || warn "无 ffprobe：verify_meeting_dir.py 跳过时长校验（sudo apt-get install -y ffmpeg）"

echo "== 网络 / 代理 =="
P="${HTTPS_PROXY:-${https_proxy:-}}"
if [[ -n "$P" ]]; then ok "HTTPS_PROXY=$P"; else warn "未设 HTTPS_PROXY；若装了 mihomo，大文件建议 export HTTPS_PROXY=http://127.0.0.1:7890"; fi
for u in https://meeting.tencent.com https://pan.baidu.com https://zoom.us; do
  code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$u")"
  [[ "$code" =~ ^[23] ]] && ok "$u → $code" || warn "$u → ${code:-超时}"
done

echo "== 存储 =="
mkdir -p "$ROOT" 2>/dev/null && ok "下载根目录: $ROOT" || bad "无法创建 $ROOT"
avail="$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2{printf "%.1f", $4/1048576}')"
[[ -n "$avail" ]] && { awk "BEGIN{exit !($avail<10)}" && warn "剩余 ${avail} GB（< 10 GB）" || ok "剩余 ${avail} GB"; }
SUM="${LINK_SUMMARY_TABLE:-$HOME/Documents/链接汇总/【长期关注】百度、腾讯、ZOOM链接汇总.md}"
[[ -f "$SUM" ]] && ok "汇总表: $SUM" || warn "汇总表不存在，首次写入时创建: $SUM"
