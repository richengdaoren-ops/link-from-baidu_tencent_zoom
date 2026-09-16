#!/usr/bin/env python3
"""在服务器上的 headless Chrome 里完成扫码登录（腾讯会议 / 百度网盘）。

流程：打开登录页 → 截图二维码到 ~/.cache/link-dl/<site>_login_qr.png →
Agent 把这张图发给用户扫码 → 脚本轮询 cookie 判定登录成功。
登录态保存在 start_chrome.sh 的固定 profile 里，之后的下载脚本直接复用。

用法：
  python3 login_via_qr.py tencent            # 腾讯会议
  python3 login_via_qr.py baidu              # 百度网盘
  python3 login_via_qr.py tencent --check    # 只检查是否已登录（退出码 0=已登录，1=未登录）
  python3 login_via_qr.py tencent --timeout 300 --refresh 90

截图不是二维码（比如默认是账号密码表单）时，先看截图，再用
  python3 cdp_client.py eval <url子串> "<点击'扫码登录'的 JS>"
切到扫码页，然后 python3 login_via_qr.py <site> --no-open 继续截图/等待。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cdp_client  # noqa: E402
import linux_env  # noqa: E402

SITES = {
    "tencent": {
        "login_url": "https://meeting.tencent.com/login.html",
        "match": "meeting.tencent.com",
        "cookie_urls": ["https://meeting.tencent.com/"],
        "logged_in_cookies": {"token_expire_time"},
        "after_login_url": "https://meeting.tencent.com/user-center/meeting-record",
        "qr_hint_js": "document.querySelector('canvas,img[src*=qr],img[src*=QR],iframe') ? true : false",
    },
    "baidu": {
        "login_url": "https://pan.baidu.com/",
        "match": "baidu.com",
        "cookie_urls": ["https://pan.baidu.com/", "https://passport.baidu.com/", "https://www.baidu.com/"],
        "logged_in_cookies": {"BDUSS"},
        "after_login_url": "https://pan.baidu.com/disk/main",
        "qr_hint_js": "true",
    },
}


def logged_in(session, cfg):
    names = {c["name"] for c in session.cookies(cfg["cookie_urls"])}
    return cfg["logged_in_cookies"] <= names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", choices=sorted(SITES))
    ap.add_argument("--port", type=int, default=linux_env.CDP_PORT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="复用已打开的登录页，不重新打开")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--refresh", type=int, default=90, help="每隔多少秒重新截图（二维码会过期）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = SITES[a.site]

    b = cdp_client.Browser(port=a.port)
    b.require()
    tab = b.find(cfg["match"])
    if not tab:
        tab = b.open("about:blank")
    s = b.session(tab)

    if a.check:
        ok = logged_in(s, cfg)
        print("已登录" if ok else "未登录")
        sys.exit(0 if ok else 1)

    if logged_in(s, cfg):
        print(f"{a.site}: 已登录，无需扫码")
        return

    if not a.no_open:
        s.navigate(cfg["login_url"])
        time.sleep(3)

    linux_env.STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(a.out or linux_env.STATE_DIR / f"{a.site}_login_qr.png")
    s.screenshot(out)
    print(f"QR_SCREENSHOT={out}")
    print("请把这张截图发给用户扫码（截图里若不是二维码，先切换到扫码登录）。等待登录中…", flush=True)

    start = last_shot = time.time()
    while time.time() - start < a.timeout:
        time.sleep(2)
        try:
            if logged_in(s, cfg):
                print(f"{a.site}: 登录成功")
                s.navigate(cfg["after_login_url"])
                return
        except cdp_client.CDPError:
            pass
        if time.time() - last_shot > a.refresh:
            s.screenshot(out)
            last_shot = time.time()
            print(f"QR_SCREENSHOT_REFRESHED={out}", flush=True)
    print(f"{a.site}: {a.timeout}s 内未检测到登录。可重跑本脚本（加 --no-open 保留当前页面）。")
    sys.exit(1)


if __name__ == "__main__":
    main()
