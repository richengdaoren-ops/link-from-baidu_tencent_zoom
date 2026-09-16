#!/usr/bin/env python3
"""导出腾讯会议分享回放的 AI 会议纪要 + 逐字稿（Linux / 原生 CDP）。

做法（2026-09 Ubuntu 实战验证的路径）：
  不在外部重放接口（缺 c_timestamp/c_nonce/trace-id 等签名参数会报 2710401），
  而是让已登录的页面自己发请求，用 CDP Network.getResponseBody 取响应体。
  逐字稿翻页：先用页面原请求（刷新签名参数）在页面内 fetch 翻页；失败再滚动转写面板
  触发页面自己加载下一页。

产物（写入会议目录）：
  会议纪要.md / 会议纪要.raw.json / 转写.txt（[HH:MM:SS] 说话人：内容）/ 转写.raw.json
  并在 manifest.json 里追加 minutes 字段。

用法：
  python3 export_share_minutes_via_cdp.py 'https://meeting.tencent.com/cw/XXXX'
  python3 export_share_minutes_via_cdp.py --meeting-dir '<已下载的会议目录>' 'https://...'
  （下载视频时也可以直接 download_share_recordings_via_cdp.py --with-minutes）
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import cdp_client  # noqa: E402
import download_my_recordings as base  # noqa: E402
import tencent_minutes as tm  # noqa: E402

BASE_URL = "https://meeting.tencent.com"
WATCH = {
    "summary": "query-summary-and-note",
    "minutes": "minutes/detail",
    "timeline": "query-timeline",
}
# 点击这些文字的 tab 以触发懒加载（找不到就忽略）
TAB_TEXTS = ["纪要", "智能纪要", "AI 纪要", "AI纪要", "会议纪要", "转写", "文字转写", "逐字稿", "章节"]

CLICK_TABS_JS = """
(function(texts){
  var hit = [];
  var els = Array.from(document.querySelectorAll('div,span,a,button,li'));
  texts.forEach(function(t){
    var el = els.find(function(e){ return e.children.length <= 1 && (e.innerText||'').trim() === t; });
    if (el) { el.click(); hit.push(t); }
  });
  return hit;
})(%s)
"""

SCROLL_JS = """
(function(){
  var n = 0;
  Array.from(document.querySelectorAll('*')).forEach(function(e){
    var st = getComputedStyle(e);
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 50) {
      e.scrollTop = e.scrollHeight; e.dispatchEvent(new Event('scroll')); n++;
    }
  });
  window.scrollTo(0, document.body.scrollHeight);
  return n;
})()
"""

FETCH_JS = """
(async function(url, headers){
  try {
    var r = await fetch(url, {credentials: 'include', headers: headers});
    return JSON.stringify({status: r.status, body: await r.text()});
  } catch (e) { return JSON.stringify({error: String(e)}); }
})(%s, %s)
"""

SAFE_HEADER_SKIP = {"cookie", "host", "referer", "user-agent", "accept-encoding", "content-length",
                    "connection", "origin"}


class Capture:
    def __init__(self, session):
        self.s = session
        self.req = {}       # requestId -> {kind, url, headers}
        self.done = []      # (kind, url, body_json)
        self.fetched = set()

    def _match(self, url):
        for kind, needle in WATCH.items():
            if needle in url:
                return kind
        return None

    def drain(self):
        events, self.s.events = self.s.events, []
        for method, p in events:
            if method == "Network.requestWillBeSent":
                url = p.get("request", {}).get("url", "")
                kind = self._match(url)
                if kind:
                    self.req[p["requestId"]] = {"kind": kind, "url": url, "headers": p["request"].get("headers", {})}
            elif method == "Network.loadingFinished":
                rid = p.get("requestId")
                if rid in self.req and rid not in self.fetched:
                    self.fetched.add(rid)
                    try:
                        body = self.s.send("Network.getResponseBody", {"requestId": rid})
                        text = body.get("body", "")
                        if body.get("base64Encoded"):
                            import base64

                            text = base64.b64decode(text).decode("utf-8", "replace")
                        data = json.loads(text)
                    except Exception as exc:
                        print(f"  getResponseBody 失败 {self.req[rid]['kind']}: {exc}")
                        continue
                    info = self.req[rid]
                    self.done.append((info["kind"], info["url"], data, info["headers"]))

    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.s.pump(min(1.0, end - time.time()))
            self.drain()

    def of(self, kind):
        return [(u, d, h) for k, u, d, h in self.done if k == kind]


def page_fetch_json(session, url, headers):
    safe = {k: v for k, v in (headers or {}).items() if k.lower() not in SAFE_HEADER_SKIP and not k.startswith(":")}
    raw = session.evaluate(FETCH_JS % (json.dumps(url), json.dumps(safe)), timeout=60)
    res = json.loads(raw or "{}")
    if "error" in res:
        raise RuntimeError(res["error"])
    data = json.loads(res.get("body") or "{}")
    return data


def api_ok(data):
    code = data.get("code", data.get("ret", 0)) if isinstance(data, dict) else -1
    return code in (0, "0", None)


def collect_minutes(cap, session):
    firsts = cap.of("minutes")
    if not firsts:
        return []
    # 取 fview=1 的首页（若有多个取最后一次）
    first_url, first_data, first_headers = next(
        ((u, d, h) for u, d, h in reversed(firsts) if "fview=1" in u), firsts[-1]
    )
    pages = [first_data]
    seen = set()
    url, cur = first_url, first_data
    mode = "fetch"
    while tm.has_more(cur):
        pid = tm.last_pid(cur)
        if pid is None or pid in seen:
            break
        seen.add(pid)
        nxt = None
        if mode == "fetch":
            try:
                cand_url = tm.next_page_url(url, pid)
                cand = page_fetch_json(session, cand_url, first_headers)
                if api_ok(cand) and tm.paragraphs_of(cand):
                    nxt, url = cand, cand_url
                else:
                    print(f"  页面内翻页返回 code={cand.get('code')}，改用滚动加载")
                    mode = "scroll"
            except Exception as exc:
                print(f"  页面内翻页失败（{exc}），改用滚动加载")
                mode = "scroll"
        if mode == "scroll":
            before = len(cap.of("minutes"))
            for _ in range(6):
                session.evaluate(SCROLL_JS)
                cap.wait(2)
                if len(cap.of("minutes")) > before:
                    break
            new = [d for _, d, _ in cap.of("minutes")[before:] if tm.paragraphs_of(d)]
            if not new:
                print("  滚动也没有加载出新页，逐字稿可能不完整")
                break
            nxt = new[-1]
        pages.append(nxt)
        cur = nxt
    return pages


def export_for_share(target_id, code, meeting_dir, title=None, wait_seconds=12):
    meeting_dir = Path(meeting_dir)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    browser = cdp_client.default_browser(base.CDP_PORT)
    s = browser.session(target_id)
    s.send("Network.enable", {"maxTotalBufferSize": 200_000_000, "maxResourceBufferSize": 50_000_000})
    s.send("Page.enable")
    s.events.clear()
    cap = Capture(s)
    s.send("Page.navigate", {"url": f"{BASE_URL}/cw/{code}"})
    cap.wait(wait_seconds)
    try:
        hit = s.evaluate(CLICK_TABS_JS % json.dumps(TAB_TEXTS))
        if hit:
            cap.wait(5)
    except cdp_client.CDPError:
        pass
    if not cap.of("minutes"):
        s.evaluate(SCROLL_JS)
        cap.wait(5)

    title = title or meeting_dir.name.split("_", 2)[-1]
    result = {}

    summaries = [d for _, d, _ in cap.of("summary") if api_ok(d)]
    timelines = [d for _, d, _ in cap.of("timeline") if api_ok(d)]
    if summaries:
        summary = summaries[-1]
        (meeting_dir / "会议纪要.raw.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        md = tm.render_summary_md(title, summary, timelines[-1] if timelines else None)
        (meeting_dir / "会议纪要.md").write_text(md, encoding="utf-8")
        result["会议纪要.md"] = len(md.encode())
    else:
        print("  没有抓到 query-summary-and-note（可能分享者未生成/未开放 AI 纪要）")

    pages = collect_minutes(cap, s)
    merged = tm.merge_minutes_pages(pages) if pages else None
    if merged and merged.get("paragraphs"):
        (meeting_dir / "转写.raw.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        txt = tm.format_transcript(merged)
        (meeting_dir / "转写.txt").write_text(txt + "\n", encoding="utf-8")
        paras = merged["paragraphs"]
        result["转写.txt"] = {
            "pages": len(pages),
            "paragraphs": len(paras),
            "range": f"{tm.hms(paras[0].get('start_time'))}–{tm.hms(paras[-1].get('start_time'))}",
            "complete": not tm.has_more(pages[-1]),
        }
    else:
        print("  没有抓到 minutes/detail（可能没有转写或未开放）")

    mf = meeting_dir / "manifest.json"
    try:
        manifest = json.loads(mf.read_text()) if mf.exists() else {}
    except json.JSONDecodeError:
        manifest = {}
    manifest["minutes"] = {**result, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    import download_share_recordings_via_cdp as share

    ap = argparse.ArgumentParser(description="导出腾讯会议分享回放的 AI 纪要与逐字稿")
    ap.add_argument("url")
    ap.add_argument("--meeting-dir", help="已有会议目录；不传则按 时间_标题 规则在根目录下定位/创建")
    ap.add_argument("--output-dir", default=str(base.OUTPUT_DIR))
    ap.add_argument("--cdp-port", type=int, default=base.linux_env.CDP_PORT)
    ap.add_argument("--wait", type=int, default=12, help="页面加载后等待抓包的秒数")
    a = ap.parse_args()
    base.CDP_PORT = a.cdp_port
    base.CDP_MODE = "native"
    code = urlparse(a.url.strip()).path.strip("/").split("/")[-1]

    browser = cdp_client.default_browser(a.cdp_port)
    tab = browser.open(f"{BASE_URL}/cw/{code}")
    try:
        time.sleep(3)
        title = None
        if a.meeting_dir:
            meeting_dir = Path(a.meeting_dir)
        else:
            sh = share.resolve_share(tab["id"], code)
            detail = share.fetch_common_record_info(tab["id"], sh)
            info = detail.get("meeting_info") or {}
            title = base.safe_name(
                share.maybe_decode_title(info.get("origin_subject"))
                or share.maybe_decode_title(info.get("subject")) or code
            )
            Path(a.output_dir).mkdir(parents=True, exist_ok=True)
            meeting_dir = share.find_meeting_dir(a.output_dir, title, share.extract_start_time(detail))
        print(f"{code} -> {meeting_dir}")
        res = export_for_share(tab["id"], code, meeting_dir, title=title, wait_seconds=a.wait)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        browser.close_tab(tab["id"])


if __name__ == "__main__":
    main()
