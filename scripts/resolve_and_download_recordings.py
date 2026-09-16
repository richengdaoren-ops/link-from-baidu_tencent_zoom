#!/usr/bin/env python3
import argparse
import gzip
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from recording_naming import (
    RESOURCE_LABELS,
    find_existing_complete_path,
    media_filename,
    path_is_complete,
    segment_indexes,
)

from enumerate_records_from_curls import parse_curl, replace_query, request_json


STREAM_LABELS = {str(key): value for key, value in RESOURCE_LABELS.items()}
STREAM_LABELS["3"] = "other"
import linux_env  # noqa: E402

DEFAULT_MY_RECORDS_OUTPUT_DIR = str(linux_env.DOWNLOAD_ROOT)


def safe_name(value, fallback="untitled"):
    value = str(value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def ts_prefix(milliseconds):
    try:
        return time.strftime("%Y%m%d_%H%M", time.localtime(int(milliseconds) / 1000))
    except Exception:
        return "unknown-time"


def meeting_dir_name(record):
    """Build per-meeting subdirectory name: {YYYYMMDD}_{HHMM}_{title}"""
    ts = ts_prefix(record.get("start_time"))
    title = safe_name(record.get("title") or "未命名会议")
    return f"{ts}_{title}"


def endpoint_url(template_url, endpoint, **params):
    parsed = urlparse(template_url)
    path = re.sub(r"/[^/]+$", f"/{endpoint}", parsed.path)
    return replace_query(parsed._replace(path=path).geturl(), **params)


def response_json_or_none(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def choose_resource(files, version):
    stream_type = str(version.get("stream_type"))
    size = str(version.get("size") or "")
    matches = [f for f in files if str(f.get("resource_type")) == stream_type]
    if size:
        sized = [f for f in matches if str(f.get("size") or "") == size]
        if sized:
            return sized[0]
    if matches:
        return matches[0]
    return None


def resolve_one(file_url_template, file_headers, record, version):
    share_id = record.get("encode_record_id")
    recording_id = version.get("recording_id")
    files_url = replace_query(
        file_url_template,
        auth_share_id=share_id,
        record_id=recording_id,
        pwd="",
        activity_uid="",
    )
    files_resp = request_json(files_url, file_headers, None)
    if files_resp.get("code") != 0:
        raise RuntimeError(f"get-multi-record-file failed with code {files_resp.get('code')}")

    files = files_resp.get("data", {}).get("files", [])
    resource = choose_resource(files, version)
    if not resource:
        raise RuntimeError("no matching resource found")

    sign_url = endpoint_url(
        file_url_template,
        "sign-multi-record-file",
        auth_share_id=share_id,
        record_id=recording_id,
        resource_id=resource.get("resource_id"),
        resource_type=resource.get("resource_type"),
        pwd="",
        activity_uid="",
    )
    sign_resp = request_json(sign_url, file_headers, None)
    if sign_resp.get("code") != 0:
        raise RuntimeError(f"sign-multi-record-file failed with code {sign_resp.get('code')}")
    signed_url = sign_resp.get("data", {}).get("url")
    if not signed_url:
        raise RuntimeError("sign response did not contain a url")

    return signed_url, resource


def resolve_all(manifest_path, file_curl_path, output_path):
    file_url_template, file_headers, _ = parse_curl(Path(file_curl_path).read_text())
    manifest = json.loads(Path(manifest_path).read_text())
    resolved = []
    failures = []
    total_versions = sum(len(r.get("versions", [])) for r in manifest)
    done = 0

    for record in manifest:
        record_out = {k: v for k, v in record.items() if k != "versions"}
        record_out["versions"] = []
        source_versions = record.get("versions", [])
        segment_by_recording = segment_indexes(source_versions)
        for version in source_versions:
            done += 1
            label = STREAM_LABELS.get(str(version.get("stream_type")), str(version.get("stream_type")))
            title = record.get("title") or "未命名会议"
            try:
                signed_url, resource = resolve_one(file_url_template, file_headers, record, version)
                item = dict(version)
                item["download_url"] = signed_url
                item["resource"] = resource
                item["stream_label"] = label
                item["segment_index"] = segment_by_recording.get(version.get("recording_id"))
                record_out["versions"].append(item)
                print(f"resolved {done}/{total_versions}: {title} - {label}")
            except Exception as exc:
                failures.append(
                    {
                        "title": title,
                        "encode_record_id": record.get("encode_record_id"),
                        "recording_id": version.get("recording_id"),
                        "stream_type": version.get("stream_type"),
                        "error": str(exc),
                    }
                )
                print(f"failed {done}/{total_versions}: {title} - {label}: {exc}", file=sys.stderr)
        resolved.append(record_out)

    payload = {"records": resolved, "failures": failures}
    Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"saved {output_path}: {total_versions - len(failures)}/{total_versions} resolved")
    if failures:
        print(f"{len(failures)} failures were recorded in {output_path}", file=sys.stderr)


def extension_from_url(url):
    path = unquote(urlparse(url).path)
    suffix = Path(path).suffix.lower()
    return suffix if suffix else ".mp4"


def local_paths(record, version, output_dir):
    stream_type = version.get("stream_type")
    ext = extension_from_url(version.get("download_url", ""))
    folder = Path(output_dir) / meeting_dir_name(record)
    return folder, folder / media_filename(
        record.get("title"), stream_type, ext, version.get("segment_index")
    )


def existing_local_path(record, version, output_dir, tolerance=1.0):
    folder, dest = local_paths(record, version, output_dir)
    explicit = version.get("local_path")
    if explicit:
        explicit_path = Path(explicit)
        expected = version.get("size")
        if explicit_path.is_file() and path_is_complete(explicit_path, expected, tolerance):
            return explicit_path
    return find_existing_complete_path(
        folder,
        record.get("title"),
        version.get("stream_type"),
        expected_size=version.get("size"),
        extension=dest.suffix,
        segment_index=version.get("segment_index"),
        tolerance=tolerance,
    )


def download_file(url, dest, expected_size=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://meeting.tencent.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }

    existing = tmp.stat().st_size if tmp.exists() else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=60) as resp:
            mode = "ab" if existing and resp.status == 206 else "wb"
            if mode == "wb":
                existing = 0
            with tmp.open(mode) as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
    except HTTPError as exc:
        if existing and exc.code == 416:
            pass
        else:
            raise

    actual = tmp.stat().st_size
    if expected_size and actual != int(expected_size):
        raise RuntimeError(f"incomplete download: got {actual}, expected {expected_size}")
    tmp.replace(dest)


def download_one(index, total, record, version, output_dir):
    _, dest = local_paths(record, version, output_dir)
    expected_size = version.get("size")
    existing = existing_local_path(record, version, output_dir)
    if existing:
        version["local_path"] = str(existing)
        return f"skip {index}/{total}: {existing.name}"
    download_file(version["download_url"], dest, expected_size)
    version["local_path"] = str(dest)
    return f"done {index}/{total}: {dest.name}"


def download_all(resolved_path, output_dir, workers=1):
    payload = json.loads(Path(resolved_path).read_text())
    records = payload.get("records", [])
    versions = [(r, v) for r in records for v in r.get("versions", []) if v.get("download_url")]
    workers = max(1, int(workers or 1))
    if workers == 1:
        for idx, (record, version) in enumerate(versions, 1):
            print(download_one(idx, len(versions), record, version, output_dir), flush=True)
    else:
        print(f"downloading {len(versions)} files with {workers} workers", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(download_one, idx, len(versions), record, version, output_dir)
                for idx, (record, version) in enumerate(versions, 1)
            ]
            for future in as_completed(futures):
                print(future.result(), flush=True)
    print(f"downloads saved under {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="Resolve and download Tencent Meeting multi-recording files.")
    parser.add_argument("--manifest", default="recordings_manifest.json")
    parser.add_argument("--file-curl", default="file.curl")
    parser.add_argument("--resolved", default="download_urls_manifest.json")
    parser.add_argument("--output-dir", default=DEFAULT_MY_RECORDS_OUTPUT_DIR)
    parser.add_argument("--workers", type=int, default=1, help="Parallel downloads; 1 is safest")
    parser.add_argument("--resolve", action="store_true", help="Resolve signed download URLs")
    parser.add_argument("--download", action="store_true", help="Download resolved URLs")
    args = parser.parse_args()

    if not args.resolve and not args.download:
        args.resolve = True

    if args.resolve:
        resolve_all(args.manifest, args.file_curl, args.resolved)
    if args.download:
        download_all(args.resolved, args.output_dir, args.workers)


if __name__ == "__main__":
    main()
