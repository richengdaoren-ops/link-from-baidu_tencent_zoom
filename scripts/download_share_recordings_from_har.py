#!/usr/bin/env python3
import argparse
import gzip
import html
import json
import os
import random
import re
import string
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from recording_naming import (
    RESOURCE_LABELS,
    find_existing_complete_path,
    media_filename,
)


BASE_URL = "https://meeting.tencent.com"
PUBLIC_API = f"{BASE_URL}/wemeet-tapi/v2/meetlog/public"
SIGN_API = f"{BASE_URL}/wemeet-cloudrecording-webapi/v1/sign"
sys.path.insert(0, str(Path(__file__).parent))
import linux_env  # noqa: E402

DEFAULT_SHARE_OUTPUT_DIR = str(linux_env.DOWNLOAD_ROOT)



def nonce(length=9):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(length))


def safe_name(value, fallback="untitled"):
    value = str(value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:160] or fallback


def load_har_template(path):
    har = json.loads(Path(path).read_text())
    entries = har.get("log", {}).get("entries", [])
    useful = None
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        if "/wemeet-tapi/" in url and "get-multi-record-file" in url:
            useful = entry
            break
    if useful is None:
        raise RuntimeError("HAR does not contain get-multi-record-file")

    headers = {}
    for item in useful["request"].get("headers", []):
        name = item.get("name", "")
        if name.lower() in {"content-length", "accept-encoding"}:
            continue
        headers[name] = item.get("value", "")
    headers["Accept-Encoding"] = "identity"

    params = {}
    for item in useful["request"].get("queryString", []):
        key = item.get("name", "")
        if key in {
            "record_id",
            "auth_share_id",
            "uni_record_share_id",
            "id",
            "sharing_id",
            "pwd",
            "activity_uid",
            "source",
            "need_multi_stream",
            "enter_from",
        }:
            continue
        params[key] = item.get("value", "")
    params.setdefault("c_instance_id", "5")
    params.setdefault("platform", "Web")
    params.setdefault("c_lang", "zh-CN")
    return headers, params


def fresh_params(base, extra=None):
    params = dict(base)
    n = nonce()
    params["c_timestamp"] = str(int(time.time() * 1000))
    params["c_nonce"] = n
    params["rnds"] = n
    params["trace-id"] = "".join(random.choice("0123456789abcdef") for _ in range(32))
    if extra:
        params.update({k: "" if v is None else str(v) for k, v in extra.items()})
    return params


def make_url(path_or_url, params):
    url = path_or_url if path_or_url.startswith("http") else f"{PUBLIC_API}/{path_or_url.lstrip('/')}"
    parsed = urlparse(url)
    current = dict(parse_qsl(parsed.query, keep_blank_values=True))
    current.update(params)
    return urlunparse(parsed._replace(query=urlencode(current)))


def request_bytes(url, headers, body=None, method=None, timeout=60):
    data = None
    clean = dict(headers)
    clean.setdefault("User-Agent", "Mozilla/5.0")
    clean["Accept-Encoding"] = "identity"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        clean["Content-Type"] = "application/json"
        method = method or "POST"
    else:
        clean.pop("Content-Type", None)
        method = method or "GET"
    clean.pop("Content-Length", None)
    req = Request(url, data=data, headers=clean, method=method)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw, resp.geturl()


def request_json(url, headers, body=None, method=None):
    raw, _ = request_bytes(url, headers, body=body, method=method)
    return json.loads(raw.decode("utf-8"))


def decode_json_string(value):
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def parse_share_page(text, short_code):
    patterns = [
        r'"long_url"\s*:\s*"([^"]+)"',
        r'\\"long_url\\"\s*:\s*\\"([^"]+?)\\"',
        r"long_url['\"]?\s*[:=]\s*['\"]([^'\"]+)",
    ]
    long_url = ""
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            long_url = html.unescape(decode_json_string(match.group(1)).replace("\\u0026", "&"))
            break
    if not long_url:
        raise RuntimeError(f"{short_code}: could not find long_url")
    parsed = urlparse(long_url)
    query_text = parsed.query or long_url.lstrip("?")
    query = dict(parse_qsl(query_text, keep_blank_values=True))
    share_id = query.get("id") or query.get("sharing_id")
    if not share_id:
        raise RuntimeError(f"{short_code}: could not find share id")
    return {
        "short_code": short_code,
        "share_id": share_id,
        "is_single": str(query.get("is-single", "")).lower() == "true",
        "record_type": query.get("record_type"),
        "long_url_redacted": re.sub(r"(id=)[^&]+", r"\1<redacted>", long_url),
    }


