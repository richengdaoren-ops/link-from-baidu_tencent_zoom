#!/usr/bin/env python3
import argparse
import base64
import binascii
import gzip
import html
import json
import os
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from getpass import getpass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
import linux_env  # noqa: E402
from recording_naming import (  # noqa: E402
    RESOURCE_LABELS as NAMING_RESOURCE_LABELS,
    find_existing_complete_path,
    media_filename,
)


BASE_URL = "https://meeting.tencent.com"
API_PREFIX = f"{BASE_URL}/wemeet-tapi/v2/meetlog/public"
CHROME_UA = linux_env.FALLBACK_UA
RESOURCE_LABELS = NAMING_RESOURCE_LABELS
DEFAULT_DOWNLOAD_ROOT = str(linux_env.DOWNLOAD_ROOT)
DEFAULT_PUBLIC_MANIFEST = "public_share_manifest.json"


def safe_name(value, fallback="untitled"):
    value = str(value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def maybe_decode_base64_text(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if re.search(r"[\u4e00-\u9fff]", value):
        return value
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", value):
        return value
    padded = value + "=" * (-len(value) % 4)
    for candidate in (padded, padded.replace("-", "+").replace("_", "/")):
        try:
            decoded = base64.b64decode(candidate, validate=False).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError):
            continue
        if decoded and re.search(r"[\u4e00-\u9fffA-Za-z0-9]", decoded):
            return decoded
    return value


def meeting_title_from_detail(detail, fallback="tencent-meeting-recording"):
    meeting_info = detail.get("meeting_info") or {}
    candidates = [
        meeting_info.get("origin_subject"),
        meeting_info.get("subject"),
        detail.get("origin_subject"),
        detail.get("subject"),
        detail.get("title"),
        fallback,
    ]
    for candidate in candidates:
        decoded = maybe_decode_base64_text(candidate)
        if decoded:
            return decoded
    return fallback


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).lower() in ("1", "true", "yes", "y")


