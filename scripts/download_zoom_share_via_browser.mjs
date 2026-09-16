#!/usr/bin/env node
// Zoom 分享录像下载 —— 浏览器法（2026 改版后唯一可用路径）。
//
// 背景：2026 年中 Zoom 改版（组件化播放页 /rec/component-page + 新密码校验，
// 废弃 /rec/validate_meet_passwd），把 download_zoom_share_recording.py 和
// yt-dlp（含 nightly）的内置密码流程全打废。此脚本用一个「非 headless 真实
// Chrome」过密码、拿到会话 cookie，再交给 yt-dlp 下视频、curl 下翻译音轨。
//
// ⚠️ 必须非 headless：headless 会被 Zoom/Cloudflare 反爬拦，提交密码后回
// 误导性的「此录制文件不存在」（≠ 删除/需登录/IP 封，别被骗）。匿名+密码即可。
//
// 前置（Ubuntu）：先起一个非 headless 独立 Chrome（无显示器时脚本自动套 Xvfb）：
//   scripts/start_chrome.sh --headful            # 默认端口 9334，与腾讯会议的 headless 9333 分开
// 需要 Node ≥ 22（使用全局 WebSocket / fetch）。
//
// 用法：
//   node download_zoom_share_via_browser.mjs <share_url> <passcode> <out_dir> [port=9334]
//
// 产出（写入 out_dir）：
//   zoom_cookies.txt      —— Netscape 会话 cookie，喂给 yt-dlp/curl
//   zoom_audio_urls.json  —— [{lang,label,url}]，翻译音轨的真实签名 URL
//
// 之后（脚本会打印具体命令，文件名带完整会议标题前缀）：
//   视频： yt-dlp --cookies zoom_cookies.txt --video-password <pc> -f view_with_share <url> -o '<标题>_video.mp4'
//   音轨： curl -b zoom_cookies.txt -H 'Referer: https://us02web.zoom.us/' '<audioUrl>' -o '<标题>_audio__<lang>.m4a'

import fs from 'node:fs';
import path from 'node:path';

const [, , SHARE, PASS, OUT_DIR, PORT_ARG] = process.argv;
if (!SHARE || !PASS || !OUT_DIR) {
  console.error('用法: node download_zoom_share_via_browser.mjs <share_url> <passcode> <out_dir> [port=9334]');
  process.exit(2);
}
const PORT = PORT_ARG || process.env.LINK_DL_ZOOM_CDP_PORT || '9334';
if (typeof WebSocket === 'undefined') {
  console.error(`当前 Node ${process.version} 没有全局 WebSocket，需要 Node ≥ 22（例如 nvm install 22）。`);
  process.exit(2);
}
fs.mkdirSync(OUT_DIR, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── 连接非 headless Chrome 的页面 target ──────────────────────────────
let list;
try {
  list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
} catch (e) {
  console.error(`连不上 Chrome 调试端口 ${PORT}。先运行 scripts/start_chrome.sh --headful`);
  process.exit(3);
}
const page = list.find((t) => t.type === 'page');
if (!page) { console.error('没有可用的 page target'); process.exit(3); }
const ws = new WebSocket(page.webSocketDebuggerUrl);

let id = 0;
const pend = new Map();
const mediaSeen = new Set();       // 所有出现过的 .m4a URL
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); return; }
  if (m.method === 'Network.requestWillBeSent') {
    const u = m.params?.request?.url || '';
    if (/\.m4a(\?|$)/i.test(u)) mediaSeen.add(u.split('#')[0]);
  }
});
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
// CDP Runtime.evaluate 的值在 result.result.value（比直觉多一层，务必记住）
const ev = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true }))?.result?.result?.value;
await new Promise((r) => ws.addEventListener('open', r));

await send('Network.enable'); await send('Page.enable'); await send('Runtime.enable');
await send('Network.clearBrowserCookies');   // 每场干净会话，避免多录像串号

// ── 1) 导航 + 过密码 ─────────────────────────────────────────────────
console.log('→ 打开分享页…');
await send('Page.navigate', { url: SHARE });
let hasPwd = false;
for (let i = 0; i < 25; i++) { await sleep(1000); if (await ev('!!document.getElementById("passcode")')) { hasPwd = true; break; } }
if (!hasPwd) console.log('  (没等到密码框；可能已直达播放页，继续)');
else {
  const fill = `(function(){var el=document.getElementById('passcode');`
    + `var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;`
    + `s.call(el,${JSON.stringify(PASS)});`
    + `el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));`
    + `var b=Array.from(document.querySelectorAll('button')).find(x=>/观看录制内容|View Recording|Watch/i.test(x.innerText));`
    + `if(b){b.click();return 'submitted';}return 'no-button';})()`;
  console.log('→ 填密码并提交:', await ev(fill));
}

