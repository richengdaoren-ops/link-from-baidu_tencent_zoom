#!/usr/bin/env python3
"""多账号登录态管理器。

每个账号 = 独立 Chrome profile 目录 + 独立 CDP 端口 + 账号元数据文件。
支持账号命名、登录态过期检测、一键切换。

用法：
  python3 account_manager.py add 卡卡西 --site tencent --port 9340
  python3 account_manager.py list
  python3 account_manager.py check 卡卡西
  python3 account_manager.py login 卡卡西
  python3 account_manager.py use 卡卡西
  python3 account_manager.py remove 卡卡西
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cdp_client
import linux_env

STATE_DIR = Path(linux_env.STATE_DIR)
ACCOUNTS_FILE = STATE_DIR / "accounts.json"

# 端口分配规则：9340 起，每个账号 +1
BASE_PORT = 9340


def load_accounts():
    if not ACCOUNTS_FILE.exists():
        return {}
    return json.loads(ACCOUNTS_FILE.read_text())


def save_accounts(accounts):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2, ensure_ascii=False))
    ACCOUNTS_FILE.chmod(0o600)


def get_account(name):
    accounts = load_accounts()
    if name not in accounts:
        print(f"账号 '{name}' 不存在。可用账号：{', '.join(sorted(accounts)) or '无'}")
        sys.exit(1)
    return accounts[name]


def find_free_port():
    accounts = load_accounts()
    used = {a["port"] for a in accounts.values()}
    port = BASE_PORT
    while port in used:
        port += 1
    return port


def cmd_add(args):
    accounts = load_accounts()
    if args.name in accounts:
        print(f"账号 '{args.name}' 已存在，端口 {accounts[args.name]['port']}")
        return

    port = args.port or find_free_port()
    profile = STATE_DIR / f"{args.name}-profile"

    accounts[args.name] = {
        "name": args.name,
        "site": args.site,
        "port": port,
        "profile": str(profile),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_login_at": None,
        "expires_at": None,
        "notes": args.notes or "",
    }
    save_accounts(accounts)
    print(f"账号 '{args.name}' 已创建：")
    print(f"  端口   : {port}")
    print(f"  profile: {profile}")
    print(f"  网站   : {args.site}")
    print(f"")
    print(f"下一步：启动 Chrome 并扫码登录：")
    print(f"  bash scripts/start_chrome.sh --port {port} --profile {profile}")
    print(f"  python3 scripts/login_via_qr.py {args.site} --port {port}")


def cmd_list(args):
    accounts = load_accounts()
    if not accounts:
        print("暂无账号。用 'add' 创建：")
        print(f"  python3 {sys.argv[0]} add <账号名> --site tencent")
        return

    print(f"{'账号':<12} {'网站':<10} {'端口':<6} {'登录状态':<10} {'创建时间':<20} {'备注'}")
    print("-" * 80)
    for name, acc in sorted(accounts.items()):
        status = check_login_status(acc)
        created = acc.get("created_at", "")[:19]
        notes = acc.get("notes", "")
        print(f"{name:<12} {acc['site']:<10} {acc['port']:<6} {status:<10} {created:<20} {notes}")


def check_login_status(acc):
    """返回 '已登录' / '未登录' / 'Chrome未启动' / '已过期'"""
    try:
        b = cdp_client.Browser(port=acc["port"])
        # 检查 Chrome 是否运行
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{acc['port']}/json/version", timeout=2)
        except Exception:
            return "Chrome未启动"
        tab = b.find(acc["site"] if acc["site"] != "tencent" else "meeting.tencent.com")
        if not tab:
            return "未登录"
        s = b.session(tab)
        from login_via_qr import SITES
        cfg = SITES.get(acc["site"], SITES["tencent"])
        if logged_in(s, cfg):
            return "已登录"
        return "未登录"
    except Exception:
        return "未知"


def logged_in(session, cfg):
    names = {c["name"] for c in session.cookies(cfg["cookie_urls"])}
    return cfg["logged_in_cookies"] <= names


def cmd_check(args):
    acc = get_account(args.name)
    status = check_login_status(acc)
    print(f"账号 '{args.name}' 状态：{status}")
    if status == "已登录":
        acc["last_login_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_accounts(load_accounts() | {args.name: acc})
    sys.exit(0 if status == "已登录" else 1)


def cmd_login(args):
    acc = get_account(args.name)

    # 检查是否已有 Chrome 在运行
    import urllib.request
    running = None
    for port_candidate in [acc["port"], 9333, 9334]:
        try:
            info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port_candidate}/json/version", timeout=2).read())
            running = {"port": port_candidate, "browser": info.get("Browser", "?")}
            break
        except Exception:
            continue

    if running:
        port = running["port"]
        print(f"检测到端口 {port} 已有 Chrome 运行（{running['browser']}）")

        # 检查该端口是否已有账号绑定
        accounts = load_accounts()
        existing_account = None
        for n, a in accounts.items():
            if a["port"] == port:
                existing_account = n
                break

        if existing_account:
            print(f"此端口已绑定账号 '{existing_account}'。重新绑定到 '{args.name}' 会抢占端口。")
            acc["port"] = port
            save_accounts(load_accounts() | {args.name: acc})
            print(f"已重新绑定账号 '{args.name}' 到端口 {port}（原账号 '{existing_account}' 的端口将被占用）")
            port = port
        else:
            print(f"端口 {port} 未绑定任何账号，正在绑定到 '{args.name}'...")
            acc["port"] = port
            aw = load_accounts()
            aw[args.name] = acc
            save_accounts(aw)

        # 检查该端口的登录态
        b = cdp_client.Browser(port=port)
        from login_via_qr import SITES
        cfg = SITES.get(args.site, SITES["tencent"])
        tab = b.find(cfg["match"])
        if not tab:
            tab = b.open("about:blank")
        s = b.session(tab)
        if logged_in(s, cfg):
            print(f"账号 '{args.name}' 在该端口已登录")
            acc["last_login_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_accounts(load_accounts() | {args.name: acc})
            return
        else:
            print(f"该端口 Chrome 已运行但未登录，正在打开登录页...")
            s.navigate(cfg["login_url"])
            time.sleep(3)
    else:
        print(f"启动账号 '{args.name}' 的 Chrome（端口 {acc['port']}）...")
        rc = os.system(f"bash {Path(__file__).parent}/start_chrome.sh --port {acc['port']} --profile {acc['profile']} >/dev/null 2>&1")
        if rc != 0:
            print(f"Chrome 启动失败（退出码 {rc}）。请先安装 Chrome。")
            sys.exit(1)
        time.sleep(3)

    # 扫码登录
    rc = os.system(f"python3 {Path(__file__).parent}/login_via_qr.py {acc['site']} --port {acc['port']} 2>&1")
    if rc == 0:
        acc = get_account(args.name)  # re-read to get latest data
        acc["last_login_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        aw = load_accounts()
        aw[args.name] = acc
        save_accounts(aw)
        print(f"登录成功，账号 '{args.name}' 已就绪")
    else:
        print(f"登录失败或超时，可重跑 python3 scripts/account_manager.py login {args.name}")


def cmd_use(args):
    acc = get_account(args.name)
    print(f"切换到账号 '{args.name}'（端口 {acc['port']}）")

    # 导出环境变量
    env_lines = [
        f"export LINK_DL_CDP_PORT={acc['port']}",
        f"export LINK_DL_STATE_DIR={STATE_DIR}",
    ]
    env_file = STATE_DIR / "current_account.env"
    env_file.write_text("\n".join(env_lines) + "\n")
    env_file.chmod(0o600)

    print(f"环境变量已写入 {env_file}")
    print(f"")
    print(f"使用方式：")
    print(f"  source {env_file}")
    print(f"  python3 scripts/download_share_recordings_via_cdp.py --with-minutes '...'")
    print(f"")
    print(f"或者直接指定端口：")
    print(f"  python3 scripts/download_share_recordings_via_cdp.py --cdp-port {acc['port']} --with-minutes '...'")


def cmd_remove(args):
    accounts = load_accounts()
    if args.name not in accounts:
        print(f"账号 '{args.name}' 不存在")
        return
    acc = accounts.pop(args.name)
    save_accounts(accounts)

    # 停止 Chrome
    os.system(f"bash {Path(__file__).parent}/start_chrome.sh --stop --port {acc['port']} 2>/dev/null || true")

    # 删除 profile（可选）
    if args.purge:
        import shutil
        profile = Path(acc["profile"])
        if profile.exists():
            shutil.rmtree(profile)
            print(f"已删除 profile: {profile}")

    print(f"账号 '{args.name}' 已移除")


def main():
    ap = argparse.ArgumentParser(description="多账号登录态管理")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("add", help="创建新账号")
    p.add_argument("name", help="账号名（如：卡卡西）")
    p.add_argument("--site", default="tencent", choices=["tencent", "baidu"])
    p.add_argument("--port", type=int, help="CDP 端口（默认自动分配）")
    p.add_argument("--notes", help="备注")

    p = sub.add_parser("list", help="列出所有账号")
    p = sub.add_parser("check", help="检查账号登录状态")
    p.add_argument("name")

    p = sub.add_parser("login", help="启动 Chrome 并扫码登录")
    p.add_argument("name")

    p = sub.add_parser("use", help="切换到指定账号")
    p.add_argument("name")

    p = sub.add_parser("remove", help="移除账号")
    p.add_argument("name")
    p.add_argument("--purge", action="store_true", help="同时删除 profile 目录")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    {"add": cmd_add, "list": cmd_list, "check": cmd_check,
     "login": cmd_login, "use": cmd_use, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    main()
