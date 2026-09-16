#!/usr/bin/env python3
"""
download_zoom_share_recording.py — Zoom share-link recording downloader.

Pipeline (the path discovered by the user):
  1. fetch share page          → extract meetingId
  2. POST /rec/validate_meet_passwd with passcode
                                → returns {"status": true, ...}, sets auth cookies
  3. GET  /nws/recording/1.0/play/share-info/{meetingId}
                                → resolves the real /rec/play/... page URL
  4. fetch the play page       → extract fileId
  5. GET  /nws/recording/1.0/play/info/{fileId}?<browser-like query>
                                → returns viewMp4Url / mp4Url + multi-language m4a + vtt

Then download each media URL with the same requests session (resumable).

Usage:
    python3 download_zoom_share_recording.py 'https://us02web.zoom.us/rec/share/<...>' --passcode 'xxx'
    python3 download_zoom_share_recording.py --batch links.csv      # CSV columns: share_url,passcode
    python3 download_zoom_share_recording.py --debug ...            # dump intermediate JSON keys

Output layout (relative to --out, default ~/Downloads/会议录制):
    <YYYYMMDD>_<HHMM>_<meeting-title>/
        video.mp4
        audio__<language>.m4a
        caption.vtt
        chapter.json
        manifest.json                # title, files, sizes, sha256 — NOT sensitive
    catalog.csv                      # cumulative index across all runs
    .session/play_info_<fileId>.json # signed URLs + cookies — SENSITIVE (0600)

⚠️  .session/ contains short-lived signed URLs. Don't commit, don't share.

Dependencies: requests (yt-dlp not required).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("Missing dependency: requests.\n  pip3 install requests", file=sys.stderr)
    sys.exit(2)


sys.path.insert(0, str(Path(__file__).parent))
import linux_env  # noqa: E402

UA = linux_env.FALLBACK_UA

DEBUG = False


def dprint(*a, **kw):
    if DEBUG:
        print("DEBUG:", *a, **kw, file=sys.stderr)


def safe_name(value: str, fallback: str = "untitled", limit: int = 160) -> str:
    value = str(value or "").strip() or fallback
    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:limit].strip(" .") or fallback)


# -------------------------------------------------------------------------
# Context object
# -------------------------------------------------------------------------

@dataclass
class ZoomShare:
    share_url: str
    passcode: str
    base_origin: str = ""        # e.g. https://us02web.zoom.us
    meeting_id: str = ""
    play_url: str = ""
    file_id: str = ""
    file_id_candidates: list = field(default_factory=list)
    extra_file_ids: list = field(default_factory=list)
    title: str = ""
    play_info: dict = field(default_factory=dict)
    extra_play_infos: list = field(default_factory=list)


# -------------------------------------------------------------------------
# Pipeline steps
# -------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def step_fetch_share_page(s: requests.Session, ctx: ZoomShare) -> None:
    parsed = urllib.parse.urlparse(ctx.share_url)
    ctx.base_origin = f"{parsed.scheme}://{parsed.netloc}"
    r = s.get(ctx.share_url, allow_redirects=True)
    r.raise_for_status()
    html = r.text
    # try several plausible patterns; whichever the page exposes
    patterns = [
        r'"meetingId"\s*:\s*"([^"]+)"',
        r'name=["\']meetingId["\'][^>]*value=["\']([^"\']+)["\']',
        r'meetingId=([A-Za-z0-9+/=_\-]+)',
        r'data-meeting-id=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            ctx.meeting_id = m.group(1)
            dprint(f"meetingId pattern hit: {pat[:40]}")
            return
    # Last resort: maybe meetingId == the long token in the URL itself
    # /rec/share/<token>  (sometimes Zoom uses this token as id)
    m = re.search(r"/rec/share/([^/?#]+)", ctx.share_url)
    if m:
        ctx.meeting_id = m.group(1)
        dprint("meetingId from URL fallback")
        return
    raise RuntimeError(
        "Could not extract meetingId from share page. "
        "Run with --debug and share what's in the HTML."
    )


def step_validate_passcode(s: requests.Session, ctx: ZoomShare) -> None:
    url = f"{ctx.base_origin}/rec/validate_meet_passwd"
    payload = {
        "id": ctx.meeting_id,
        "passwd": ctx.passcode,
        "action": "viewdetailpage",
    }
    r = s.post(
        url,
        data=payload,
        headers={
            "Referer": ctx.share_url,
            "Origin": ctx.base_origin,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    r.raise_for_status()
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"validate_meet_passwd: non-JSON response: {r.text[:200]}")
    dprint("validate_meet_passwd response keys:", list(j.keys()))
    # accept several shapes: {status:true} or {result:true} or {errorCode:0}
    ok = (
        j.get("status") is True
        or j.get("result") is True
        or j.get("errorCode") == 0
    )
    if not ok:
        raise RuntimeError(f"passcode rejected: {j}")


def step_resolve_play_url(s: requests.Session, ctx: ZoomShare) -> None:
    url = f"{ctx.base_origin}/nws/recording/1.0/play/share-info/{ctx.meeting_id}"
    r = s.get(url, headers={"Referer": ctx.share_url, "Accept": "application/json"})
    r.raise_for_status()
    j = r.json()
    dprint("share-info response keys:", _all_keys(j))
    # walk JSON for any /rec/play/ URL
    play = _first_value(j, lambda v: isinstance(v, str) and "/rec/play/" in v)
    if not play:
        # named keys we've seen in the wild
        result = j.get("result") if isinstance(j.get("result"), dict) else j
        play = (
            result.get("redirectUrl")
            or result.get("playUrl")
            or result.get("url")
        )
    if not play:
        raise RuntimeError(f"share-info missing play URL. JSON keys: {_all_keys(j)}")
    if play.startswith("/"):
        play = ctx.base_origin + play
    ctx.play_url = play


def step_extract_file_id(s: requests.Session, ctx: ZoomShare) -> None:
    r = s.get(ctx.play_url, headers={"Referer": ctx.share_url})
    r.raise_for_status()
    html = r.text
    final_url = r.url
    dprint(f"play page final URL: {final_url[:120]}")
    dprint(f"play page HTML length: {len(html)}")

    candidates: list[str] = []

    def add(c: str, why: str) -> None:
        c = c.strip()
        if c and c not in candidates:
            candidates.append(c)
            dprint(f"fileId candidate ({why}): {c[:60]}{'…' if len(c) > 60 else ''}")

    patterns = [
        # JSON-style (double / single / HTML-escaped quotes)
        r'"fileId"\s*:\s*"([^"]+)"',
        r"'fileId'\s*:\s*'([^']+)'",
        r'&quot;fileId&quot;\s*:\s*&quot;([^&]+)&quot;',
        r'"fid"\s*:\s*"([^"]+)"',
        r'"recordingId"\s*:\s*"([^"]+)"',
        # JS variable / property assignment styles
        r'fileId\s*[:=]\s*["\']([A-Za-z0-9+/=_\-.]{12,})["\']',
        # URL / form / data-* attribute styles
        r'fileId=([A-Za-z0-9+/=_\-.]{12,})',
        r'data-file-id=["\']([^"\']+)["\']',
        r'data-fileid=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html):
            add(m.group(1), pat[:40])

    # URL token fallback. /rec/play/<X>.<Y> — try BOTH the full token and the part before dot.
    m_full = re.search(r"/rec/play/([^/?#]+)", final_url)
    if m_full:
        full = m_full.group(1)
        add(full, "play URL token (full)")
        if "." in full:
            add(full.split(".", 1)[0], "play URL token (before dot)")

    # If debugging, dump HTML + snippets so we can eyeball missed forms
    if DEBUG:
        dump_path = Path.cwd() / f"play_page_{ctx.meeting_id[:16]}.html"
        try:
            dump_path.write_text(html)
            dprint(f"play page HTML dumped to: {dump_path}")
        except OSError:
            pass
        snippets = []
        for needle in ("fileId", "file_id", "fid", "recordingId", "playInfo"):
            for m in re.finditer(rf"(.{{0,40}}{re.escape(needle)}.{{0,80}})", html, re.IGNORECASE):
                snippets.append(m.group(1))
                if len(snippets) >= 6:
                    break
            if len(snippets) >= 6:
                break
        for sn in snippets[:6]:
            dprint(f"snippet: {sn!r}")

    if not candidates:
        raise RuntimeError("Could not extract fileId from play page (no candidates)")
    ctx.file_id_candidates = candidates
    ctx.file_id = candidates[0]  # default to first; step_get_play_info iterates


def _envelope_ok(j: dict) -> bool:
    """Zoom's standard envelope: {status, errorCode, errorMessage, result}.
    Treat as success if status is True OR errorCode == 0 OR result is a non-empty dict."""
    if j.get("status") is True or j.get("errorCode") == 0:
        return True
    r = j.get("result")
    return isinstance(r, dict) and bool(r)


def play_info_query(ctx: ZoomShare) -> dict:
    """Build the browser-style query Zoom sends for play/info and related APIs."""
    play_query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ctx.play_url).query, keep_blank_values=True))
    parsed_share = urllib.parse.urlparse(ctx.share_url)
    params = {
        "eagerLoadZvaPages": "sidemenu.billing.plan_management",
        "accessLevel": "meeting",
        "canPlayFromShare": "true",
        "from": "share_recording_detail",
        "continueMode": "true",
        "oldStyle": "true",
        "componentName": "rec-play",
        "originRequestUrl": ctx.share_url,
        "originDomain": parsed_share.netloc,
    }
    params.update({k: v for k, v in play_query.items() if k not in params})
    return params


def play_api_url(ctx: ZoomShare, path: str, fid: str) -> str:
    query = urllib.parse.urlencode(play_info_query(ctx))
    return f"{ctx.base_origin}/nws/recording/1.0/play/{path}/{fid}?{query}"


def fetch_play_info(s: requests.Session, ctx: ZoomShare, fid: str) -> dict:
    url = play_api_url(ctx, "info", fid)
    r = s.get(url, headers={"Referer": ctx.play_url, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def step_get_play_info(s: requests.Session, ctx: ZoomShare) -> None:
    candidates = ctx.file_id_candidates or [ctx.file_id]
    last_err: str = ""
    for i, fid in enumerate(candidates):
        try:
            j = fetch_play_info(s, ctx, fid)
        except Exception as e:
            last_err = f"candidate#{i} HTTP/JSON error: {e}"
            dprint(last_err)
            continue
        if _envelope_ok(j):
            ctx.play_info = j
            ctx.file_id = fid
            dprint(f"play/info accepted candidate#{i}: {fid[:32]}…")
            break
        last_err = (
            f"candidate#{i} ({fid[:24]}…): "
            f"errorCode={j.get('errorCode')} "
            f"errorMessage={j.get('errorMessage')!r} "
            f"result={type(j.get('result')).__name__}"
        )
        dprint(last_err)
    else:
        raise RuntimeError(f"all fileId candidates rejected by play/info. last: {last_err}")

    if DEBUG:
        all_paths = _all_keys(ctx.play_info)
        dprint(f"play/info has {len(all_paths)} key paths; full tree:")
        for p in all_paths:
            dprint(f"    {p}")

    result = ctx.play_info.get("result", ctx.play_info)
    if isinstance(result, dict):
        meet = result.get("meet") if isinstance(result.get("meet"), dict) else {}
        recording = result.get("recording") if isinstance(result.get("recording"), dict) else {}
        ctx.title = (
            result.get("topic")
            or result.get("title")
            or result.get("meetingTopic")
            or meet.get("topic")
            or recording.get("displayFileName")
            or f"zoom_{ctx.meeting_id[:12]}"
        )
    else:
        ctx.title = f"zoom_{ctx.meeting_id[:12]}"


def _walk_strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _walk_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_strings(v)
    elif isinstance(o, str):
        yield o


def looks_like_file_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_\-.]{24,}", value or ""))


def add_file_id_candidate(ctx: ZoomShare, fid: str, why: str = "") -> None:
    fid = (fid or "").strip()
    if not looks_like_file_id(fid):
        return
    if fid not in ctx.file_id_candidates:
        ctx.file_id_candidates.append(fid)
        dprint(f"fileId candidate ({why or 'extra'}): {fid[:60]}{'…' if len(fid) > 60 else ''}")


def discover_separate_audio_file_ids(s: requests.Session, ctx: ZoomShare) -> None:
    """Best-effort probe for the lazy-loaded Smart Recording audio fileId list."""
    seeds = list(dict.fromkeys(ctx.file_id_candidates + [ctx.file_id]))
    for fid in seeds:
        if not fid:
            continue
        url = play_api_url(ctx, "separate-audio", fid)
        try:
            r = s.get(url, headers={"Referer": ctx.play_url, "Accept": "application/json"}, timeout=20)
            if r.status_code >= 400:
                dprint(f"separate-audio {fid[:24]}… HTTP {r.status_code}")
                continue
            payload = r.json()
        except Exception as e:
            dprint(f"separate-audio {fid[:24]}… failed: {e}")
            continue
        for value in _walk_strings(payload):
            parsed = urllib.parse.urlparse(value)
            if parsed.path:
                m = re.search(r"/play/info/([^/?#]+)", parsed.path)
                if m:
                    add_file_id_candidate(ctx, m.group(1), "separate-audio play/info URL")
                    continue
            if looks_like_file_id(value):
                add_file_id_candidate(ctx, value, "separate-audio JSON string")


def fetch_extra_play_infos(s: requests.Session, ctx: ZoomShare) -> None:
    """Fetch additional fileIds after the main video play_info has been accepted."""
    for fid in list(dict.fromkeys(ctx.extra_file_ids + ctx.file_id_candidates)):
        if not fid or fid == ctx.file_id:
            continue
        try:
            j = fetch_play_info(s, ctx, fid)
        except Exception as e:
            dprint(f"extra play/info {fid[:24]}… failed: {e}")
            continue
        if not _envelope_ok(j):
            dprint(f"extra play/info {fid[:24]}… rejected")
            continue
        result = j.get("result", j)
        if isinstance(result, dict) and result.get("interpreterAudioList"):
            dprint(f"extra play/info {fid[:24]}… contains interpreterAudioList")
        ctx.extra_play_infos.append({"file_id": fid, "play_info": j})


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _all_keys(o, prefix="", out=None):
    """Recursively collect dotted key paths of a JSON object (for debugging)."""
    if out is None:
        out = []
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{prefix}.{k}" if prefix else k
            out.append(p)
            _all_keys(v, p, out)
    elif isinstance(o, list) and o:
        _all_keys(o[0], prefix + "[0]", out)
    return out


def _first_value(o, pred):
    """DFS the JSON for the first value matching pred(v)."""
    if pred(o):
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = _first_value(v, pred)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _first_value(v, pred)
            if r is not None:
                return r
    return None


SKIP_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg")

# Common language codes Zoom uses for AI-translated captions.
# We probe each by appending &lang=<code> to ccUrl and keeping the ones that respond.
CAPTION_LANG_PROBE = [
    "zh-CN", "zh", "en-US", "en", "ru-RU", "ru",
    "es-ES", "es", "fr-FR", "fr", "de-DE", "de",
    "ja-JP", "ja", "ko-KR", "ko", "pt", "it",
]


def probe_caption_languages(s: requests.Session, base_cc_url: str, referer: str) -> list[str]:
    """For each candidate lang code, try ccUrl + &lang=<code>.
    Return the list of codes that return a non-empty WEBVTT response."""
    found: list[str] = []
    sep = "&" if "?" in base_cc_url else "?"
    for lang in CAPTION_LANG_PROBE:
        url = f"{base_cc_url}{sep}lang={lang}"
        try:
            # Use GET with a tiny stream so we see the first bytes; HEAD is unreliable here
            with s.get(url, headers={"Referer": referer}, stream=True, timeout=10) as r:
                if r.status_code != 200:
                    continue
                first = next(r.iter_content(chunk_size=128), b"")
                # Valid VTT starts with "WEBVTT" (sometimes after BOM)
                if first.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
                    found.append(lang)
                    dprint(f"caption lang found: {lang}")
        except Exception as e:
            dprint(f"caption probe {lang} failed: {e}")
    return found


def collect_media_urls(play_info: dict, base_origin: str = "") -> list[dict]:
    """Walk play_info and collect {kind, lang, url, filename} entries.

    Rules:
      - Resolve relative URLs (starting with '/') against base_origin.
      - Drop image thumbnails.
      - For 'video' kind, dedup by URL path (so mp4Url and viewMp4Url that point to
        the same file aren't both added).
    """
    out: list[dict] = []
    seen_urls: set[str] = set()
    seen_video_paths: set[str] = set()
    result = play_info.get("result", play_info)

    def add(kind: str, lang, url):
        if not isinstance(url, str) or not url:
            return
        # Recording flags often live where URLs would — skip booleans/ints upstream.
        if url.startswith("/") and base_origin:
            url = base_origin + url
        if not url.startswith("http"):
            return
        # skip thumbnails
        path_low = url.lower().split("?", 1)[0]
        if path_low.endswith(SKIP_IMAGE_EXTS):
            return
        if url in seen_urls:
            return
        # Video-level dedup by path (two URLs with same path = same file, different signatures)
        if kind == "video":
            try:
                p = urllib.parse.urlparse(url).path
                if p in seen_video_paths:
                    return
                seen_video_paths.add(p)
            except Exception:
                pass
        seen_urls.add(url)
        out.append({"kind": kind, "lang": lang, "url": url})

    if isinstance(result, dict):
        # primary mp4 — viewMp4Url comes first so its filename wins on dedup
        for key in ("viewMp4Url", "mp4Url", "videoUrl"):
            add("video", None, result.get(key))
        # audioFiles[]
        for af in result.get("audioFiles", []) or []:
            if isinstance(af, dict):
                add("audio", af.get("lang") or af.get("language"), af.get("url"))
        # Smart Recording / Playback Language interpreted audio tracks.
        for entry in result.get("interpreterAudioList", []) or []:
            if isinstance(entry, dict):
                lang = entry.get("language") or entry.get("languageText") or entry.get("icon") or "unknown"
                add("audio", lang, entry.get("audioUrl"))
        # caption / transcript file lists
        for cap in (result.get("captionFiles") or result.get("transcriptFiles") or []):
            if isinstance(cap, dict):
                kind = "transcript" if cap.get("type") == "transcript" else "caption"
                add(kind, cap.get("lang") or cap.get("language"), cap.get("url"))
        # named caption / chapter URLs (often relative paths)
        add("caption", None, result.get("transcriptUrl"))
        add("caption", None, result.get("captionUrl"))
        add("caption", None, result.get("vttUrl"))
        add("caption", None, result.get("ccUrl"))
        add("chapter", None, result.get("chapterUrl"))
        # result.recording.* — these are flags (True/False) in many recordings, not URLs.
        # Only treat as URL if the value is a string starting with http or '/'.
        rec = result.get("recording") if isinstance(result.get("recording"), dict) else {}
        for key, kind in (("videoFile", "video"), ("aslFile", "asl")):
            v = rec.get(key)
            if isinstance(v, str):
                add(kind, None, v)
        asf = rec.get("audioSeparateFile")
        if isinstance(asf, str):
            add("audio", None, asf)
        elif isinstance(asf, list):
            for item in asf:
                if isinstance(item, dict):
                    add("audio", item.get("lang") or item.get("language"), item.get("url"))
                elif isinstance(item, str):
                    add("audio", None, item)

    # Fallback walk: any string value that looks like a media URL
    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o.startswith("http"):
            low = o.lower()
            if any(ext in low for ext in (".mp4", ".m4a", ".vtt", ".srt", ".json")):
                if ".mp4" in low:
                    kind = "video"
                elif ".m4a" in low:
                    kind = "audio"
                elif "chapter" in low and ".json" in low:
                    kind = "chapter"
                else:
                    kind = "caption"
                add(kind, None, o)
    walk(result)

    # derive a friendly filename per entry
    KIND_DEFAULT_EXT = {
        "video": "mp4", "audio": "m4a", "asl": "mp4",
        "caption": "vtt", "transcript": "vtt", "chapter": "json",
    }
    for entry in out:
        path = urllib.parse.urlparse(entry["url"]).path
        base = os.path.basename(path)
        # Use original filename if it has a real extension we recognize
        ext_ok = "." in base and len(base.rsplit(".", 1)[1]) <= 5
        if ext_ok:
            entry["filename"] = base
        else:
            ext = KIND_DEFAULT_EXT.get(entry["kind"], "bin")
            stem = entry["kind"]
            if entry.get("lang"):
                stem += f"_{entry['lang']}"
            entry["filename"] = f"{stem}.{ext}"
    return out


def language_label(entry: dict) -> str:
    labels = {
        "CN": "中文-CN",
        "ZH": "中文-ZH",
        "US": "English-US",
        "EN": "English-EN",
        "RU": "Русский-RU",
    }
    lang = str(entry.get("lang") or "").strip()
    return labels.get(lang.upper(), safe_name(lang, "original", 60))


def rename_media_files(media: list[dict], title: str) -> None:
    used: set[str] = set()
    for entry in media:
        original = entry.get("filename") or f"{entry.get('kind') or 'media'}.bin"
        ext = Path(original).suffix
        if not ext:
            ext = {
                "video": ".mp4",
                "audio": ".m4a",
                "asl": ".mp4",
                "caption": ".vtt",
                "transcript": ".vtt",
                "chapter": ".json",
            }.get(entry.get("kind"), ".bin")
        kind = safe_name(entry.get("kind") or "media", "media", 30)
        parts = [kind]
        if entry.get("kind") == "audio":
            parts.append(language_label(entry))
        elif entry.get("lang"):
            parts.append(language_label(entry))
        filename = "__".join(parts) + ext
        counter = 2
        while filename in used:
            filename = "__".join(parts) + f"_{counter}{ext}"
            counter += 1
        used.add(filename)
        entry["original_filename"] = original
        entry["filename"] = filename


def output_folder(base_out: Path, ctx: ZoomShare) -> Path:
    title = safe_name(ctx.title, "zoom-recording", 120)
    # Use meeting start time if available, otherwise fall back
    start_ts = getattr(ctx, "start_time", None)
    if start_ts:
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M", _time.localtime(int(start_ts) / 1000))
    else:
        ts = "unknown"
    return base_out / f"{ts}_{title}"


def step_install_cdn_cookies(s: requests.Session, ctx: ZoomShare) -> None:
    """Some Zoom recordings require hitting nodeNmsPresig (or similar) to receive
    CloudFront signed cookies before the media URL works. Walk play_info for any
    URL whose path/query suggests a presign endpoint, GET it via the session, and
    let cookies stick to the jar.
    """
    result = ctx.play_info.get("result", {})
    if not isinstance(result, dict):
        return
    presign_urls: list[str] = []
    for key in ("nodeNmsPresig", "presignUrl", "cdnPresignUrl"):
        v = result.get(key)
        if isinstance(v, str) and v.startswith("http"):
            presign_urls.append(v)
    if not presign_urls:
        dprint("no presign URL found in play/info")
        return
    for u in presign_urls:
        host = urllib.parse.urlparse(u).netloc
        try:
            r = s.get(u, headers={"Referer": ctx.play_url}, allow_redirects=True, timeout=20)
            dprint(f"presign {host} → HTTP {r.status_code}, set {len(r.cookies)} cookies")
        except Exception as e:
            dprint(f"presign {host} failed: {e}")


def download_with_resume(s: requests.Session, url: str, dest: Path, label: str,
                         referer: Optional[str] = None) -> dict:
    """Stream-download via the requests session (cookies travel automatically).
    Re-runs resume via HTTP Range header. Returns {bytes, sha256}."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers: dict = {}
    if referer:
        headers["Referer"] = referer
    mode = "wb"
    already = 0
    if dest.exists():
        already = dest.stat().st_size
        if already > 0:
            headers["Range"] = f"bytes={already}-"
            mode = "ab"
            dprint(f"resuming {dest.name} from {already} bytes")
    print(f"  ↓ {label} → {dest.name}", flush=True)
    with s.get(url, headers=headers, stream=True, timeout=(20, 120)) as r:
        if r.status_code == 416:  # already complete
            print("    (already complete)", flush=True)
        elif r.status_code not in (200, 206):
            preview = r.text[:200] if r.text else ""
            raise RuntimeError(f"HTTP {r.status_code} for {label}: {preview}")
        else:
            total = r.headers.get("content-length")
            total_b = int(total) + already if total and r.status_code == 206 else (int(total) if total else 0)
            done = already
            last_pct = -1
            with open(dest, mode) as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total_b:
                        pct = int(done * 100 / total_b)
                        if pct != last_pct:
                            last_pct = pct
                            sys.stderr.write(
                                f"\r    {pct:3d}%  {done/(1024*1024):.1f}/{total_b/(1024*1024):.1f} MB"
                            )
                            sys.stderr.flush()
            sys.stderr.write("\n")
    size = dest.stat().st_size
    h = hashlib.sha256()
    with dest.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"bytes": size, "sha256": h.hexdigest()}


