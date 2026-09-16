#!/usr/bin/env python3
"""Ubuntu/Linux 运行环境的集中配置（路径、CDP 端口、UA、代理）。

所有值都可以用环境变量覆盖，脚本里不再硬编码 /Users/gaosheng/...：

    MEETING_DOWNLOAD_ROOT   录像根目录，默认 ~/Downloads/会议录制
    LINK_SUMMARY_TABLE      链接汇总表，默认 ~/Documents/链接汇总/【长期关注】百度、腾讯、ZOOM链接汇总.md
    LINK_DL_CDP_PORT        腾讯会议/百度用的 headless Chrome 调试端口，默认 9333
    LINK_DL_ZOOM_CDP_PORT   Zoom 用的非 headless（Xvfb）Chrome 调试端口，默认 9334
    LINK_DL_STATE_DIR       Chrome profile / 日志 / 二维码截图目录，默认 ~/.cache/link-dl
    LINK_DL_TZ_OFFSET       目录名里的时间按哪个时区（小时偏移），默认 8（北京时间）；服务器本身可以是 UTC
    LINK_DL_UA              下载时的 User-Agent（默认运行时从浏览器读取，读不到用下面的兜底值）
    HTTPS_PROXY / https_proxy  urllib 下载会自动走代理（例如 mihomo http://127.0.0.1:7890）
"""
import os
from pathlib import Path

HOME = Path.home()

DOWNLOAD_ROOT = Path(os.environ.get("MEETING_DOWNLOAD_ROOT", HOME / "Downloads" / "会议录制")).expanduser()
SUMMARY_TABLE = Path(
    os.environ.get(
        "LINK_SUMMARY_TABLE",
        HOME / "Documents" / "链接汇总" / "【长期关注】百度、腾讯、ZOOM链接汇总.md",
    )
).expanduser()
STATE_DIR = Path(os.environ.get("LINK_DL_STATE_DIR", HOME / ".cache" / "link-dl")).expanduser()

CDP_HOST = os.environ.get("LINK_DL_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("LINK_DL_CDP_PORT", "9333"))
ZOOM_CDP_PORT = int(os.environ.get("LINK_DL_ZOOM_CDP_PORT", "9334"))

FALLBACK_UA = os.environ.get(
    "LINK_DL_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)


def clean_ua(ua):
    """headless Chrome 的 UA 带 HeadlessChrome，下载时换成普通 Chrome。"""
    ua = str(ua or "").strip() or FALLBACK_UA
    return ua.replace("HeadlessChrome/", "Chrome/")
