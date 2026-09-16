#!/usr/bin/env python3
"""把「会议录制」目录整理到统一命名规则（dry-run 友好、可逆、不删文件）。

三类来源：
  - tencent-cdp     : manifest.json（files[].{name,size,status}）
  - tencent-public  : public_share_manifest.json（resources[].{resource_type,record_id,size}）
                      ⚠️ public 的 local_path 全指向同一名、不可信，必须用 size 配对实际文件
  - zoom            : manifest.json（files[].{kind,filename,bytes,sha256}）或无 manifest
  - tencent-export  : 历史会议客户端导出（目录名 YYYYMMDDHHMMSS-标题，无 manifest）

映射规则统一走 recording_naming.py（复用 media_filename / segment_indexes / zoom_media_filename）。

用法：
  python3 organize_recordings.py <root> --scope tencent-top [--apply] [--backup-dir <dir>]
  scope ∈ {tencent-top, tencent-history, zoom}
  不带 --apply = dry-run，只打印映射表，不动文件。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from recording_naming import (  # noqa: E402
    RESOURCE_ZH_LABELS,
    media_filename,
    normalize_resource_type,
    safe_name,
    segment_indexes,
    zoom_media_filename,
)

PICTURE_PREFIX = {"mixed": 0, "screen": 1, "speaker": 2}
LEGACY_RE = re.compile(r"(mixed|screen|speaker)(?:_(\d+))?\.mp4$", re.IGNORECASE)


# ── 目录分类 ──────────────────────────────────────────────────────────
def detect_type(folder: Path) -> str:
    """按内部文件判定来源类型（不看目录名，避免 Zoom_Recording 这类误导名）。"""
    names = {p.name for p in folder.iterdir()} if folder.is_dir() else set()
    if "public_share_manifest.json" in names:
        return "tencent-public"
    if "manifest.json" in names:
        # manifest 内部 schema 决定是腾讯 CDP 还是 Zoom
        try:
            data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            return "unknown"
        files = data.get("files") or []
        if files and isinstance(files[0], dict) and "kind" in files[0]:
            return "zoom"
        return "tencent-cdp"
    # 无 manifest：Zoom 文件特征 / 历史导出特征
    if any(n == "video.mp4" for n in names) or any(n.startswith("audio__") for n in names):
        return "zoom"
    if any("共享屏幕" in n or "说话人" in n for n in names):
        return "tencent-export"
    return "unknown"


# ── 标题提取 ──────────────────────────────────────────────────────────
def title_from_dirname(folder: Path) -> str:
    """剥掉 YYYYMMDD_HHMM_ 或 unknown_ 前缀，剩标题；剥不掉就用全名。"""
    name = folder.name
    name = re.sub(r"^\d{8}_\d{4}_", "", name)
    name = re.sub(r"^unknown_", "", name)
    return name or folder.name


def title_from_zoom_manifest(folder: Path) -> str | None:
    mf = folder / "manifest.json"
    if not mf.is_file():
        return None
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return None
    t = data.get("title")
    return safe_name(t) if t else None


# ── 腾讯 CDP ──────────────────────────────────────────────────────────
def plan_tencent_cdp(folder: Path) -> list[dict]:
    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    title = safe_name(data.get("title") or title_from_dirname(folder))
    files = data.get("files") or []
    # 解析每个文件名 → (resource_type, seg)，检测多段
    parsed = []
    has_seg = False
    for f in files:
        fn = f.get("name") or f.get("filename")  # CDP 有两种 schema：files[].name / files[].filename
        m = LEGACY_RE.match(fn or "")
        if not m:
            continue
        rt = PICTURE_PREFIX[m.group(1).lower()]
        seg = int(m.group(2)) if m.group(2) else None
        if seg is not None:
            has_seg = True
        parsed.append({"old": fn, "rt": rt, "seg": seg, "size": f.get("size")})
    # 多段模式下，无后缀的补 1（理论上不会出现）
    if has_seg:
        for p in parsed:
            if p["seg"] is None:
                p["seg"] = 1
    plans = []
    for p in parsed:
        new = media_filename(title, p["rt"], segment_index=p["seg"])
        if p["old"] == new:
            continue
        plans.append({
            "old": folder / p["old"], "new": folder / new,
            "size": p["size"], "kind": "tencent-cdp", "title": title,
            "resource_type": p["rt"], "segment": p["seg"],
        })
    return plans


# ── 腾讯公开法 ────────────────────────────────────────────────────────
def plan_tencent_public(folder: Path) -> list[dict]:
    data = json.loads((folder / "public_share_manifest.json").read_text(encoding="utf-8"))
    title = safe_name(data.get("share", {}).get("title") or title_from_dirname(folder))
    resources = data.get("resources") or []
    # 实际文件大小表（只看 mp4）
    actual = {}
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() == ".mp4":
            actual.setdefault(p.stat().st_size, p.name)
    # 段号：按 record_id 分组
    seg_map = segment_indexes(resources, "record_id")
    plans = []
    used_actual = set()
    for r in resources:
        rt = normalize_resource_type(r.get("resource_type"))
        size = r.get("size")
        seg = seg_map.get(r.get("record_id"))
        # 用 size 配对实际文件（local_path 不可信）
        actual_name = actual.get(size)
        if not actual_name or actual_name in used_actual:
            # 兜底：同 size 多文件时按 resource_type 期望名猜
            expect_legacy = {0: "mixed.mp4", 1: "screen.mp4", 2: "speaker.mp4"}.get(rt)
            if expect_legacy and expect_legacy in actual.values() and expect_legacy not in used_actual:
                actual_name = expect_legacy
            else:
                continue
        used_actual.add(actual_name)
        new = media_filename(title, rt, segment_index=seg)
        if actual_name == new:
            continue
        plans.append({
            "old": folder / actual_name, "new": folder / new,
            "size": size, "kind": "tencent-public", "title": title,
            "resource_type": rt, "segment": seg,
        })
    return plans


# ── Zoom ──────────────────────────────────────────────────────────────
def plan_zoom(folder: Path) -> list[dict]:
    title = title_from_zoom_manifest(folder) or title_from_dirname(folder)
    title = safe_name(title)
    plans = []
    for p in folder.iterdir():
        if not p.is_file():
            continue
        fn = p.name
        new = None
        if fn == "video.mp4":
            new = zoom_media_filename(title, "video")
        elif fn.startswith("audio__") and fn.endswith(".m4a"):
            lang = fn[len("audio__"):-len(".m4a")]
            new = zoom_media_filename(title, "audio", lang=lang)
        elif fn.startswith("caption") and fn.endswith(".vtt"):
            m = re.match(r"caption(?:_(rec\w+))?\.vtt$", fn)
            rec = m.group(1) if (m and m.group(1)) else None
            new = zoom_media_filename(title, "caption", rec=rec)
        elif fn == "chapter.json":
            new = zoom_media_filename(title, "chapter")
        if new and fn != new:
            plans.append({"old": p, "new": folder / new, "size": p.stat().st_size,
                          "kind": "zoom", "title": title})
    # 目录名一致性：manifest 有真实标题、且目录名标题部分不符时，改目录名
    if title_from_zoom_manifest(folder):
        m = re.match(r"^(\d{8}_\d{4}_)", folder.name)
        prefix = m.group(1) if m else ""
        cur_title_part = folder.name[len(prefix):] if prefix else folder.name
        if cur_title_part != title:
            new_dirname = f"{prefix}{title}"
            if folder.name != new_dirname:
                plans.append({"old": folder, "new": folder.parent / new_dirname,
                              "is_dir": True, "kind": "zoom", "title": title})
    return plans


# ── 历史会议客户端导出 ────────────────────────────────────────────────
EXPORT_DIR_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})-(.+)$")


def plan_tencent_export(folder: Path) -> list[dict]:
    m = EXPORT_DIR_RE.match(folder.name)
    if not m:
        return []
    y, mo, d, hh, mm, ss, raw_title = m.groups()
    title = safe_name(raw_title)
    new_dirname = f"{y}{mo}{d}_{hh}{mm}_{title}"
    plans = []
    if folder.name != new_dirname:
        plans.append({"old": folder, "new": folder.parent / new_dirname,
                      "is_dir": True, "kind": "tencent-export", "title": title})
    old_prefix = folder.name  # 文件名前缀是旧目录全名
    for p in folder.iterdir():
        if not p.is_file():
            continue
        fn = p.name
        rt = None
        if "-共享屏幕" in fn:
            rt = 1
        elif "-说话人" in fn:
            rt = 2
        if rt is None:
            continue
        new = media_filename(title, rt)
        if fn == new:
            continue
        plans.append({"old": p, "new": folder / new, "size": p.stat().st_size,
                      "kind": "tencent-export", "title": title, "resource_type": rt})
    return plans


# ── 扫描 + 计划 ───────────────────────────────────────────────────────
def scan(root: Path, scope: str) -> dict:
    """返回 {category: [(folder, type, plans)]}。scope 决定扫哪些目录。"""
    out = {"tencent": [], "zoom": [], "unknown": [], "skip": []}
    if scope in ("tencent-top", "zoom"):
        for p in sorted(root.iterdir()):
            if not p.is_dir() or p.name.startswith(".") or p.name.startswith("__"):
                continue
            if p.name.startswith("_") or p.name in {"CPTSD评估干预实操（2025）",
                                                      "腾讯会议录制下载工具包",
                                                      "历史会议_2024-2025"}:
                out["skip"].append((p, "skip(工具/备份)", []))
                continue
            if p.name.startswith("unknown_"):
                t = detect_type(p)
                out["tencent"].append((p, t, plan_tencent_public(p) if t == "tencent-public" else []))
                continue
            t = detect_type(p)
            if t in ("tencent-cdp", "tencent-public"):
                plans = plan_tencent_cdp(p) if t == "tencent-cdp" else plan_tencent_public(p)
                out["tencent"].append((p, t, plans))
            elif t == "zoom" and scope == "zoom":
                out["zoom"].append((p, t, plan_zoom(p)))
            elif t == "zoom":
                out["skip"].append((p, f"zoom(留待zoom scope)", []))
            else:
                out["unknown"].append((p, t, []))
    elif scope == "tencent-history":
        hist = root / "历史会议_2024-2025"
        if not hist.is_dir():
            return out
        for p in sorted(hist.iterdir()):
            if not p.is_dir() or p.name.startswith(".") or p.name.startswith("__"):
                continue
            t = detect_type(p)
            if t == "tencent-export":
                out["tencent"].append((p, t, plan_tencent_export(p)))
            else:
                out["unknown"].append((p, t, []))
    return out


# ── 报告 ──────────────────────────────────────────────────────────────
def report(scan_result: dict) -> int:
    total = 0
    for cat in ("tencent", "zoom"):
        for folder, t, plans in scan_result[cat]:
            if not plans:
                continue
            print(f"\n▼ [{t}] {folder.name}")
            for pl in plans:
                total += 1
                old = pl["old"].name if not pl.get("is_dir") else f"{pl['old'].name}/"
                new = pl["new"].name if not pl.get("is_dir") else f"{pl['new'].name}/"
                size = pl.get("size")
                szs = f"  ({size}B)" if size is not None else ""
                rt = pl.get("resource_type")
                rts = f" type={rt}({RESOURCE_ZH_LABELS.get(rt,'?')})" if rt is not None else ""
                seg = pl.get("segment")
                segs = f" seg={seg}" if seg is not None else ""
                print(f"    {old}  →  {new}{szs}{rts}{segs}")
    if scan_result["unknown"]:
        print(f"\n⚠ 未识别目录 {len(scan_result['unknown'])} 个:")
        for folder, t, _ in scan_result["unknown"]:
            print(f"    [{t}] {folder.name}")
    return total


# ── 执行 ──────────────────────────────────────────────────────────────
def apply_plans(scan_result: dict, backup_dir: Path) -> tuple[int, int]:
    """执行改名。先备份 manifest 原件，再改文件名，最后回写 manifest 的 local_path。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    done = skipped = 0
    for cat in ("tencent", "zoom"):
        for folder, t, plans in scan_result[cat]:
            if not plans:
                continue
            # 备份 manifest 原件
            for mf_name in ("manifest.json", "public_share_manifest.json"):
                mf = folder / mf_name
                if mf.is_file():
                    dst = backup_dir / f"{folder.name}__{mf_name}"
                    if not dst.exists():
                        shutil.copy2(mf, dst)
            # 检查目标名冲突
            new_names = [pl["new"].name for pl in plans if not pl.get("is_dir")]
            if len(new_names) != len(set(new_names)):
                print(f"  ✗ 跳过 {folder.name}: 改名后存在重名 {new_names}")
                skipped += len(plans)
                continue
            # 改文件名（先文件后目录，避免目录改名后路径失效）
            file_plans = [pl for pl in plans if not pl.get("is_dir")]
            dir_plans = [pl for pl in plans if pl.get("is_dir")]
            for pl in file_plans:
                if pl["old"].exists() and not pl["new"].exists():
                    pl["old"].rename(pl["new"])
                    done += 1
            # 回写 manifest local_path（腾讯）/ filename（Zoom）
            if cat == "tencent":
                rewrite_manifest_local_paths(folder, t, file_plans)
            elif cat == "zoom":
                rewrite_zoom_manifest(folder, file_plans)
            # 最后改目录名
            for pl in dir_plans:
                if pl["old"].exists() and not pl["new"].exists():
                    pl["old"].rename(pl["new"])
                    done += 1
    return done, skipped