// ── 2) 等到播放页 ────────────────────────────────────────────────────
let playUrl = '';
for (let i = 0; i < 30; i++) { await sleep(1000); const u = (await ev('document.location.href')) || ''; if (u.includes('/rec/play/')) { playUrl = u; break; } }
if (!playUrl) {
  const body = (await ev('document.body.innerText.replace(/\\s+/g," ").slice(0,120)')) || '';
  console.error('✗ 没到播放页。页面文本:', body);
  console.error('  若含「此录制文件不存在」而你手动能播 → 多半是 headless 被反爬拦，请确认 Chrome 非 headless。');
  ws.close(); process.exit(4);
}
console.log('✓ 已到播放页:', (await ev('document.title')) || '');
await ev('(function(){var v=document.querySelector("video");if(v){v.muted=true;try{v.play();}catch(e){}}})()');
await sleep(5000);

// ── 3) 导出会话 cookie（含 httpOnly，下载必需）────────────────────────
const cookies = (await send('Network.getAllCookies'))?.result?.cookies || [];
const lines = ['# Netscape HTTP Cookie File'];
for (const c of cookies) {
  const inclSub = c.domain.startsWith('.') ? 'TRUE' : 'FALSE';
  const exp = c.session || !c.expires || c.expires < 0 ? 0 : Math.floor(c.expires);
  lines.push([c.domain, inclSub, c.path || '/', c.secure ? 'TRUE' : 'FALSE', exp, c.name, c.value].join('\t'));
}
const cookiePath = path.join(OUT_DIR, 'zoom_cookies.txt');
fs.writeFileSync(cookiePath, lines.join('\n') + '\n');
console.log(`✓ cookie 已导出 (${cookies.length} 条) → ${cookiePath}`);

// ── 4) 抓翻译音轨真实 URL（点语言菜单，逐项 click 触发加载）──────────
// play-info 里的 interpreterAudioList.audioUrl 是模板、直接下 403；
// 真实签名 URL 只在播放器切到该语言时才由 .m4a 请求带出。
const audio = [];
try {
  const opened = await ev(`(function(){var c=document.querySelector('.vjs-language-control,[class*=language-control]');if(c){c.click();return true;}return false;})()`);
  if (opened) {
    await sleep(1200);
    const items = (await ev(`JSON.stringify(Array.from(document.querySelectorAll('.vjs-language-control .vjs-menu-item,[class*=language] .vjs-menu-item')).map(e=>(e.innerText||'').trim()).filter(Boolean))`)) || '[]';
    const labels = JSON.parse(items);
    console.log(`→ 发现语言项: ${labels.join(' | ') || '(无)'}`);
    for (let i = 0; i < labels.length; i++) {
      const before = new Set(mediaSeen);
      await ev(`(function(){var c=document.querySelector('.vjs-language-control,[class*=language-control]');if(c)c.click();})()`); // 重新展开
      await sleep(600);
      await ev(`(function(){var it=Array.from(document.querySelectorAll('.vjs-menu-item')).filter(e=>e.innerText&&e.innerText.trim());if(it[${i}])it[${i}].click();return it[${i}]?it[${i}].innerText:'';})()`);
      await sleep(3500); // 等该语言音轨发起 .m4a 请求
      const fresh = [...mediaSeen].filter((u) => !before.has(u));
      for (const u of fresh) audio.push({ label: labels[i], url: u });
    }
  } else {
    console.log('→ 未找到语言切换控件（可能此录像无多语音轨）');
  }
} catch (e) { console.log('→ 音轨抓取出错（非致命）:', e.message); }

const audioPath = path.join(OUT_DIR, 'zoom_audio_urls.json');
fs.writeFileSync(audioPath, JSON.stringify(audio, null, 2));
console.log(`✓ 音轨 URL 已记录 (${audio.length} 条) → ${audioPath}`);

// ── 下一步提示 ───────────────────────────────────────────────────────
// 标题取自 OUT_DIR 目录名：剥掉 `YYYYMMDD_HHMM_` 前缀（没有前缀就用整个目录名）。
// 与 SKILL.md 的 Zoom 命名规范一致：所有产出文件都带完整会议标题前缀。
const outBase = path.basename(OUT_DIR);
const zoomTitle = outBase.replace(/^\d{8}_\d{4}_/, '') || outBase;
console.log('\n下一步：');
console.log(`  视频: yt-dlp --cookies '${cookiePath}' --video-password '<passcode>' -f view_with_share '${SHARE}' -o '${path.join(OUT_DIR, `${zoomTitle}_video.mp4`)}'`);
console.log(`  音轨: 见 ${audioPath}，逐条 curl -b '${cookiePath}' -H 'Referer: https://us02web.zoom.us/' '<url>' -o '${zoomTitle}_audio__<lang>.m4a'`);
ws.close();
process.exit(0);