def replace_query(url, **values):
    parsed = urlparse(url)
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in values.items():
        pairs[key] = "" if value is None else str(value)
    pairs.setdefault("c_timestamp", str(int(time.time() * 1000)))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def request_bytes(url, headers=None, data=None, method=None, timeout=30):
    clean_headers = dict(headers or {})
    clean_headers.setdefault("User-Agent", CHROME_UA)
    clean_headers.setdefault("Accept-Encoding", "identity")
    req = Request(url, data=data, headers=clean_headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        final_url = resp.geturl()
    return raw, final_url


def request_text(url, headers=None, timeout=30):
    raw, final_url = request_bytes(url, headers=headers, timeout=timeout)
    return raw.decode("utf-8", errors="replace"), final_url


def request_json(url, headers=None, body=None, method=None):
    data = None
    clean_headers = dict(headers or {})
    clean_headers.setdefault("Accept", "application/json, text/plain, */*")
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        clean_headers.setdefault("Content-Type", "application/json")
        method = method or "POST"
    else:
        clean_headers.pop("Content-Type", None)
        method = method or "GET"
    raw, _ = request_bytes(url, headers=clean_headers, data=data, method=method)
    return json.loads(raw.decode("utf-8"))


def api_url(path, params):
    return replace_query(f"{API_PREFIX}/{path.lstrip('/')}", **params)


def default_api_params(args):
    params = {
        "c_app_id": args.c_app_id,
        "c_os_model": args.c_os_model,
        "c_os": args.c_os,
        "c_os_version": args.c_os_version,
        "c_app_version": args.c_app_version,
        "c_instance_id": args.c_instance_id or uuid.uuid4().hex,
        "platform": args.platform,
    }
    for item in args.api_param or []:
        if "=" not in item:
            raise ValueError(f"--api-param must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        params[key] = value
    return params


def parse_input_url(value):
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"https://{value}")
    host = parsed.netloc.lower()
    if host and "meeting.tencent.com" not in host:
        raise ValueError("expected a meeting.tencent.com URL")

    parts = [part for part in parsed.path.split("/") if part]
    short_code = None
    if len(parts) >= 2 and parts[0] in ("crm", "cw"):
        short_code = parts[1]
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    share_id = query.get("id") or query.get("sharing_id")
    is_single = parse_bool(query.get("is-single"), False)
    record_type = query.get("record_type")
    return {
        "input_url": urlunparse(parsed),
        "short_code": short_code,
        "share_id": share_id,
        "is_single": is_single,
        "record_type": record_type,
    }


def json_string(value):
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def extract_share_info(html_text, parsed_input):
    info = dict(parsed_input)
    long_url = None

    patterns = [
        r'"long_url"\s*:\s*"([^"]+)"',
        r'\\"long_url\\"\s*:\s*\\"([^"]+?)\\"',
        r"long_url['\"]?\s*[:=]\s*['\"]([^'\"]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if match:
            long_url = html.unescape(json_string(match.group(1)).replace("\\u0026", "&"))
            break

    if long_url:
        parsed_long = urlparse(long_url)
        query_text = parsed_long.query or long_url.lstrip("?")
        query = dict(parse_qsl(query_text, keep_blank_values=True))
        info["long_url"] = long_url
        info["share_id"] = info.get("share_id") or query.get("id") or query.get("sharing_id")
        info["is_single"] = parse_bool(query.get("is-single"), info.get("is_single", False))
        info["record_type"] = info.get("record_type") or query.get("record_type")

    if not info.get("share_id"):
        match = re.search(
            r"[?&](?:id|sharing_id)=([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})",
            html_text,
        )
        if match:
            info["share_id"] = match.group(1)

    title_match = re.search(r'"subject"\s*:\s*"([^"]+)"', html_text) or re.search(
        r'"title"\s*:\s*"([^"]+)"', html_text
    )
    if title_match:
        info["title"] = html.unescape(json_string(title_match.group(1)))

    if not info.get("short_code"):
        raise ValueError("could not find /crm or /cw short code in URL")
    if not info.get("share_id"):
        raise ValueError("could not extract share id from /cw page")
    return info


def public_headers(short_code):
    return {
        "User-Agent": CHROME_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/cw/{short_code}",
    }


def assert_ok(resp, label):
    if resp.get("code") != 0:
        msg = resp.get("msg") or resp.get("message") or resp.get("err_detail") or ""
        raise RuntimeError(f"{label} failed with code {resp.get('code')}: {msg}")
    return resp


def auth_password(api_params, share_info, password, headers):
    params = dict(api_params)
    params.update(
        {
            "share_id": share_info["share_id"],
            "enter_from": "share",
            "pwd": password,
        }
    )
    resp = request_json(api_url("permission/auth", params), headers=headers)
    return assert_ok(resp, "permission/auth").get("data", {})


def fetch_common_record_info(api_params, share_info, password, headers):
    params = dict(api_params)
    body = {
        "pk_meeting_info_id": "",
        "sharing_id": share_info["share_id"],
        "is_single": bool(share_info.get("is_single")),
        "cover_image_style": "meetlog_detail_webp_1000",
        "pwd": password,
        "activity_uid": "",
        "lang": "zh-CN",
        "is_origin_content": True,
        "is_cve": True,
        "forward_cgi_path": "shares",
        "enter_from": "share",
        "short_url_code": share_info["short_code"],
    }
    resp = request_json(api_url("detail/common-record-info", params), headers=headers, body=body)
    return assert_ok(resp, "common-record-info").get("data", {})


def fetch_multi_info(api_params, auth_share_id, uni_record_share_id, password, headers):
    params = dict(api_params)
    params.update(
        {
            "pwd": password,
            "auth_share_id": auth_share_id,
            "uni_record_share_id": uni_record_share_id or auth_share_id,
            "activity_uid": "",
        }
    )
    resp = request_json(api_url("record-detail/get-multi-record-info", params), headers=headers)
    return assert_ok(resp, "get-multi-record-info").get("data", {})


def fetch_files(api_params, auth_share_id, record_id, password, headers):
    params = dict(api_params)
    params.update(
        {
            "record_id": record_id,
            "auth_share_id": auth_share_id,
            "pwd": password,
            "activity_uid": "",
        }
    )
    resp = request_json(api_url("record-detail/get-multi-record-file", params), headers=headers)
    return assert_ok(resp, "get-multi-record-file").get("data", {})


def sign_file(api_params, auth_share_id, record_id, resource, password, headers):
    params = dict(api_params)
    params.update(
        {
            "record_id": record_id,
            "resource_id": resource.get("resource_id"),
            "resource_type": resource.get("resource_type"),
            "auth_share_id": auth_share_id,
            "pwd": password,
            "activity_uid": "",
        }
    )
    resp = request_json(api_url("record-detail/sign-multi-record-file", params), headers=headers)
    data = assert_ok(resp, "sign-multi-record-file").get("data", {})
    signed_url = data.get("url")
    if not signed_url:
        raise RuntimeError("sign-multi-record-file response did not contain url")
    return signed_url


def extension_from_url(url):
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix else ".mp4"


def redacted_url(url):
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query=""))