def maybe_decode_title(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if re.search(r"[\u4e00-\u9fff]", value):
        return value
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = __import__("base64").b64decode(padded, validate=False).decode("utf-8").strip()
    except Exception:
        return value
    return decoded or value


def title_from_detail(detail, fallback):
    info = detail.get("meeting_info") or {}
    for key in ("origin_subject", "subject"):
        title = maybe_decode_title(info.get(key))
        if title:
            return title
    return fallback


def collect_recording(headers, api_params, short_code):
    page_headers = dict(headers)
    page_headers["Accept"] = "text/html,*/*"
    text = request_bytes(f"{BASE_URL}/cw/{short_code}", page_headers)[0].decode("utf-8", "replace")
    share = parse_share_page(text, short_code)

    common_body = {
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
        "short_url_code": short_code,
        "is_short_ctw": False,
    }
    common = request_json(
        make_url("detail/common-record-info", fresh_params(api_params)),
        headers,
        body=common_body,
    )
    if common.get("code") != 0:
        raise RuntimeError(f"{short_code}: common-record-info failed: {common.get('msg') or common}")
    detail = common.get("data", {})
    title = title_from_detail(detail, short_code)
    recordings = detail.get("recordings") or []
    if not recordings:
        raise RuntimeError(f"{short_code}: no recordings found")

    items = []
    multi_recording = len(recordings) > 1
    for recording_index, recording in enumerate(recordings, 1):
        record_id = recording.get("id")
        record_share_id = recording.get("sharing_id") or share["share_id"]
        files = request_json(
            make_url(
                "record-detail/get-multi-record-file",
                fresh_params(
                    api_params,
                    {
                        "record_id": record_id,
                        "auth_share_id": share["share_id"],
                        "activity_uid": "",
                    },
                ),
            ),
            headers,
        )
        if files.get("code") != 0:
            raise RuntimeError(f"{short_code}: get-multi-record-file failed: {files.get('msg') or files}")
        sign = request_json(
            make_url(
                SIGN_API,
                fresh_params(
                    api_params,
                    {
                        "id": record_share_id,
                        "source": "shares",
                        "pwd": "",
                        "activity_uid": "",
                        "tk": "",
                        "sharing_id": share["share_id"],
                        "need_multi_stream": "1",
                        "enter_from": "share",
                    },
                ),
            ),
            headers,
        )
        if sign.get("code") != 0:
            raise RuntimeError(f"{short_code}: sign failed: {sign.get('msg') or sign}")
        data = sign.get("data", {})
        signed = []
        if data.get("sign_urls"):
            for url in data["sign_urls"]:
                signed.append({"stream_type": 0, "sign_url": url})
        signed.extend(data.get("multi_stream_recordings") or [])
        signed_types = {str(item.get("stream_type")) for item in signed}
        for resource in files.get("data", {}).get("files", []):
            resource_type = str(resource.get("resource_type"))
            if int(resource.get("size") or 0) <= 0 or resource_type in signed_types:
                continue
            legacy_sign = request_json(
                make_url(
                    "record-detail/sign-multi-record-file",
                    fresh_params(
                        api_params,
                        {
                            "record_id": record_id,
                            "resource_id": resource.get("resource_id"),
                            "resource_type": resource.get("resource_type"),
                            "auth_share_id": share["share_id"],
                            "pwd": "",
                            "activity_uid": "",
                        },
                    ),
                ),
                headers,
            )
            if legacy_sign.get("code") != 0:
                continue
            legacy_url = (legacy_sign.get("data") or {}).get("url")
            if legacy_url:
                signed.append(
                    {
                        "stream_type": resource.get("resource_type"),
                        "sign_url": legacy_url,
                        "size": resource.get("size"),
                    }
                )
                signed_types.add(resource_type)
        for signed_item in signed:
            url = signed_item.get("sign_url")
            if not url:
                continue
            items.append(
                {
                    "short_code": short_code,
                    "title": title,
                    "record_id": record_id,
                    "record_share_id": record_share_id,
                    "segment_index": recording_index if multi_recording else None,
                    "stream_type": signed_item.get("stream_type"),
                    "size": signed_item.get("size") or next(
                        (
                            f.get("size")
                            for f in files.get("data", {}).get("files", [])
                            if str(f.get("resource_type")) == str(signed_item.get("stream_type"))
                        ),
                        "",
                    ),
                    "url": url,
                }
            )
    return {"share": share, "title": title, "items": items}


def output_path(root, item):
    parsed = urlparse(item["url"])
    original = Path(parsed.path).name or f"stream_{item.get('stream_type')}.mp4"
    title = safe_name(item.get("title"), "未命名会议")
    stream_type = int(item.get("stream_type") or 0)
    start_ts = item.get("start_time")
    if start_ts:
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M", _time.localtime(int(start_ts) / 1000))
    else:
        ts = "unknown"
    folder = Path(root) / f"{ts}_{title}"
    ext = Path(original).suffix or ".mp4"
    return folder / media_filename(
        title, stream_type, ext, item.get("segment_index")
    )


def existing_output_path(root, item):
    dest = output_path(root, item)
    return find_existing_complete_path(
        dest.parent,
        item.get("title"),
        item.get("stream_type"),
        expected_size=item.get("size"),
        extension=dest.suffix,
        segment_index=item.get("segment_index"),
    )


def download_file(url, dest, expected_size=None, referer=BASE_URL, cookie="", retries=3):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    expected = int(expected_size) if expected_size else None
    if dest.exists() and (expected is None or dest.stat().st_size == expected):
        return "skipped"
    for attempt in range(1, retries + 1):
        existing = tmp.stat().st_size if tmp.exists() else 0
        headers = {
            "User-Agent": linux_env.FALLBACK_UA,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Range": f"bytes={existing}-" if existing else "bytes=0-",
        }
        if cookie:
            headers["Cookie"] = cookie
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=60) as resp:
                mode = "ab" if existing and resp.status == 206 else "wb"
                with tmp.open(mode) as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
            actual = tmp.stat().st_size
            if expected and actual != expected:
                raise RuntimeError(f"incomplete download: got {actual}, expected {expected}")
            tmp.replace(dest)
            return "done"
        except HTTPError as exc:
            if expected and exc.code == 416 and tmp.exists() and tmp.stat().st_size == expected:
                tmp.replace(dest)
                return "done"
            if attempt == retries:
                raise
        except Exception:
            if attempt == retries:
                raise
        time.sleep(min(2 ** attempt, 10))
    raise RuntimeError("download failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--har", required=True)
    parser.add_argument("--output-dir", default=DEFAULT_SHARE_OUTPUT_DIR)
    parser.add_argument("--manifest", default="tencent_batch_manifest.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("urls", nargs="+")
    args = parser.parse_args()

    headers, params = load_har_template(args.har)
    auth_cookie = headers.get("Cookie", "")
    short_codes = [urlparse(u).path.strip("/").split("/")[-1] for u in args.urls]
    manifest = {"items": [], "failures": []}
    for code in short_codes:
        try:
            result = collect_recording(headers, params, code)
            print(f"{code}: {result['title']} - {len(result['items'])} signed streams")
            for item in result["items"]:
                expected_dest = output_path(args.output_dir, item)
                existing = existing_output_path(args.output_dir, item)
                dest = existing or expected_dest
                item_out = {k: v for k, v in item.items() if k != "url"}
                item_out["local_path"] = str(dest)
                item_out["signed_url_redacted"] = urlunparse(urlparse(item["url"])._replace(query=""))
                if args.dry_run:
                    item_out["download_status"] = "dry-run"
                elif existing:
                    item_out["download_status"] = "skipped"
                    print(f"  skipped: stream_type={item.get('stream_type')} {dest.name}")
                else:
                    status = download_file(
                        item["url"],
                        dest,
                        expected_size=item.get("size"),
                        referer=f"{BASE_URL}/cw/{code}",
                        cookie=auth_cookie,
                    )
                    item_out["download_status"] = status
                    print(f"  {status}: stream_type={item.get('stream_type')} {dest.name}")
                manifest["items"].append(item_out)
        except Exception as exc:
            manifest["failures"].append({"short_code": code, "error": str(exc)})
            print(f"{code}: failed: {exc}")
    Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"saved manifest: {args.manifest}")
    if manifest["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