def write_manifest(out_dir: Path, ctx: ZoomShare, files: list[dict]) -> None:
    manifest = {
        "title": ctx.title,
        "meeting_id": ctx.meeting_id,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": [
            {
                "kind": f["kind"],
                "lang": f.get("lang"),
                "filename": f["filename"],
                "bytes": f["bytes"],
                "sha256": f["sha256"],
            }
            for f in files
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )


def append_catalog(catalog_path: Path, ctx: ZoomShare, out_dir: Path, files: list[dict]) -> None:
    new = not catalog_path.exists()
    with catalog_path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow([
                "downloaded_at", "title", "meeting_id",
                "kind", "lang", "filename", "bytes", "sha256", "path",
            ])
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for fi in files:
            w.writerow([
                ts, ctx.title, ctx.meeting_id,
                fi["kind"], fi.get("lang") or "",
                fi["filename"], fi["bytes"], fi["sha256"],
                str(out_dir / fi["filename"]),
            ])


def inspect_play_info(play_info: dict) -> None:
    """Non-sensitive summary of what's in play_info: URL hosts/filenames/kind only,
    NO tokens or signed query strings printed."""
    result = play_info.get("result", play_info)
    print("\n=== play_info inspection (tokens redacted) ===\n")

    if not isinstance(result, dict):
        print(f"result is not a dict (got {type(result).__name__}): {result!r}")
        return

    # 1. Walk the entire result tree and list every URL-shaped string,
    #    classified by kind, with host + filename only.
    urls_found: list[tuple[str, str, str, str]] = []  # (path, kind, host, filename)
    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif isinstance(o, str) and o.startswith("http"):
            try:
                p = urllib.parse.urlparse(o)
                fname = os.path.basename(p.path) or "(no path)"
                low = o.lower()
                params = dict(urllib.parse.parse_qsl(p.query))
                ct = params.get("response-content-type", "")
                kind = "?"
                if ".m4a" in low or "m4a" in ct or ".aac" in low:
                    kind = "audio"
                elif ".mp4" in low or "mp4" in ct:
                    kind = "video"
                elif ".vtt" in low or ".srt" in low or "text" in ct:
                    kind = "caption"
                elif ".jpg" in low or ".jpeg" in low or ".png" in low or ".webp" in low or "image" in ct:
                    kind = "image"
                elif ".json" in low:
                    kind = "json"
                urls_found.append((path, kind, p.netloc, fname[:60]))
            except Exception:
                pass
    walk(result)

    print(f"[URLs found in play_info]   total={len(urls_found)}")
    if not urls_found:
        print("  (none)")
    else:
        # widths
        w_path = max(len(u[0]) for u in urls_found)
        w_kind = max(len(u[1]) for u in urls_found)
        w_host = max(len(u[2]) for u in urls_found)
        for path, kind, host, fname in urls_found:
            print(f"  {path:<{w_path}}  kind={kind:<{w_kind}}  host={host:<{w_host}}  file={fname}")

    # 2. Top-level result.* fields whose name suggests media — show types
    print("\n[result.* top-level media-ish fields]")
    name_re = re.compile(r"(audio|video|caption|chapter|transcript|asl|cc|recording|view|file|url|track|lang)", re.I)
    for k, v in result.items():
        if not name_re.search(k):
            continue
        desc = _describe(v)
        print(f"  {k}: {desc}")

    # 3. Drill into result.recording.*
    rec = result.get("recording")
    if isinstance(rec, dict):
        print("\n[result.recording.*]")
        for k, v in rec.items():
            print(f"  {k}: {_describe(v)}")