def output_path(output_dir, title, record_id, resource, signed_url=None):
    resource_type = int(resource.get("resource_type") or 0)
    ext = Path(urlparse(signed_url or "").path).suffix if signed_url else ".mp4"
    if not ext:
        ext = extension_from_url(signed_url or "") or ".mp4"
    return Path(output_dir) / media_filename(
        title, resource_type, ext, resource.get("segment_index")
    )


def existing_output_path(output_dir, title, resource, signed_url=None):
    resource_type = int(resource.get("resource_type") or 0)
    ext = Path(urlparse(signed_url or "").path).suffix if signed_url else ".mp4"
    if not ext:
        ext = extension_from_url(signed_url or "") or ".mp4"
    return find_existing_complete_path(
        output_dir,
        title,
        resource_type,
        expected_size=resource.get("size"),
        extension=ext,
        segment_index=resource.get("segment_index"),
    )


def expected_output_path(output_dir, resource, signed_url=None):
    """Return the deterministic local path for a replay resource.

    This mirrors output_path() but does not require a title/record_id.  It is
    used by dry-run manifests so local_path stays accurate before signing URLs.
    """
    return output_path(output_dir, resource.get("title"), resource.get("record_id"), resource, signed_url)


def public_share_output_dir(root_dir, share):
    title = safe_name(share.get("title") or "未命名会议")
    start_ts = share.get("start_time")
    if start_ts:
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M", _time.localtime(int(start_ts) / 1000))
    else:
        ts = "unknown"
    return Path(root_dir) / f"{ts}_{title}"


