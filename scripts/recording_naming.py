#!/usr/bin/env python3
"""Shared naming and legacy-path compatibility for Tencent Meeting media."""
import re
from pathlib import Path

RESOURCE_LABELS = {0: "mixed", 1: "screen", 2: "speaker"}
RESOURCE_ZH_LABELS = {0: "混合画面", 1: "共享屏幕", 2: "讲者摄像头"}


def safe_name(value, fallback="未命名会议", max_length=180):
    value = str(value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_length] or fallback


def normalize_resource_type(resource_type):
    if isinstance(resource_type, str):
        normalized = resource_type.strip().lower()
        for key, label in RESOURCE_LABELS.items():
            if normalized == label:
                return key
    try:
        return int(resource_type)
    except (TypeError, ValueError):
        return resource_type


def segment_indexes(items, recording_id_key="recording_id"):
    """Map recording IDs to stable 1-based segment indexes.

    Different streams from the same recording ID belong to the same segment.
    A single recording needs no segment suffix.
    """
    ids = []
    for item in items:
        recording_id = item.get(recording_id_key)
        if recording_id not in ids:
            ids.append(recording_id)
    if len(ids) <= 1:
        return {recording_id: None for recording_id in ids}
    return {recording_id: index for index, recording_id in enumerate(ids, 1)}


def media_filename(title, resource_type, extension=".mp4", segment_index=None):
    resource_type = normalize_resource_type(resource_type)
    label = RESOURCE_ZH_LABELS.get(resource_type, f"类型{resource_type}")
    title = safe_name(title)
    extension = extension if str(extension).startswith(".") else f".{extension}"
    segment = f"_第{int(segment_index):02d}段" if segment_index is not None else ""
    return f"{title}{segment}_{label}{extension}"


def legacy_media_filename(resource_type, extension=".mp4", segment_index=None):
    resource_type = normalize_resource_type(resource_type)
    label = RESOURCE_LABELS.get(resource_type, f"type_{resource_type}")
    extension = extension if str(extension).startswith(".") else f".{extension}"
    segment = f"_{int(segment_index)}" if segment_index is not None else ""
    return f"{label}{segment}{extension}"


def candidate_media_paths(folder, title, resource_type, extension=".mp4", segment_index=None):
    folder = Path(folder)
    return [
        folder / media_filename(title, resource_type, extension, segment_index),
        folder / legacy_media_filename(resource_type, extension, segment_index),
    ]


def path_is_complete(path, expected_size=None, tolerance=1.0):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    if not expected_size:
        return True
    expected = int(expected_size)
    return path.stat().st_size >= expected * float(tolerance)


def find_existing_complete_path(
    folder,
    title,
    resource_type,
    expected_size=None,
    extension=".mp4",
    segment_index=None,
    tolerance=1.0,
):
    for path in candidate_media_paths(folder, title, resource_type, extension, segment_index):
        if path_is_complete(path, expected_size, tolerance):
            return path
    return None


# ── Zoom naming ──────────────────────────────────────────────────────

def _with_ext(stem, extension, default):
    extension = extension if (extension and str(extension).startswith(".")) else default
    return f"{stem}{extension}"


def zoom_media_filename(title, kind, lang=None, rec=None, extension=None):
    """Build a Zoom media filename prefixed with the full meeting title.

    kind ∈ {"video","audio","caption","chapter"}.
    """
    title = safe_name(title)
    if kind == "video":
        return _with_ext(f"{title}_video", extension, ".mp4")
    if kind == "audio":
        if not lang:
            raise ValueError("Zoom audio filename requires a lang, e.g. '中文-CN'")
        return _with_ext(f"{title}_audio__{lang}", extension, ".m4a")
    if kind == "caption":
        rec_part = f"_{rec}" if rec else ""
        return _with_ext(f"{title}_caption{rec_part}", extension, ".vtt")
    if kind == "chapter":
        return _with_ext(f"{title}_chapter", extension, ".json")
    raise ValueError(f"unknown Zoom media kind: {kind}")


def zoom_legacy_filename(kind, lang=None, rec=None, extension=None):
    """Legacy short Zoom filename (video.mp4 / audio__lang.m4a / caption_recN.vtt)."""
    if kind == "video":
        return _with_ext("video", extension, ".mp4")
    if kind == "audio":
        if not lang:
            raise ValueError("Zoom audio legacy filename requires a lang")
        return _with_ext(f"audio__{lang}", extension, ".m4a")
    if kind == "caption":
        rec_part = f"_{rec}" if rec else ""
        return _with_ext(f"caption{rec_part}", extension, ".vtt")
    if kind == "chapter":
        return _with_ext("chapter", extension, ".json")
    raise ValueError(f"unknown Zoom media kind: {kind}")
