#!/usr/bin/env python3
import argparse
import gzip
import json
import random
import re
import shlex
import string
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


def parse_curl(text):
    tokens = shlex.split(text.replace("\\\n", " "))
    if not tokens or tokens[0] != "curl":
        raise ValueError("input must start with curl")

    url = None
    headers = {}
    body = None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("http"):
            url = token
        elif token in ("-H", "--header") and i + 1 < len(tokens):
            raw = tokens[i + 1]
            if ":" in raw:
                key, value = raw.split(":", 1)
                headers[key.strip()] = value.strip()
            i += 1
        elif token in ("--data-raw", "--data", "--data-binary", "-d") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 1
        i += 1

    if not url:
        raise ValueError("no URL found in curl")
    return url, headers, body


def fresh_url(url):
    parsed = urlparse(url)
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    nonce = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(8))
    pairs["c_timestamp"] = str(int(time.time() * 1000))
    if "c_nonce" in pairs:
        pairs["c_nonce"] = nonce
    if "rnds" in pairs:
        pairs["rnds"] = nonce
    if "trace-id" in pairs:
        pairs["trace-id"] = "".join(random.choice("0123456789abcdef") for _ in range(32))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def replace_query(url, **values):
    parsed = urlparse(url)
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in values.items():
        pairs[key] = "" if value is None else str(value)
    return fresh_url(urlunparse(parsed._replace(query=urlencode(pairs))))


def request_json(url, headers, body):
    data = body.encode("utf-8") if body is not None else None
    clean_headers = dict(headers)
    clean_headers.setdefault("Accept", "application/json, text/plain, */*")
    if data is not None:
        clean_headers.setdefault("Content-Type", "application/json")
    else:
        clean_headers.pop("Content-Type", None)
    clean_headers["Accept-Encoding"] = "identity"
    clean_headers.pop("Content-Length", None)
    req = Request(fresh_url(url), data=data, headers=clean_headers, method="POST" if data is not None else "GET")
    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def page_body(template_body, page_index):
    body = json.loads(template_body or "{}")
    body["page_index"] = page_index
    body.setdefault("page_size", 10)
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def detail_url(template_url, encode_record_id):
    return replace_query(
        template_url,
        pwd="",
        auth_share_id=encode_record_id,
        uni_record_share_id=encode_record_id,
        activity_uid="",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Enumerate Tencent Meeting cloud recordings from copied list/detail cURL commands."
    )
    parser.add_argument("--list-curl", required=True, help="File containing Copy as cURL for my-record-list")
    parser.add_argument("--detail-curl", required=True, help="File containing Copy as cURL for get-multi-record-info")
    parser.add_argument("-o", "--output", default="recordings_manifest.json")
    args = parser.parse_args()

    list_url, list_headers, list_body_template = parse_curl(Path(args.list_curl).read_text())
    detail_url_template, detail_headers, detail_body = parse_curl(Path(args.detail_curl).read_text())

    records = []
    page = 1
    while True:
        body = page_body(list_body_template, page)
        resp = request_json(list_url, list_headers, body)
        if resp.get("code") != 0:
            raise RuntimeError(f"list page {page} failed: {resp}")
        batch = resp.get("data", {}).get("records", [])
        if not batch:
            break
        records.extend(batch)
        total = int(resp.get("data", {}).get("total_count") or 0)
        print(f"page {page}: {len(batch)} records, collected {len(records)}/{total or '?'}")
        if total and len(records) >= total:
            break
        page += 1

    cloud_records = [r for r in records if r.get("record_type") == "cloud_record"]
    manifest = []
    for idx, record in enumerate(cloud_records, 1):
        encode_record_id = record.get("encode_record_id")
        if not encode_record_id:
            continue
        url = detail_url(detail_url_template, encode_record_id)
        try:
            detail = request_json(url, detail_headers, detail_body)
        except Exception as exc:
            print(f"detail failed for {record.get('title')}: {exc}", file=sys.stderr)
            detail = {"error": str(exc)}
        base_infos = detail.get("data", {}).get("base_infos", []) if isinstance(detail, dict) else []
        manifest.append(
            {
                "title": record.get("title"),
                "meeting_code": record.get("meeting_info", {}).get("meeting_code"),
                "start_time": record.get("start_time"),
                "uni_record_id": record.get("uni_record_id"),
                "encode_record_id": encode_record_id,
                "size": record.get("size"),
                "versions": base_infos,
                "detail_error": detail.get("error") if isinstance(detail, dict) else None,
            }
        )
        print(f"detail {idx}/{len(cloud_records)}: {record.get('title')} ({len(base_infos)} versions)")

    Path(args.output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"saved {args.output}: {len(manifest)} cloud recordings")


if __name__ == "__main__":
    main()
