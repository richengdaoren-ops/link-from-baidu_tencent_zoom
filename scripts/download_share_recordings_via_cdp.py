#!/usr/bin/env python3
"""Tencent Meeting share-link (/crm/ /cw/) recording downloader via CDP（Linux 版）.

Unlike download_share_recordings_from_har.py (external requests with HAR
cookies), this script authenticates by issuing XHRs inside the user's
logged-in Chrome tab — the only reliable auth path per site experience.
Each file is signed and downloaded immediately to avoid signed-URL expiry.

Usage:
    python3 download_share_recordings_via_cdp.py 'https://meeting.tencent.com/cw/XXXX' ...
    python3 download_share_recordings_via_cdp.py --dry-run 'https://...'
    python3 download_share_recordings_via_cdp.py --with-minutes 'https://...'   # 同时导出 AI 纪要 + 逐字稿

Prerequisites (Linux):
    - scripts/start_chrome.sh 已启动 headless Chrome（127.0.0.1:9333）
    - 该 profile 已扫码登录腾讯会议（scripts/login_via_qr.py tencent）
    - 旧 macOS 代理模式：--cdp-mode proxy --cdp-port 3456

2026-09 Ubuntu 实战要点：
    - get-multi-record-file / sign-multi-record-file 的 auth_share_id 必须是分享页 UUID
      （long_url 里的 id=...），传 encode_uni_record_id 会报 2710500。
    - COS 签名 URL（ylz.cos.meeting.tencent.com）外部下载必须带：腾讯会议 cookie（含
      HttpOnly，用 Network.getCookies 取）+ Range: bytes=0- + /cw/<code> Referer + 浏览器 UA，
      否则 403；带齐后返回 206。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import download_my_recordings as base
from recording_naming import find_existing_complete_path, media_filename, normalize_resource_type

BASE_URL = "https://meeting.tencent.com"
PUBLIC_API = f"{base.API_BASE}/meetlog/public"


LONG_URL_PATTERNS = [
    r'\\"long_url\\"\s*:\s*\\"([^"]+?)\\"',
    r'"long_url"\s*:\s*"([^"]+)"',
]


def navigate_and_wait(target_id, url, timeout=30):
    base.cdp_call("GET", f"/navigate?target={target_id}&url={url}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        found = base.cdp_eval(
            target_id, 'document.documentElement.outerHTML.indexOf("long_url") > -1'
        )
        if found:
            return
    raise RuntimeError(f"page did not expose long_url within {timeout}s: {url}")


def parse_long_url_query(long_url):
    # long_url may be a bare query string ("id=...&is-single=false"), not a full URL
    long_url = json.loads(f'"{long_url}"') if "\\u" in long_url else long_url
    long_url = long_url.replace("\\u0026", "&")
    parsed = urlparse(long_url)
    query_text = parsed.query or long_url.lstrip("?")
    return dict(parse_qsl(query_text, keep_blank_values=True))


def resolve_share(target_id, code):
    navigate_and_wait(target_id, f"{BASE_URL}/cw/{code}")
    html_text = base.cdp_eval(target_id, "document.documentElement.outerHTML") or ""
    query = {}
    for pattern in LONG_URL_PATTERNS:
        match = re.search(pattern, html_text)
        if match:
            query = parse_long_url_query(match.group(1))
            if query.get("id") or query.get("sharing_id"):
                break
    share_id = query.get("id") or query.get("sharing_id")
    if not share_id:
        href = base.cdp_eval(target_id, "document.location.href") or ""
        query = dict(parse_qsl(urlparse(href).query, keep_blank_values=True))
        share_id = query.get("id") or query.get("sharing_id")
    if not share_id:
        raise RuntimeError(f"{code}: could not resolve share id")
    return {
        "short_code": code,
        "share_id": share_id,
        "is_single": str(query.get("is-single", "")).lower() == "true",
    }


def fetch_common_record_info(target_id, share):
    body = {
        "pk_meeting_info_id": "",
        "sharing_id": share["share_id"],
        "is_single": share["is_single"],
        "cover_image_style": "meetlog_detail_webp_1000",
        "ticket": "",
        "randstr": "",
        "pwd": "",
        "activity_uid": "",
        "lang": "zh",
        "is_origin_content": True,
        "is_cve": True,
        "forward_cgi_path": "shares",
        "enter_from": "share",
        "short_url_code": share["short_code"],
        "is_short_ctw": False,
    }
    data = base.browser_xhr(target_id, "POST", f"{PUBLIC_API}/detail/common-record-info", body)
    return data.get("data", {})


def fetch_share_files(target_id, share_id, record_id):
    path = f"{PUBLIC_API}/record-detail/get-multi-record-file"
    path += f"?record_id={record_id}&auth_share_id={share_id}&pwd=&activity_uid="
    data = base.browser_xhr(target_id, "GET", path)
    return data.get("data", {}).get("files", [])


def sign_share_file(target_id, share_id, record_id, resource_id, resource_type):
    path = f"{PUBLIC_API}/record-detail/sign-multi-record-file"
    path += f"?record_id={record_id}&resource_id={resource_id}&resource_type={resource_type}"
    path += f"&auth_share_id={share_id}&pwd=&activity_uid="
    data = base.browser_xhr(target_id, "GET", path)
    url = data.get("data", {}).get("url")
    if not url:
        raise RuntimeError("sign returned no URL")
    return url


def find_meeting_dir(output_dir, title, start_time_ms):
    Path(output_dir).mkdir(parents=True, exist_ok=True)  # 新服务器上根目录可能还不存在
    safe_title = base.safe_name(title)
    ts = base.ts_prefix(start_time_ms) if start_time_ms else "unknown_time"
    intended = Path(output_dir) / f"{ts}_{safe_title}"
    # Prefer an exact timestamp+title match so a re-run resolves to the same
    # folder. Distinct meetings that share a title but differ in start time
    # (e.g. a recurring series all named "亦可心理-督导成长系列培训") must NOT
    # collapse into one folder and overwrite each other.
    if intended.is_dir():
        return intended
    if start_time_ms:
        # Same meeting, title text drifted slightly: match by unique ts prefix.
        for child in sorted(Path(output_dir).iterdir()):
            if child.is_dir() and child.name.startswith(f"{ts}_"):
                return child
        return intended
    # No start time available: fall back to best-effort title-only match.
    for child in sorted(Path(output_dir).iterdir()):
        if child.is_dir() and child.name.endswith(f"_{safe_title}"):
            return child
    return intended


def maybe_decode_title(value):
    # Share APIs return the meeting subject base64-encoded; decode when needed
    value = str(value or "").strip()
    if not value or re.search(r"[一-鿿]", value):
        return value
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", value):
        return value
    import base64

    padded = value + "=" * (-len(value) % 4)
    for candidate in (padded, padded.replace("-", "+").replace("_", "/")):
        try:
            decoded = base64.b64decode(candidate, validate=False).decode("utf-8").strip()
        except Exception:
            continue
        if decoded:
            return decoded
    return value


def extract_start_time(detail):
    info = detail.get("meeting_info") or {}
    for key in ("start_time", "meeting_start_time", "media_start_time"):
        value = info.get(key) or detail.get(key)
        if value:
            return str(value)
    return ""


def manifest_file_key(item):
    resource_type = normalize_resource_type(item.get("resource_type"))
    segment_index = item.get("segment_index")
    if resource_type in base.RESOURCE_LABELS:
        return resource_type, segment_index

    name = str(item.get("name") or "")
    legacy = re.match(r"^(mixed|screen|speaker)(?:_(\d+))?\.[^.]+$", name)
    if legacy:
        label_to_type = {label: key for key, label in base.RESOURCE_LABELS.items()}
        return label_to_type[legacy.group(1)], int(legacy.group(2)) if legacy.group(2) else None

    view_labels = {"混合画面": 0, "共享屏幕": 1, "讲者摄像头": 2}
    current = re.search(r"(?:_第(\d+)段)?_(混合画面|共享屏幕|讲者摄像头)\.[^.]+$", name)
    if current:
        return view_labels[current.group(2)], int(current.group(1)) if current.group(1) else None

    return "name", name


def update_manifest(meeting_dir, share, title, detail, file_results):
    manifest_path = meeting_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {}
    existing = {manifest_file_key(f): f for f in manifest.get("files", [])}
    for result in file_results:
        existing[manifest_file_key(result)] = result
    manifest.update(
        {
            "short_code": share["short_code"],
            "title": title,
            "share_id_prefix": share["share_id"][:8] + "…",
            "meeting_id": (detail.get("meeting_info") or {}).get("meeting_id", manifest.get("meeting_id", "")),
            "start_time_ms": extract_start_time(detail) or manifest.get("start_time_ms", ""),
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": list(existing.values()),
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def process_share(target_id, code, output_dir, dry_run=False, max_retries=2, with_minutes=False):
    share = resolve_share(target_id, code)
    detail = fetch_common_record_info(target_id, share)
    info = detail.get("meeting_info") or {}
    title = base.safe_name(
        maybe_decode_title(info.get("origin_subject"))
        or maybe_decode_title(info.get("subject"))
        or maybe_decode_title(detail.get("subject"))
        or code
    )
    recordings = detail.get("recordings") or []
    if not recordings:
        raise RuntimeError(f"{code}: no recordings in share detail")

    meeting_dir = find_meeting_dir(output_dir, title, extract_start_time(detail))
    meeting_dir.mkdir(parents=True, exist_ok=True)
    print(f"{code}: {title} -> {meeting_dir.name}")

    base.REFERER = f"{BASE_URL}/cw/{code}"
    file_results = []
    # A single meeting may be split into multiple recordings (stop/restart).
    # Each recording carries its own mixed/screen/speaker, which share the same
    # resource labels — suffix the filename with the recording index so segment 2
    # does not overwrite segment 1. Single-recording meetings stay un-suffixed.
    multi_recording = len(recordings) > 1
    for rec_idx, recording in enumerate(recordings, 1):
        record_id = recording.get("id")
        files = fetch_share_files(target_id, share["share_id"], record_id)
        for f in files:
            resource_type = int(f.get("resource_type", -1))
            size = int(f.get("size") or 0)
            if size <= 0 or resource_type not in base.RESOURCE_LABELS:
                continue
            segment_index = rec_idx if multi_recording else None
            filename = media_filename(title, resource_type, segment_index=segment_index)
            dest = meeting_dir / filename
            existing = find_existing_complete_path(
                meeting_dir,
                title,
                resource_type,
                expected_size=size,
                segment_index=segment_index,
                tolerance=0.95,
            )
            result_base = {
                "resource_type": resource_type,
                "segment_index": segment_index,
                "size": size,
            }
            if existing:
                print(f"  SKIP (exists): {existing.name}")
                file_results.append({
                    **result_base,
                    "name": existing.name,
                    "local_path": str(existing),
                    "status": "skipped",
                })
                continue
            if dry_run:
                print(f"  DRY-RUN: {filename} ({size / 1024**2:.1f}MB)")
                file_results.append({
                    **result_base,
                    "name": filename,
                    "local_path": str(dest),
                    "status": "dry-run",
                })
                continue
            status = "error: not attempted"
            for attempt in range(max_retries + 1):
                signed = sign_share_file(
                    target_id, share["share_id"], record_id, f.get("resource_id"), resource_type
                )
                print(f"  Downloading {filename} ({size / 1024**2:.1f}MB)...", end=" ", flush=True)
                status = base.download_file(signed, dest, size)
                print(status)
                if status in ("done", "skip"):
                    break
                if attempt < max_retries:
                    print("  Re-signing and retrying...")
            file_results.append({
                **result_base,
                "name": filename,
                "local_path": str(dest),
                "status": status,
            })

    if not dry_run:
        update_manifest(meeting_dir, share, title, detail, file_results)
        if with_minutes:
            try:
                import export_share_minutes_via_cdp as minutes_mod

                extra = minutes_mod.export_for_share(target_id, code, meeting_dir)
                print(f"  纪要/逐字稿: {extra}")
            except Exception as exc:  # 纪要失败不影响视频结果
                print(f"  纪要/逐字稿导出失败: {exc}")
    return file_results


def main():
    parser = argparse.ArgumentParser(description="Download Tencent Meeting share recordings via CDP")
    parser.add_argument("--output-dir", default=str(base.OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cdp-port", type=int, default=base.linux_env.CDP_PORT)
    parser.add_argument("--cdp-mode", choices=["native", "proxy"], default="native")
    parser.add_argument("--with-minutes", action="store_true", help="同时导出 AI 会议纪要与逐字稿")
    parser.add_argument("urls", nargs="+")
    args = parser.parse_args()

    base.CDP_PORT = args.cdp_port
    base.CDP_MODE = args.cdp_mode
    codes = [urlparse(u.strip()).path.strip("/").split("/")[-1] for u in args.urls]

    r = base.cdp_call("GET", f"/new?url={BASE_URL}/cw/{codes[0]}")
    if not isinstance(r, dict) or "targetId" not in r:
        raise SystemExit(f"Failed to open tab: {r}")
    target_id = r["targetId"]
    time.sleep(4)
    # Signed COS file URLs require the logged-in session cookie (incl. HttpOnly) or they 403.
    base.refresh_auth_from_browser(target_id)
    if "token_expire_time" not in (base.COOKIE or ""):
        print("WARNING: 没有腾讯会议登录 cookie —— 需要登录的链接会失败/下载 403。"
              "先运行 python3 scripts/login_via_qr.py tencent")

    failures = []
    try:
        for code in codes:
            try:
                results = process_share(
                    target_id, code, args.output_dir, args.dry_run, with_minutes=args.with_minutes
                )
                bad = [x for x in results if str(x["status"]).startswith("error")]
                if bad:
                    failures.append({"short_code": code, "errors": bad})
            except Exception as exc:
                print(f"{code}: failed: {exc}")
                failures.append({"short_code": code, "error": str(exc)})
            time.sleep(0.5)
    finally:
        base.cdp_call("GET", f"/close?target={target_id}")

    if failures:
        print(f"\nFailures: {json.dumps(failures, ensure_ascii=False, indent=2)}")
        raise SystemExit(1)
    print("\nAll shares processed successfully.")


if __name__ == "__main__":
    main()
