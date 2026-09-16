#!/usr/bin/env python3
"""腾讯会议 AI 纪要 / 逐字稿的公共处理：分页、合并、转文本、渲染 Markdown。

逐字稿接口 /wemeet-cloudrecording-webapi/v1/minutes/detail 的分页（2026-09 实测）：
    首页  limit=20&start_pid=0&fview=1
    翻页  pid=<上一页最后一段 pid>&fview=0
    响应 more:true 继续，more:false 结束
纪要接口 query-summary-and-note：official_template_summary（文字纪要）+
graphic_template_summary（结构化视觉纪要）。两者的内部字段未完全固定，
这里做宽松渲染，同时总是保留 raw JSON 以便重新渲染。
"""
import json
import random
import re
import string
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

MINUTES_PATH = "/wemeet-cloudrecording-webapi/v1/minutes/detail"


# ── 分页 ─────────────────────────────────────────────────────────────

def _nonce(n=8):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def refresh_signature_params(url):
    """重放页面请求时刷新 c_timestamp / c_nonce / trace-id（缺了会报 2710401）。"""
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q["c_timestamp"] = str(int(time.time() * 1000))
    q["c_nonce"] = _nonce()
    if "rnds" in q:
        q["rnds"] = q["c_nonce"]
    q["trace-id"] = "".join(random.choice("0123456789abcdef") for _ in range(32))
    return urlunparse(u._replace(query=urlencode(q)))


def next_page_url(url, last_pid):
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.pop("start_pid", None)
    q["pid"] = str(last_pid)
    q["fview"] = "0"
    return refresh_signature_params(urlunparse(u._replace(query=urlencode(q))))


def minutes_of(resp):
    if not isinstance(resp, dict):
        return {}
    for cand in (resp.get("minutes"), (resp.get("data") or {}).get("minutes") if isinstance(resp.get("data"), dict) else None):
        if isinstance(cand, dict):
            return cand
    return {}


def has_more(resp):
    for holder in (resp, resp.get("data") if isinstance(resp.get("data"), dict) else None, minutes_of(resp)):
        if isinstance(holder, dict) and "more" in holder:
            return bool(holder["more"])
    return False


def paragraphs_of(resp):
    return minutes_of(resp).get("paragraphs") or []


def last_pid(resp):
    paras = paragraphs_of(resp)
    if not paras:
        return None
    last = paras[-1]
    for key in ("pid", "paragraph_id", "id"):
        if last.get(key) not in (None, ""):
            return last[key]
    return None


def fetch_all_minutes_pages(get_json, encode_record_id, meeting_id, recording_id, max_pages=200):
    """get_json(path_with_query) -> dict；用于 browser_xhr 这类“页面内请求”函数。"""
    qs = {
        "mock": "1", "platform": "Web", "id": encode_record_id, "meeting_id": meeting_id,
        "recording_id": recording_id, "start_pid": "0", "limit": "20", "fview": "1",
        "minutes_version": "0", "return_ori": "0", "return_ori_minutes_translating": "1",
        "lang": "zh", "page_source": "record",
    }
    url = f"{MINUTES_PATH}?{urlencode(qs)}"
    pages = []
    seen = set()
    for _ in range(max_pages):
        resp = get_json(url)
        pages.append(resp)
        pid = last_pid(resp)
        if not has_more(resp) or pid is None or pid in seen:
            break
        seen.add(pid)
        u = urlparse(url)
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        q.pop("start_pid", None)
        q["pid"] = str(pid)
        q["fview"] = "0"
        url = f"{u.path}?{urlencode(q)}"
    return pages


def merge_minutes_pages(pages):
    merged = None
    seen = set()
    for resp in pages:
        m = minutes_of(resp)
        if not m:
            continue
        if merged is None:
            merged = {k: v for k, v in m.items() if k != "paragraphs"}
            merged["paragraphs"] = []
        for p in m.get("paragraphs") or []:
            key = p.get("pid") or (p.get("start_time"), p.get("end_time"))
            if key in seen:
                continue
            seen.add(key)
            merged["paragraphs"].append(p)
    if merged:
        merged["paragraphs"].sort(key=lambda p: int(p.get("start_time") or 0))
    return merged


# ── 逐字稿文本 ────────────────────────────────────────────────────────

