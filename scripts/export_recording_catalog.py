#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path

from recording_naming import path_is_complete
from resolve_and_download_recordings import DEFAULT_MY_RECORDS_OUTPUT_DIR, existing_local_path, local_paths


CATALOG_CSV = "腾讯会议录制目录.csv"
CATALOG_MARKDOWN = "腾讯会议录制目录.md"
CATALOG_JSON = "腾讯会议录制目录.json"


def format_size(size):
    try:
        value = int(size)
    except Exception:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return str(value)


def format_duration(milliseconds):
    try:
        seconds = int(milliseconds) // 1000
    except Exception:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_start(milliseconds):
    try:
        import datetime as dt

        return dt.datetime.fromtimestamp(int(milliseconds) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def rows_from_payload(payload, output_dir):
    rows = []
    for record_index, record in enumerate(payload.get("records", []), 1):
        versions = record.get("versions", [])
        for version_index, version in enumerate(versions, 1):
            _, expected_path = local_paths(record, version, output_dir)
            path = existing_local_path(record, version, output_dir, tolerance=0.95) or expected_path
            rows.append(
                {
                    "序号": len(rows) + 1,
                    "录制序号": record_index,
                    "版本序号": version_index,
                    "标题": record.get("title") or "",
                    "开始时间": format_start(record.get("start_time")),
                    "会议号": record.get("meeting_code") or "",
                    "版本类型": version.get("stream_label") or "",
                    "时长": format_duration(version.get("duration")),
                    "大小": format_size(version.get("size")),
                    "字节数": version.get("size") or "",
                    "本地文件夹": str(path.parent),
                    "本地文件名": path.name,
                    "本地完整路径": str(path),
                    "是否已下载完整": "是" if path_is_complete(path, version.get("size"), tolerance=0.95) else "否",
                    "录制ID": record.get("uni_record_id") or "",
                    "分享ID": record.get("encode_record_id") or "",
                    "版本Recording ID": version.get("recording_id") or "",
                }
            )
    return rows


def write_csv(rows, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    headers = ["序号", "标题", "开始时间", "版本类型", "时长", "大小", "是否已下载完整", "本地文件名"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        values = [str(row.get(header, "")).replace("|", "\\|") for header in headers]
        lines.append("| " + " | ".join(values) + " |")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Export a tidy catalog for Tencent Meeting recordings.")
    parser.add_argument("--resolved", default="download_urls_manifest.json")
    parser.add_argument("--output-dir", default=DEFAULT_MY_RECORDS_OUTPUT_DIR)
    parser.add_argument("--csv", default="")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.resolved).read_text())
    rows = rows_from_payload(payload, args.output_dir)
    output_dir = Path(args.output_dir)
    csv_path = Path(args.csv) if args.csv else output_dir / CATALOG_CSV
    markdown_path = Path(args.markdown) if args.markdown else output_dir / CATALOG_MARKDOWN
    json_path = Path(args.json) if args.json else output_dir / CATALOG_JSON
    write_csv(rows, csv_path)
    write_markdown(rows, markdown_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported {len(rows)} rows")
    print(csv_path)
    print(markdown_path)
    print(json_path)


if __name__ == "__main__":
    main()