def rewrite_manifest_local_paths(folder: Path, t: str, file_plans: list[dict]):
    """把 manifest 里残留的旧 local_path 更新为新文件名（仅 tencent-public 有可信 size 配对）。"""
    if t == "tencent-public":
        mf = folder / "public_share_manifest.json"
    elif t == "tencent-cdp":
        return  # CDP manifest 用 files[].name，下面单独处理
    else:
        return
    if not mf.is_file():
        return
    data = json.loads(mf.read_text(encoding="utf-8"))
    size_to_new = {pl["size"]: pl["new"].name for pl in file_plans if pl.get("size") is not None}
    changed = False
    for r in data.get("resources", []):
        size = r.get("size")
        if size in size_to_new:
            old_lp = r.get("local_path", "")
            new_lp = str(Path(old_lp).with_name(size_to_new[size])) if old_lp else str(folder / size_to_new[size])
            if r.get("local_path") != new_lp:
                r["local_path"] = new_lp
                changed = True
    if changed:
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # CDP 的 files[].name 同步
    if t == "tencent-cdp":
        pass  # handled below separately


def rewrite_zoom_manifest(folder: Path, file_plans: list[dict]):
    """回写 Zoom manifest 的 files[].filename（video.mp4 -> <标题>_video.mp4 等）。"""
    mf = folder / "manifest.json"
    if not mf.is_file():
        return
    data = json.loads(mf.read_text(encoding="utf-8"))
    old_to_new = {pl["old"].name: pl["new"].name for pl in file_plans}
    changed = False
    for f in data.get("files", []):
        if f.get("filename") in old_to_new:
            f["filename"] = old_to_new[f["filename"]]
            changed = True
    if changed:
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def rewrite_cdp_manifest(folder: Path, file_plans: list[dict]):
    mf = folder / "manifest.json"
    if not mf.is_file():
        return
    data = json.loads(mf.read_text(encoding="utf-8"))
    old_to_new = {pl["old"].name: pl["new"].name for pl in file_plans}
    changed = False
    for f in data.get("files", []):
        # 兼容 files[].name 与 files[].filename 两种 schema
        for key in ("name", "filename"):
            if f.get(key) in old_to_new:
                f[key] = old_to_new[f[key]]
                changed = True
    if changed:
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--scope", required=True,
                    choices=["tencent-top", "tencent-history", "zoom"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--only", default=None,
                    help="逗号分隔的目录名前缀，仅处理匹配的（试点用）")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    result = scan(root, args.scope)
    if args.only:
        prefixes = tuple(s.strip() for s in args.only.split(",") if s.strip())
        for cat in ("tencent", "zoom", "unknown", "skip"):
            result[cat] = [(f, t, p) for (f, t, p) in result[cat] if f.name.startswith(prefixes)]
    total = report(result)

    if args.scope == "tencent-top":
        n_t = len(result["tencent"])
        n_z = sum(1 for _, t, _ in result["skip"] if "zoom" in str(t))
        print(f"\n小结: 腾讯目录 {n_t} 个, 待改名 {total} 项; 跳过 {len(result['skip'])} (含 Zoom {n_z}); 未识别 {len(result['unknown'])}")

    if not args.apply:
        print("\n[dry-run] 未改动。加 --apply 执行（会先备份 manifest）。")
        return
    if total == 0:
        print("\n无需改名。")
        return
    backup_dir = Path(args.backup_dir) if args.backup_dir else root / f"_rename_backup_{scope_tag(args.scope)}"
    done, skipped = apply_plans(result, backup_dir)
    # CDP manifest 回写（apply_plans 内只处理 public，CDP 单独补）
    for folder, t, plans in result["tencent"]:
        if t == "tencent-cdp":
            rewrite_cdp_manifest(folder, [pl for pl in plans if not pl.get("is_dir")])
    print(f"\n✓ 完成 {done} 项改名, 跳过 {skipped} 项。备份 → {backup_dir}")


def scope_tag(scope: str) -> str:
    return {"tencent-top": "tencent_top", "tencent-history": "tencent_history",
            "zoom": "zoom"}.get(scope, scope)


if __name__ == "__main__":
    main()