def _describe(v) -> str:
    """Render a value as a short non-sensitive string for inspection output."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return f"bool: {v}"
    if isinstance(v, (int, float)):
        return f"{type(v).__name__}: {v}"
    if isinstance(v, str):
        if v.startswith("http"):
            return f"URL"  # already enumerated above
        if len(v) > 60:
            return f"str[{len(v)}]: {v[:24]!r}…"
        return f"str: {v!r}"
    if isinstance(v, list):
        return f"list[{len(v)}]" + (f"  first_kind={type(v[0]).__name__}" if v else "")
    if isinstance(v, dict):
        return f"dict[{len(v)}]: keys={list(v.keys())}"
    return f"{type(v).__name__}: {v!r}"


def stash_play_info(session_dir: Path, ctx: ZoomShare) -> Path:
    """Persist play_info JSON for retry/debug. Treated as sensitive (0600)."""
    session_dir.mkdir(parents=True, exist_ok=True)
    p = session_dir / f"play_info_{ctx.file_id}.json"
    p.write_text(json.dumps(ctx.play_info, ensure_ascii=False, indent=2))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def stash_extra_play_infos(session_dir: Path, ctx: ZoomShare) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    for item in ctx.extra_play_infos:
        fid = item.get("file_id") or "unknown"
        p = session_dir / f"play_info_{fid}.json"
        p.write_text(json.dumps(item["play_info"], ensure_ascii=False, indent=2))
        try:
            p.chmod(0o600)
        except OSError:
            pass


# -------------------------------------------------------------------------
# Driver
# -------------------------------------------------------------------------

def run_one(
    share_url: str,
    passcode: str,
    base_out: Path,
    inspect_only: bool = False,
    extra_file_ids: Optional[list[str]] = None,
) -> None:
    s = make_session()
    ctx = ZoomShare(share_url=share_url, passcode=passcode, extra_file_ids=extra_file_ids or [])

    print("[1/5] fetch share page", flush=True)
    step_fetch_share_page(s, ctx)
    print(f"      meetingId: {ctx.meeting_id[:24]}…")

    print("[2/5] validate passcode", flush=True)
    step_validate_passcode(s, ctx)

    print("[3/5] resolve play URL", flush=True)
    step_resolve_play_url(s, ctx)

    print("[4/5] extract fileId", flush=True)
    step_extract_file_id(s, ctx)
    for fid in ctx.extra_file_ids:
        add_file_id_candidate(ctx, fid, "CLI --extra-file-id")
    discover_separate_audio_file_ids(s, ctx)
    print(f"      fileId: {ctx.file_id[:24]}…")

    print("[5/5] fetch play/info", flush=True)
    step_get_play_info(s, ctx)
    fetch_extra_play_infos(s, ctx)

    # Stash play_info FIRST so it's available for inspection even if extraction fails
    sess_path = stash_play_info(base_out / ".session", ctx)
    stash_extra_play_infos(base_out / ".session", ctx)
    dprint(f"play_info stashed at {sess_path}")

    # --inspect: dump structure (no tokens) and stop before downloading
    if inspect_only:
        inspect_play_info(ctx.play_info)
        for item in ctx.extra_play_infos:
            print(f"\n=== extra play_info: {item['file_id'][:24]}… ===")
            inspect_play_info(item["play_info"])
        return

    # Some recordings require GET'ing a presign endpoint to install CDN cookies
    print("      install CDN cookies (presign)", flush=True)
    step_install_cdn_cookies(s, ctx)

    media = []
    seen_media_keys: set[tuple[str, str, str]] = set()
    for info in [ctx.play_info] + [item["play_info"] for item in ctx.extra_play_infos]:
        for item in collect_media_urls(info, base_origin=ctx.base_origin):
            parsed = urllib.parse.urlparse(item["url"])
            key = (item["kind"], item.get("lang") or "", parsed.path)
            if key in seen_media_keys:
                continue
            seen_media_keys.add(key)
            media.append(item)
    summary = ", ".join(
        f"{m['kind']}{':' + (m.get('lang') or '') if m.get('lang') else ''}"
        for m in media
    )
    print(f"      → {len(media)} media files: {summary}")
    if not media:
        raise RuntimeError(
            f"play/info returned no recognizable media URLs. "
            f"Inspect: {sess_path}"
        )
    rename_media_files(media, ctx.title)

    out_dir = output_folder(base_out, ctx)
    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    for m in media:
        try:
            info = download_with_resume(
                s, m["url"], out_dir / m["filename"],
                f"{m['kind']}/{m.get('lang') or '-'}",
                referer=ctx.play_url,
            )
        except Exception as e:
            print(f"    !! skip {m['kind']}/{m.get('lang') or '-'}: {e}", file=sys.stderr)
            continue
        files.append({**m, **info})

    if not files:
        raise RuntimeError("no files downloaded successfully")

    write_manifest(out_dir, ctx, files)
    append_catalog(base_out / "catalog.csv", ctx, out_dir, files)

    total_mb = sum(f["bytes"] for f in files) / (1024 * 1024)
    print(f"\n✓ done — {len(files)} files, {total_mb:.1f} MB → {out_dir}")


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="Zoom share-link recording downloader")
    ap.add_argument("share_url", nargs="?", help="Zoom share URL")
    ap.add_argument("--passcode", help="Recording passcode")
    ap.add_argument("--passcode-env", help="Read recording passcode from this environment variable")
    ap.add_argument("--batch", help="CSV file with columns: share_url,passcode")
    ap.add_argument("--out", default=str(linux_env.DOWNLOAD_ROOT), help="Output base directory (default: ~/Downloads/会议录制)")
    ap.add_argument(
        "--extra-file-id",
        action="append",
        default=[],
        help="Additional Zoom play/info fileId to fetch, repeatable. Useful for Playback Language audio.",
    )
    ap.add_argument("--debug", action="store_true", help="Dump JSON keys at each step")
    ap.add_argument("--inspect", action="store_true",
                    help="Run pipeline through play/info, print non-sensitive structure summary, do NOT download")
    args = ap.parse_args()

    DEBUG = args.debug
    base_out = Path(args.out).resolve()

    if args.batch:
        with open(args.batch, newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                url = (row.get("share_url") or "").strip()
                pwd = (row.get("passcode") or "").strip()
                if not url or not pwd:
                    continue
                try:
                    extra_ids = [
                        value.strip()
                        for value in (row.get("extra_file_id") or row.get("extra_file_ids") or "").split(",")
                        if value.strip()
                    ] or args.extra_file_id
                    run_one(url, pwd, base_out, inspect_only=args.inspect, extra_file_ids=extra_ids)
                except Exception as e:
                    print(f"!! failed for {url[:60]}: {e}", file=sys.stderr)
        return

    passcode = args.passcode
    if args.passcode_env:
        passcode = os.environ.get(args.passcode_env)
        if not passcode:
            ap.error(f"environment variable {args.passcode_env} is empty or not set")

    if not args.share_url or not passcode:
        ap.error("provide either share_url + --passcode/--passcode-env, or --batch <file>")

    run_one(args.share_url, passcode, base_out, inspect_only=args.inspect, extra_file_ids=args.extra_file_id)


if __name__ == "__main__":
    main()