def download_file(url, dest, expected_size=None, referer=None, user_agent=CHROME_UA, retries=3):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    expected = int(expected_size) if expected_size else None

    if dest.exists() and (expected is None or dest.stat().st_size == expected):
        return "skipped"

    for attempt in range(1, retries + 1):
        existing = tmp.stat().st_size if tmp.exists() else 0
        headers = {
            "User-Agent": user_agent,
            "Referer": referer or BASE_URL,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        # 续传用 Range；新下载首次试 Range: bytes=0-，失败回退普通无 Range GET。
        # 部分 COS 节点对 Range: bytes=0- 返回 416，标准无 Range GET 更通用。
        if existing:
            headers["Range"] = f"bytes={existing}-"
        elif attempt == 1:
            headers["Range"] = "bytes=0-"
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
            if expected is not None and actual != expected:
                raise RuntimeError(f"incomplete download: got {actual}, expected {expected}")
            tmp.replace(dest)
            return "done"
        except HTTPError as exc:
            if expected is not None and exc.code == 416 and tmp.exists() and tmp.stat().st_size == expected:
                tmp.replace(dest)
                return "done"
            if attempt == retries:
                raise
        except Exception:
            if attempt == retries:
                raise
        time.sleep(min(2 ** attempt, 10))
    raise RuntimeError("download failed")


def collect_resources(api_params, share_info, password, headers, include_multi_info=True):
    auth_data = auth_password(api_params, share_info, password, headers)
    detail = fetch_common_record_info(api_params, share_info, password, headers)
    title = meeting_title_from_detail(detail, share_info.get("title") or "tencent-meeting-recording")
    recordings = detail.get("recordings") or []
    if not recordings:
        raise RuntimeError("common-record-info returned no recordings")

    resources = []
    multi_recording = len(recordings) > 1
    for recording_index, recording in enumerate(recordings, 1):
        record_id = recording.get("id") or recording.get("recording_id")
        if not record_id:
            continue
        uni_record_share_id = recording.get("sharing_id") or share_info["share_id"]
        multi_info = {}
        if include_multi_info:
            try:
                multi_info = fetch_multi_info(
                    api_params,
                    share_info["share_id"],
                    uni_record_share_id,
                    password,
                    headers,
                )
            except Exception as exc:
                multi_info = {"error": str(exc)}
        files_data = fetch_files(api_params, share_info["share_id"], record_id, password, headers)
        for file_info in files_data.get("files", []):
            item = dict(file_info)
            item["record_id"] = record_id
            item["recording_sharing_id"] = uni_record_share_id
            item["title"] = title
            item["segment_index"] = recording_index if multi_recording else None
            item["multi_info_error"] = multi_info.get("error")
            resources.append(item)

    return {
        "share": {
            "short_code": share_info["short_code"],
            "share_id": share_info["share_id"],
            "is_single": bool(share_info.get("is_single")),
            "record_type": share_info.get("record_type"),
            "title": title,
            "download_enable": auth_data.get("download_enable"),
            "view_minutes_enable": auth_data.get("view_minutes_enable"),
        },
        "detail": {
            "pk_meeting_info_id": detail.get("pk_meeting_info_id"),
            "recording_count": detail.get("recording_count"),
            "allow_download": detail.get("allow_download"),
        },
        "resources": resources,
    }


def filter_resources(resources, resource_types):
    if not resource_types:
        return resources
    wanted = {int(item) for item in resource_types}
    return [item for item in resources if int(item.get("resource_type") or -1) in wanted]


def write_manifest(path, payload):
    if not path:
        return
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def check_disk_space(output_dir, resources):
    total = sum(int(item.get("size") or 0) for item in resources)
    free = shutil.disk_usage(Path(output_dir).resolve().parent if Path(output_dir).suffix else output_dir).free
    if total and free < total:
        raise RuntimeError(f"not enough free disk space: need {total} bytes, have {free} bytes")


def process_download(index, total, api_params, share, password, headers, resource, output_dir):
    signed_url = sign_file(api_params, share["share_id"], resource["record_id"], resource, password, headers)
    existing = existing_output_path(output_dir, resource.get("title"), resource, signed_url)
    dest = existing or output_path(output_dir, resource.get("title"), resource["record_id"], resource, signed_url)
    status = "skipped" if existing else download_file(
        signed_url,
        dest,
        expected_size=resource.get("size"),
        referer=f"{BASE_URL}/cw/{share['short_code']}",
        user_agent=CHROME_UA,
    )
    return {
        "index": index,
        "total": total,
        "status": status,
        "resource_id": resource.get("resource_id"),
        "resource_type": resource.get("resource_type"),
        "size": resource.get("size"),
        "local_path": str(dest),
        "signed_url_redacted": redacted_url(signed_url),
    }


def resolve_password(args):
    # 无密码公开链接：--no-password（旧版对空 env 直接 raise，Ubuntu 实战中踩过）
    if getattr(args, "no_password", False):
        return ""
    if args.password:
        return args.password
    if args.password_env:
        value = os.environ.get(args.password_env)
        if value is None:
            raise RuntimeError(f"environment variable {args.password_env} is not set (无密码链接请用 --no-password)")
        return value
    return getpass("Tencent Meeting replay password: ")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Download Tencent Meeting public /crm or /cw replay links with a password."
    )
    parser.add_argument("url", help="Tencent Meeting public replay URL, e.g. https://meeting.tencent.com/crm/xxxx")
    parser.add_argument("--password", help="Replay password. Prefer --password-env for shared terminals.")
    parser.add_argument("--password-env", help="Read replay password from this environment variable.")
    parser.add_argument("--no-password", action="store_true",
                        help="链接本身无密码。若 permission/auth 仍报“用户鉴权失败”，说明是需登录的链接，改走 C3（CDP）流程")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_DOWNLOAD_ROOT,
        help="Download root directory. Each public replay link gets its own subfolder.",
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_PUBLIC_MANIFEST,
        help="Manifest path. A relative path is saved inside the replay subfolder.",
    )
    parser.add_argument("--resource-type", action="append", type=int, help="Only download this resource_type; repeatable")
    parser.add_argument("--all", action="store_true", help="Download all resources. This is the default.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel downloads; 1 is safest")
    parser.add_argument("--dry-run", action="store_true", help="Resolve metadata only; do not sign or download files")
    parser.add_argument("--debug", action="store_true", help="Print non-secret debug progress")
    parser.add_argument("--skip-multi-info", action="store_true", help="Skip optional get-multi-record-info call")
    parser.add_argument("--skip-disk-check", action="store_true", help="Do not check free disk space before download")
    parser.add_argument("--api-param", action="append", help="Override/add API query parameter as KEY=VALUE")
    parser.add_argument("--c-app-id", default="1400000000")
    parser.add_argument("--c-os-model", default="Linux x86_64")
    parser.add_argument("--c-os", default="web")
    parser.add_argument("--c-os-version", default="Linux")
    parser.add_argument("--c-app-version", default="1.0.0")
    parser.add_argument("--c-instance-id", default="")
    parser.add_argument("--platform", default="web")
    return parser