def hms(ms):
    sec = int(ms or 0) // 1000
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def paragraph_text(para):
    text = ""
    for sent in para.get("sentences") or []:
        words = sent.get("words")
        if words:
            text += "".join(w.get("text", "") for w in words)
        else:
            text += sent.get("text", "")
    return text.strip() or str(para.get("text") or "").strip()


def speaker_name(para):
    sp = para.get("speaker") or {}
    return sp.get("user_name") or sp.get("name") or para.get("speaker_name") or "未知"


def format_transcript(minutes):
    lines = []
    for para in (minutes or {}).get("paragraphs") or []:
        text = paragraph_text(para)
        if text:
            lines.append(f"[{hms(para.get('start_time'))}] {speaker_name(para)}：{text}")
    return "\n".join(lines)


# ── 纪要 Markdown ─────────────────────────────────────────────────────

TITLE_KEYS = ("title", "topic", "subject", "heading", "name", "chapter_title", "question")
TEXT_KEYS = ("content", "text", "summary", "desc", "description", "detail", "answer", "value", "abstract")
CHILD_KEYS = ("children", "items", "list", "sections", "sub_items", "points", "details", "sub_topics",
              "todo_list", "chapters", "paragraphs", "contents", "sub_sections")
TIME_KEYS = ("start_time", "begin_time", "time", "timestamp")


def maybe_json(value):
    if isinstance(value, str):
        t = value.strip()
        if t[:1] in "[{":
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return value
    return value


def html_to_md(text):
    text = str(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<(b|strong)>(.*?)</\1>", r"**\2**", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(maybe_json(v), key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def render_node(node, level=3, out=None):
    out = [] if out is None else out
    node = maybe_json(node)
    if node in (None, "", [], {}):
        return out
    if isinstance(node, str):
        out.append(html_to_md(node))
        out.append("")
    elif isinstance(node, (int, float, bool)):
        return out
    elif isinstance(node, list):
        if all(isinstance(x, str) for x in node):
            out.extend(f"- {html_to_md(x)}" for x in node if str(x).strip())
            out.append("")
        else:
            for x in node:
                render_node(x, level, out)
    elif isinstance(node, dict):
        title = next((node[k] for k in TITLE_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
        tprefix = ""
        for k in TIME_KEYS:
            v = node.get(k)
            if isinstance(v, (int, str)) and str(v).isdigit() and int(v) < 10 ** 9:  # 会内毫秒偏移
                tprefix = f"[{hms(v)}] "
                break
        if title:
            out.append(f"{'#' * min(level, 6)} {tprefix}{html_to_md(title)}")
            out.append("")
        used = set(TITLE_KEYS)
        for k in TEXT_KEYS:
            if k in node:
                used.add(k)
                render_node(node[k], level + 1, out)
        for k in CHILD_KEYS:
            if k in node:
                used.add(k)
                render_node(node[k], level + 1, out)
        if not title and len(used) == len(TITLE_KEYS):
            # 不认识的结构：把剩余字符串/子结构都渲染出来
            for k, v in node.items():
                v = maybe_json(v)
                if isinstance(v, (dict, list)):
                    render_node(v, level, out)
                elif isinstance(v, str) and len(v) > 8 and not re.fullmatch(r"[\w\-=+/]+", v):
                    out.append(html_to_md(v))
                    out.append("")
    return out


def render_summary_md(title, summary_resp, timeline_resp=None, meta=None):
    data = summary_resp.get("data", summary_resp) if isinstance(summary_resp, dict) else summary_resp
    lines = [f"# {title} · 会议纪要", ""]
    for k, v in (meta or {}).items():
        if v:
            lines.append(f"- {k}：{v}")
    if meta:
        lines.append("")
    rendered = False
    for key, label in (("official_template_summary", "AI 会议纪要"), ("graphic_template_summary", "结构化纪要")):
        v = find_key(data, key)
        if v in (None, "", [], {}):
            continue
        body = render_node(v, level=3)
        if any(x.strip() for x in body):
            lines += [f"## {label}", ""] + body
            rendered = True
    if timeline_resp:
        tl = find_key(timeline_resp, "timeline_infos")
        if tl:
            lines += ["## 章节时间线", ""] + render_node(tl, level=3)
    if not rendered:
        lines += ["## 纪要（原始结构宽松渲染）", ""] + render_node(data, level=3)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
