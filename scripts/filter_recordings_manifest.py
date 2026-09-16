#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def main():
    parser = argparse.ArgumentParser(description="Filter Tencent Meeting recordings_manifest.json before resolving downloads.")
    parser.add_argument("--input", default="recordings_manifest.json")
    parser.add_argument("--output", default="recordings_manifest.filtered.json")
    parser.add_argument("--latest", type=int, help="Keep the newest/top N records in current manifest order")
    parser.add_argument("--title-contains", action="append", default=[], help="Keep records whose title contains this text")
    parser.add_argument("--start-from", help="Keep records whose start_time is >= this unix milliseconds value")
    parser.add_argument("--start-to", help="Keep records whose start_time is <= this unix milliseconds value")
    parser.add_argument("--stream-type", action="append", default=[], help="Keep only version stream types, e.g. 1 or 2")
    args = parser.parse_args()

    records = json.loads(Path(args.input).read_text())

    if args.title_contains:
        needles = [n.casefold() for n in args.title_contains]
        records = [r for r in records if any(n in str(r.get("title") or "").casefold() for n in needles)]

    if args.start_from:
        start_from = as_int(args.start_from)
        records = [r for r in records if as_int(r.get("start_time")) >= start_from]

    if args.start_to:
        start_to = as_int(args.start_to)
        records = [r for r in records if as_int(r.get("start_time")) <= start_to]

    if args.latest is not None:
        records = records[: args.latest]

    if args.stream_type:
        wanted = {str(v) for v in args.stream_type}
        filtered = []
        for record in records:
            copy = dict(record)
            copy["versions"] = [v for v in record.get("versions", []) if str(v.get("stream_type")) in wanted]
            if copy["versions"]:
                filtered.append(copy)
        records = filtered

    Path(args.output).write_text(json.dumps(records, ensure_ascii=False, indent=2))
    versions = sum(len(r.get("versions", [])) for r in records)
    print(f"saved {args.output}: {len(records)} recordings, {versions} versions")


if __name__ == "__main__":
    main()