def main():
    args = build_parser().parse_args()
    password = resolve_password(args)
    parsed_input = parse_input_url(args.url)

    page_url = f"{BASE_URL}/cw/{parsed_input['short_code']}" if parsed_input.get("short_code") else args.url
    if args.debug:
        print(f"loading page: {page_url}")
    html_text, final_url = request_text(page_url, headers={"User-Agent": CHROME_UA, "Accept": "text/html,*/*"})
    share_info = extract_share_info(html_text, parsed_input)
    if args.debug and final_url != page_url:
        print(f"final page URL: {final_url}")
    print(f"resolved share: /cw/{share_info['short_code']} id={share_info['share_id']}")

    api_params = default_api_params(args)
    headers = public_headers(share_info["short_code"])
    payload = collect_resources(api_params, share_info, password, headers, include_multi_info=not args.skip_multi_info)
    payload["resources"] = filter_resources(payload["resources"], args.resource_type)
    # 跳过 size=0 的资源（纯音频 resource_type=3：API 报 size=0 且不支持断点续传，
    # 无法验证下载完成；主音轨已在视频流里）
    payload["resources"] = [r for r in payload["resources"] if int(r.get("size") or 0) > 0]
    if not payload["resources"]:
        raise RuntimeError("no matching resources found")
    output_dir = public_share_output_dir(args.output_dir, payload["share"])
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = output_dir / manifest_path

    total_size = sum(int(item.get("size") or 0) for item in payload["resources"])
    print(f"found {len(payload['resources'])} resources, total {total_size} bytes")
    print(f"output folder: {output_dir}")

    if args.dry_run:
        for item in payload["resources"]:
            item["download_status"] = "dry-run"
            item["local_path"] = str(expected_output_path(output_dir, item))
        output_dir.mkdir(parents=True, exist_ok=True)
        write_manifest(manifest_path, payload)
        print(f"saved manifest: {manifest_path}")
        return

    if not args.skip_disk_check:
        output_dir.mkdir(parents=True, exist_ok=True)
        check_disk_space(output_dir, payload["resources"])

    workers = max(1, int(args.workers or 1))
    results = []
    if workers == 1:
        for idx, resource in enumerate(payload["resources"], 1):
            result = process_download(idx, len(payload["resources"]), api_params, payload["share"], password, headers, resource, output_dir)
            results.append(result)
            print(f"{result['status']} {idx}/{len(payload['resources'])}: resource_type={result['resource_type']}")
    else:
        print(f"downloading with {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    process_download,
                    idx,
                    len(payload["resources"]),
                    api_params,
                    payload["share"],
                    password,
                    headers,
                    resource,
                    output_dir,
                )
                for idx, resource in enumerate(payload["resources"], 1)
            ]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"{result['status']} {result['index']}/{result['total']}: resource_type={result['resource_type']}")

    by_resource_id = {str(r["resource_id"]): r for r in results if r.get("resource_id")}
    used_result_indexes = set()
    for item in payload["resources"]:
        result = None
        resource_id = str(item.get("resource_id"))
        resource_type = str(item.get("resource_type"))
        if resource_id in by_resource_id:
            candidates = [
                (idx, r)
                for idx, r in enumerate(results)
                if str(r.get("resource_id")) == resource_id and str(r.get("resource_type")) == resource_type
            ]
            if not candidates:
                candidates = [
                    (idx, r)
                    for idx, r in enumerate(results)
                    if str(r.get("resource_id")) == resource_id
                ]
            for idx, candidate in candidates:
                if idx not in used_result_indexes:
                    result = candidate
                    used_result_indexes.add(idx)
                    break
        if not result:
            item["download_status"] = None
            item["local_path"] = str(expected_output_path(output_dir, item))
            item["signed_url_redacted"] = None
            continue
        item["download_status"] = result.get("status")
        item["local_path"] = result.get("local_path")
        item["signed_url_redacted"] = result.get("signed_url_redacted")
    write_manifest(manifest_path, payload)
    print(f"saved manifest: {manifest_path}")
    print(f"downloads saved under {output_dir}")


if __name__ == "__main__":
    main()
