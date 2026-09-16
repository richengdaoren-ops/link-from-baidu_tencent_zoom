#!/usr/bin/env python3
"""下载后校验一个会议目录（Ubuntu 实战里的验收标准固化）：

  - 无 .part 残留
  - manifest.json 里每个视频文件的本地大小 == API 返回 size（逐字节）
  - ffprobe 读得出编码/分辨率/时长（有 ffprobe 时）
  - 转写.txt 的最后时间戳落在视频时长内且接近结尾（覆盖率）

用法：python3 verify_meeting_dir.py '<会议目录>' [...更多目录]
退出码：0 全部通过；1 有问题
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def ffprobe(path):
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=60,
        ).stdout
        return json.loads(out)
    except Exception:
        return None


def hms_to_s(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def fmt(sec):
    sec = int(sec)
    return f"{sec // 60}分{sec % 60:02d}秒"


def verify(d):
    d = Path(d)
    problems, notes = [], []
    parts = list(d.glob("*.part"))
    if parts:
        problems.append(f".part 残留: {[p.name for p in parts]}")
    mf = d / "manifest.json"
    manifest = json.loads(mf.read_text()) if mf.exists() else {}
    if not manifest:
        problems.append("缺 manifest.json")
    duration = None
    media_count = 0
    for f in manifest.get("files", []):
        name = f.get("name") or f.get("filename") or ""
        p = Path(f.get("local_path") or d / name)
        if not p.exists():
            p = d / Path(f.get("name", "")).name
        if not p.exists():
            problems.append(f"文件不存在: {name}")
            continue
        is_media = p.suffix.lower() in {".mp4", ".m4a", ".webm", ".mov", ".mkv"}
        if not is_media:
            notes.append(f"{p.name}: {p.stat().st_size:,} B")
            continue
        media_count += 1
        size = p.stat().st_size
        exp = int(f.get("size") or 0)
        if exp and size != exp:
            problems.append(f"{p.name}: 大小 {size} ≠ API {exp}")
        else:
            notes.append(f"{p.name}: {size:,} B{'（与 API 一致）' if exp else ''}")
        info = ffprobe(p)
        if info is None:
            problems.append(f"{p.name}: ffprobe 失败")
        elif info:
            streams = info.get("streams", [])
            v = next((s for s in streams if s.get("codec_type") == "video"), {})
            a = next((s for s in streams if s.get("codec_type") == "audio"), {})
            dur = float((info.get("format") or {}).get("duration") or 0)
            duration = max(duration or 0, dur)
            notes.append(f"  ffprobe: {v.get('codec_name')} {v.get('width')}x{v.get('height')} + {a.get('codec_name')}, {fmt(dur)}")
            if not v:
                problems.append(f"{p.name}: ffprobe 读不到视频流")
    if not media_count:
        problems.append("manifest 没有有效媒体文件")
    tx = d / "转写.txt"
    if tx.exists():
        stamps = re.findall(r"^\[(\d\d:\d\d:\d\d)\]", tx.read_text(encoding="utf-8"), flags=re.M)
        if stamps:
            first, last = hms_to_s(stamps[0]), hms_to_s(stamps[-1])
            notes.append(f"转写: {len(stamps)} 段，覆盖 {stamps[0]}–{stamps[-1]}")
            if duration:
                if last > duration + 5:
                    problems.append("转写时间戳超出视频时长（可能不是同一场）")
                elif duration - last > max(300, duration * 0.1):
                    problems.append(f"转写结束于 {stamps[-1]}，距视频结尾 {fmt(duration - last)}，可能没翻完页")
        minutes = manifest.get("minutes", {}).get("转写.txt")
        if isinstance(minutes, dict) and minutes.get("complete") is False:
            problems.append("逐字稿分页未完成（manifest minutes.complete=false）")
    else:
        notes.append("无 转写.txt")
    if (d / "会议纪要.md").exists():
        notes.append(f"会议纪要.md: {(d / '会议纪要.md').stat().st_size:,} B")
    print(f"\n# {d.name}")
    for n in notes:
        print(f"  {n}")
    for p in problems:
        print(f"  ✘ {p}")
    print("  ✔ 通过" if not problems else f"  共 {len(problems)} 个问题")
    return not problems


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    results = [verify(x) for x in sys.argv[1:]]
    sys.exit(0 if all(results) else 1)
