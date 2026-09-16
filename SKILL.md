---
name: link-from-baidu_tencent_zoom
description: 在 Ubuntu/Linux 服务器上管理百度网盘、腾讯会议、Zoom 分享链接——提取、去重、写入汇总表，获取元数据，转存/下载录像、AI 会议纪要和逐字稿。用自带脚本直连 Chrome 调试端口（CDP），不依赖 Agent 的 browser 工具，也不依赖 macOS。
version: 2.2.1-linux
author: sheng
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [productivity, baidu-pan, tencent-meeting, zoom, link-management, download, ubuntu, cdp]
    category: productivity
---

# 分享链接管理（百度网盘 / 腾讯会议 / Zoom）· Ubuntu 版

流程：收到链接 → 提取信息 → 写入汇总表 → 获取元数据 → 转存/下载 → 校验 → 回写汇总表。

本版从 macOS 版迁移而来，依据 2026-09 在 Ubuntu 服务器上下载腾讯会议回放的实战记录。
与 macOS 版的区别见文末「与 macOS 版的差异」。`SKILL_DIR` 指本 skill 所在目录，脚本都在 `$SKILL_DIR/scripts/`。

## When to Use

**必须触发：**
- 消息包含 `pan.baidu.com/s/`、`meeting.tencent.com/crm/`、`meeting.tencent.com/cw/`、`zoom.us/rec/share/`、`feishu.cn/minutes/`
- 用户说"保存这个链接""转存""下载录像""会议回放""纪要""逐字稿"等，并附了链接
- 用户要求批量下载腾讯会议"我的录制"或 Zoom 录像

**不触发：**
- 只讨论这些平台，没有链接
- 其他网盘（123、115、阿里云盘等）

---

## Part 0：Linux 运行环境（每次任务开始先做）

### 0.1 自检

```bash
bash "$SKILL_DIR/scripts/check_env.sh"
```

只读检查：python3、Chrome、调试端口、Xvfb、Node、yt-dlp、ffprobe、代理、磁盘、汇总表。按输出补齐缺的东西。

### 0.2 不要用 Agent 自带的 browser 工具

在无 root 的 VM 上（AppArmor 限制 user namespace），Chrome 沙箱起不来。Agent 的 browser 工具这时会报
**"Chromium browser is missing"**，但真正原因是沙箱，**不是没装浏览器**，不要去重装。
直接用本 skill 的启动脚本（自动加 `--no-sandbox`），再用脚本通过 CDP 驱动：

```bash
# 腾讯会议 / 百度网盘：headless，端口 9333，登录态保存在 ~/.cache/link-dl/main-profile
bash "$SKILL_DIR/scripts/start_chrome.sh" [--proxy http://127.0.0.1:7890]

# Zoom：必须非 headless，端口 9334；没有显示器时自动用 xvfb-run
bash "$SKILL_DIR/scripts/start_chrome.sh" --headful [--proxy http://127.0.0.1:7890]

# 关闭
bash "$SKILL_DIR/scripts/start_chrome.sh" --stop [--headful]
```

通用 CDP 小工具（排查、看页面、执行 JS）：

```bash
python3 "$SKILL_DIR/scripts/cdp_client.py" status | tabs | open <url>
python3 "$SKILL_DIR/scripts/cdp_client.py" eval  <url子串|targetId> '<js>'
python3 "$SKILL_DIR/scripts/cdp_client.py" shot  <url子串|targetId> /tmp/page.png   # 截图后可发给用户看
python3 "$SKILL_DIR/scripts/cdp_client.py" cookies <url子串>                        # 只列 cookie 名，不输出值
# Zoom 实例加 --port 9334
```

### 0.3 代理（按域名分流）

如果服务器装了 mihomo 等代理（例如 `127.0.0.1:7890`）：

| 目标 | 路由 | 原因 |
|------|------|------|
| 百度网盘 (`pan.baidu.com`) | 走代理 | 国内镜像服务器，但已多轮限速；实测代理直连 vs 代理无明显差异 |
| Google CDN/其他境外 | 走代理 | 直连约 26 KB/s，代理约 10.8 MB/s |
| **腾讯会议** (`meeting.tencent.com`, `*.cos.meeting.tencent.com`) | **直连** | 服务器在国内，代理绕路反而更慢；直连 ~10.6 MB/s vs 代理 ~2.6 MB/s（4 倍差距） |

```bash
# 腾讯会议直连（推荐）：
export HTTPS_PROXY= HTTP_PROXY= NO_PROXY=*
# 或只清空腾讯相关的就行：
export NO_PROXY=127.0.0.1,localhost,meeting.tencent.com,cos.meeting.tencent.com

# 百度/境外任务走代理：
export HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 NO_PROXY=127.0.0.1,localhost
```

下载脚本（urllib）会自动读取 `HTTPS_PROXY`；Chrome 通过 `start_chrome.sh --proxy` 设置。调试端口在本机，脚本访问时自动绕过代理。

⚠️ 代理的主要用途是**首次安装**（下载 Chrome、chromedriver、ffmpeg 等）和**境外资源获取**。腾讯会议录像不应走代理。

### 0.4 扫码登录（服务器是 headless，没有屏幕）

需要登录的平台（腾讯会议 C1/C3、百度网盘转存）先登录一次，之后复用同一个 profile：

```bash
python3 "$SKILL_DIR/scripts/login_via_qr.py" tencent     # 或 baidu
python3 "$SKILL_DIR/scripts/login_via_qr.py" tencent --check   # 只查是否已登录，退出码 0 表示已登录
```

脚本会输出 `QR_SCREENSHOT=<png路径>`。**把这张图发给用户，请用户用手机扫码**，脚本会一直等到检测到登录 cookie（默认 300 秒，每 90 秒重新截一次图，因为二维码会过期）。
如果截图里不是二维码（比如默认显示账号密码表单），先看截图，用 `cdp_client.py eval` 点击"扫码登录"，再用 `login_via_qr.py <site> --no-open` 接着截图和等待。

### 0.6 汇报规范

用户喜欢以下格式的进度报告，任何批量下载过程中都应定期主动给出：

```text
进度：3/8 完成（已下载 4.2 GB）
当前：正在下载 {会议名称} 第 2 流（共享屏幕，{大小} MB）
网速：10.6 MB/s（直连）
时间：自启动起 12 分钟
预估剩余：约 18 分钟
```

- 报告频率：每个链接完成后、或用户主动问时立即给出
- 报告内容：完成数/总数、总下载量、当前文件名+大小、当前网速、运行时间
- 报告网速时标注是否走代理（如"直连 10.6 MB/s"或"代理 3.7 MB/s"），方便用户决策路由
- 所有下载结束后给出汇总：总 GB、平均网速、校验结果、失败原因

### 0.5 路径约定（均可用环境变量覆盖）

| 用途 | 默认值 | 环境变量 |
|------|--------|----------|
| 录像根目录 | `~/Downloads/会议录制/` | `MEETING_DOWNLOAD_ROOT` |
| 链接汇总表 | `~/Documents/链接汇总/【长期关注】百度、腾讯、ZOOM链接汇总.md` | `LINK_SUMMARY_TABLE` |
| Chrome profile / 日志 / 二维码 | `~/.cache/link-dl/` | `LINK_DL_STATE_DIR` |
| 腾讯/百度 CDP 端口 | 9333 | `LINK_DL_CDP_PORT` |
| Zoom CDP 端口 | 9334 | `LINK_DL_ZOOM_CDP_PORT` |
| 目录名时区 | UTC+8（服务器可以是 UTC） | `LINK_DL_TZ_OFFSET` |

---

## Part A：链接管理（提取、去重、写汇总表）

### A1 链接类型识别与信息提取

| 类型 | URL 模式 | 提取字段 |
|------|----------|----------|
| 百度网盘 | `https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+` | 链接、提取码、分享人、备注 |
| 腾讯会议 | `https?://meeting\.tencent\.com/(crm\|cw)/[A-Za-z0-9_-]+` | 链接、密码、备注 |
| 飞书妙记 | `https?://[\w-]+\.feishu\.cn/minutes/[\w-]+` | 链接、备注 |
| Zoom | `https?://[\w-]+\.zoom\.us/rec/share/[A-Za-z0-9_-]+` | 链接、passcode、备注 |

提取码/密码：
- **百度网盘**：匹配 `提取码[：:]\s*([A-Za-z0-9]{4})`，或紧跟链接的 4 位字母数字；没提供就是无密码，填 `-`
- **腾讯会议**：用户提供回放密码；没提供填 `-`
- **Zoom**：用户提供 passcode；没提供填 `-`

### A2 汇总表

**路径**：`$LINK_SUMMARY_TABLE`（默认 `~/Documents/链接汇总/【长期关注】百度、腾讯、ZOOM链接汇总.md`）。
文件不存在时先建目录，并写入表头：

```
| 类型 | 来源 | 标题 | 链接 | 密码 | 记录日期 | 过期日期 | 转存状态 | 下载状态 | 备注 |
|---|---|---|---|---|---|---|---|---|---|
```

**去重**：读取汇总表并比较链接 URL（`/crm/` 和 `/cw/` 视为同一个短码）。已存在就提示"此链接已记录"，**不再新增行**；用户要求下载时，继续下载并更新原来那一行。

**插入**：新行插在表头和 `|---|` 分隔行之后，最新的记录在最上面。用 Python 读取、修改、写回文件，不要手工重敲整张表。

**行格式**：

```
| {类型} | {来源} | {标题} | {链接} | {密码} | {YYYY-MM-DD} | {过期日期} | {转存状态} | {下载状态} | {备注} |
```

- 类型：百度网盘 / 腾讯会议 / Zoom / 飞书妙记
- 来源：分享人，未知写"未知"
- 标题：文件名或会议标题，推断不出写"未知文件"
- 密码：有就填，无密码填 `-`
- 过期日期：已知填 `~YYYY-MM-DD`（`~` 表示估算），未知留空
- 转存状态：⬜ 未转存 / ✅ 已转存 / ❌ 已过期 / `-`（腾讯会议、Zoom 不适用）
- 下载状态：⬜ 未下载 / ✅ 已下载 / ❌ 已过期或失败
- 备注：补充信息，例如 `需登录观看；单流混合画面；含纪要+逐字稿`

### A3 元数据获取

**⚠️ 动态页面不要用 curl 判断内容。** 腾讯会议和百度网盘都是 SPA/SSR 页面，一律用 Part 0 的 Chrome + CDP 获取。

- **腾讯会议**：`download_share_recordings_via_cdp.py --dry-run <url>` 会输出标题和文件大小。
  链接性质判定见 C0。
- **百度网盘**：见 Part B。
- **Zoom**：见 Part D。
- **飞书妙记**：见 Part E。

拿到元数据后更新汇总表对应行。

### A4 输出确认

```
已保存{类型}链接：
  链接：{短链接}
  密码：{密码或"无"}
  来源：（分享人）
  保存到：链接汇总表

后续操作：
  - 百度网盘：发送"转存"可转存到网盘
  - 腾讯会议/Zoom/飞书妙记：发送"下载"可下载录像
```

---

## Part B：百度网盘转存（Chrome CDP，替代 macOS 版的 Safari 自动化）

> ⚠️ 这部分在 Ubuntu 上**尚未实测**，是按 macOS 版的逻辑换成 CDP 执行的。第一次使用时要逐步确认，并把遇到的问题补进 Pitfalls。

前提：headless Chrome 已启动，并已执行 `login_via_qr.py baidu` 登录百度账号。

流程：A1（解析）→ B0（元数据）→ B1（转存）连续自动执行，中间不暂停。

### B0 打开分享页并读取元数据

```bash
C="python3 $SKILL_DIR/scripts/cdp_client.py"
$C open 'https://pan.baidu.com/s/<code>'            # 输出 targetId
$C shot pan.baidu.com /tmp/baidu.png                  # 截图，确认页面状态
# 有提取码时：找到 4 位输入框，填入后提交（选择器以截图和 DOM 为准）
$C eval pan.baidu.com "(function(){var i=document.querySelector('input[maxlength=\"4\"],#accessCode');if(!i)return 'no-input';i.value='<提取码>';i.dispatchEvent(new Event('input',{bubbles:true}));var b=Array.from(document.querySelectorAll('a,button,span')).find(e=>/提取文件/.test(e.innerText||''));b&&b.click();return 'ok'})()"
# 读取 yunData / locals（bdstoken、share_uk、shareid）和文件列表（文件名、大小、fs_id）
$C eval pan.baidu.com "JSON.stringify({y: window.yunData||null, l: (window.locals&&window.locals.dump)?window.locals.dump():null})"
```

读取结果后更新汇总表（标题、过期日期，在备注里补充文件列表）。

### B1 自动转存

1. 转存目标默认 `/01-接收`。先在页面里请求 `/api/list?dir=/`，确认网盘里实际的文件夹名
2. 在页面内同步调用转存接口（cookie 始终留在浏览器里）：

```bash
$C eval pan.baidu.com "(function(){var x=new XMLHttpRequest();x.open('POST','/share/transfer?app_id=250528&channel=chunlei&clienttype=0&web=1&bdstoken=<bdstoken>&from=<share_uk>&shareid=<shareid>',false);x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');x.send('fsidlist=[<fs_id>]&path='+encodeURIComponent('<目标目录>'));return x.responseText})()"
```

3. 更新汇总表：转存状态改为 ✅ 已转存，或 ❌ 并写明失败原因

### B2 展示结果

```
已处理百度网盘链接：
  链接：pan.baidu.com/s/xxxxx
  文件：（文件名或文件夹内容）
  过期：~YYYY-MM-DD
  转存：✅ 已转存 → /01-接收/xxx（或 ❌ 失败原因）
```

---

## 命名规范

### 根目录

```
$MEETING_DOWNLOAD_ROOT   （默认 ~/Downloads/会议录制/）
```

### 会议子目录

每个会议一个子目录，命名为 `{YYYYMMDD}_{HHMM}_{标题}`，时间是会议实际开始时间（按 UTC+8，不受服务器时区影响）：

```
会议录制/
  20260913_1605_精神动力学视角下，疗愈“幽灵”带来的痛苦-视频回放/
  20260528_1027_5月观心实验室专家团体督导/
  20260516_1402_BCYP-Online-Seminar-5/
```

- 标题经 `safe_name()` 清理（去掉特殊字符并截断长度）
- 同日同标题的会议靠时间戳区分；会议号和 ID 记在 `manifest.json` 里，不放进目录名
- 标题缺失写 `未命名会议`；特殊字符替换为 `_`；目录名冲突时加 `_{序号}`

### 腾讯会议子目录内文件

```text
20260913_1605_<标题>/
  <标题>_混合画面.mp4
  <标题>_共享屏幕.mp4        ← 有独立流时才有
  <标题>_讲者摄像头.mp4      ← 有独立流时才有
  会议纪要.md                ← AI 纪要（文字版 + 结构化纪要，若有章节则附时间线）
  会议纪要.raw.json          ← 纪要原始响应，可用来重新渲染
  转写.txt                   ← 逐字稿，每行格式为 [HH:MM:SS] 说话人：内容
  转写.raw.json              ← 逐字稿原始分页数据（含字级时间戳）
  manifest.json
```

- 画面类型：`resource_type=0`/mixed → `混合画面`；`1`/screen → `共享屏幕`；`2`/speaker → `讲者摄像头`
- 单段录制：`{标题}_{画面类型}.mp4`；多段录制：`{标题}_第{NN}段_{画面类型}.mp4`
- **历史兼容**：旧文件名 `mixed.mp4` / `screen.mp4` / `speaker.mp4` / `mixed_1.mp4` 继续支持。判断文件是否已下载时，依次查找 manifest 记录的路径 → 新的完整文件名 → 历史短名，不因为文件改过名就重复下载

### Zoom 子目录内文件

```text
20260516_1402_BCYP-Online-Seminar-5/
  BCYP-Online-Seminar-5_video.mp4
  BCYP-Online-Seminar-5_audio__中文-CN.m4a
  BCYP-Online-Seminar-5_caption_rec1.vtt
  BCYP-Online-Seminar-5_chapter.json
```

规则为 `<safe_name(标题)>_<类型>`，类型包括 `video` / `audio__<语言-地区>` / `caption[_<rec>]` / `chapter`。旧短名 `video.mp4` 等继续兼容。
若 manifest 里有真实标题，而目录名是 `Zoom_Recording` 这类占位名，整理时把目录改名为 `YYYYMMDD_HHMM_<标题>`，并同步回写 manifest。

### 历史目录整理

客户端导出的旧目录（`YYYYMMDDHHMMSS-标题`，文件后缀为 `-共享屏幕` / `-说话人`）和旧短名文件，用下面的脚本批量整理。先 dry-run 看结果，确认后再加 `--apply`：

```bash
# --scope 取 tencent-top（新下载的腾讯目录）/ tencent-history（客户端导出旧目录）/ zoom
python3 "$SKILL_DIR/scripts/organize_recordings.py" "$MEETING_DOWNLOAD_ROOT" --scope tencent-top            # dry-run
python3 "$SKILL_DIR/scripts/organize_recordings.py" "$MEETING_DOWNLOAD_ROOT" --scope tencent-top --apply --backup-dir ~/会议录制-manifest备份
```

### 录制目录

`$MEETING_DOWNLOAD_ROOT/录制目录.xlsx`（C1 批量下载时生成，需要 `openpyxl`）

---

## Part C：腾讯会议录像下载

### C0 先判定链接属于哪种情况

| 现象 | 场景 | 走哪条流程 |
|------|------|-----------|
| 用户给了回放密码 | 公开 + 密码 | **C2** |
| 无密码，未登录也能看 | 公开无密码 | **C2**，加 `--no-password` |
| 无密码，`permission/auth` 返回"用户鉴权失败"，`common-record-info` 返回 code 403 且 recordings 为空（但能拿到打码的标题和创建者） | **需登录观看** | **C3**（最常见） |
| 用户要下载自己账号的录制 | 我的录制 | **C1** |
| 下载脚本报 `no recordings in share detail` | 可能是权限问题（Pending Approval），并非真无录制 | 用 CDP 打开查看实际状态 |

拿不准时，先跑 `download_public_share_recording.py <url> --no-password --dry-run`：如果报鉴权失败，就走 C3。也可以直接问用户"是否需要登录腾讯会议才能看"。

**权限状态区分**：`no recordings in share detail` 这个报错由两种原因引起：
- 分享者**没有生成录制**（真的无录制，极罕见）
- 分享者设置了访问权限，当前账号未被授权——页面显示 "Pending Approval" / "No Access Permission"，创建者设置了特定用户才能访问
  处理方法：CDP 打开链接查看页面内容。页面有申请按钮 `button.applyCard_btn__B0_kg` 则点击申请。页面显示 "Pending Approval" 即为**需审批**，不是无录制。统计待审批链接时，记录创建者名，汇总给用户自行联系审批。

### C3 需登录的分享回放（Linux 主流程，已实测）

```bash
bash   "$SKILL_DIR/scripts/start_chrome.sh" --proxy http://127.0.0.1:7890
python3 "$SKILL_DIR/scripts/login_via_qr.py" tencent        # 首次需要扫码，之后复用登录态
export HTTPS_PROXY=http://127.0.0.1:7890 NO_PROXY=127.0.0.1,localhost

# 先 dry-run：输出标题、文件数、大小
python3 "$SKILL_DIR/scripts/download_share_recordings_via_cdp.py" --dry-run 'https://meeting.tencent.com/cw/<code>'
# 下载视频，同时导出 AI 纪要和逐字稿（可一次传多个链接）
python3 "$SKILL_DIR/scripts/download_share_recordings_via_cdp.py" --with-minutes 'https://meeting.tencent.com/cw/<code>'
# 指定账号端口（多账号场景）
python3 "$SKILL_DIR/scripts/download_share_recordings_via_cdp.py" --cdp-port 9340 --with-minutes 'https://meeting.tencent.com/cw/<code>'
# 只补导出纪要和逐字稿
python3 "$SKILL_DIR/scripts/export_share_minutes_via_cdp.py" 'https://meeting.tencent.com/cw/<code>'
# 校验
python3 "$SKILL_DIR/scripts/verify_meeting_dir.py" "$MEETING_DOWNLOAD_ROOT/<会议目录>"
```

脚本内部的做法（出问题时按这个顺序排查）：
1. 在已登录的页面上下文里发请求（XHR/fetch 自动带 cookie），效果等同于 HAR 鉴权，但不需要导出 HAR
2. 从 `/cw/<code>` 的 SSR 数据里取 `long_url`，其中的 `id=` 就是 **share_id（分享页 UUID）**
3. 调用 `common-record-info`（需登录态），得到 `recordings[].id`
4. 调用 `get-multi-record-file` / `sign-multi-record-file`，**`auth_share_id` 必须传 share_id**。传 `encode_uni_record_id` 会报 **2710500**
5. 下载 COS 签名 URL（`ylz.cos.meeting.tencent.com/...mp4?token=...`）时，**必须同时带上**：腾讯会议 cookie（包括 HttpOnly，脚本用 `Network.getCookies` 获取）、`Range: bytes=0-`、`Referer: https://meeting.tencent.com/cw/<code>`、浏览器 UA。少任何一项都会返回 403，带齐后返回 206。中断后重跑会从 `.part` 续传
6. 纪要和逐字稿**不在外部重放请求**：缺少 `c_timestamp` / `c_nonce` / `trace-id` 签名参数会报 **2710401**。脚本开启 `Network.enable` 后重新加载页面，用 `Network.getResponseBody` 直接取页面自己请求到的响应：
   - `query-summary-and-note` → `official_template_summary`（文字纪要）+ `graphic_template_summary`（结构化纪要）
   - `minutes/detail` → 逐字稿。首页参数为 `limit=20&start_pid=0&fview=1`，翻页参数为 `pid=<上一页最后一段 pid>&fview=0`；`more:true` 就继续，`more:false` 结束。翻页时先在页面内 fetch（刷新签名参数），失败再滚动转写面板，让页面自己加载**
   ⚠️ **抓逐字稿前必须先点击 "Transcription" 标签**（`button.tab-panel_tab-panel-tab__TiKvH`，匹配 text 含 "Transcription"），激活逐字稿面板。否则 `.minutes-module-list` 可能为空或只显示 AI 纪要，抓不到逐字稿内容。激活后滚动 `.minutes-module-list` 触发分页加载，再抓取内容。**
   - `query-timeline.timeline_infos` 可能为空（分享者没有生成章节），这时章节结构以 graphic 纪要为准
7. 判断流是否完整，看以下几项是否一致：`get-multi-record-file` 返回的文件数、`get-multi-record-info.base_infos`、`v1/sign` 的 `multi_stream_recordings`、页面播放器实际加载的 mp4 数量。只有一个混合画面流（resource_type=0）是正常情况，不算下载缺失

### C2 公开分享 + 密码（无需登录）

```bash
export TENCENT_REPLAY_PASSWORD='***'
python3 "$SKILL_DIR/scripts/download_public_share_recording.py" \
  'https://meeting.tencent.com/crm/<short_code>' --password-env TENCENT_REPLAY_PASSWORD [--dry-run]
# 无密码公开链接
python3 "$SKILL_DIR/scripts/download_public_share_recording.py" 'https://meeting.tencent.com/cw/<code>' --no-password
```

可用 `--resource-type 0|1|2` 只下载某一种流。验证标准：没有 `.part` 残留，文件大小与 API 返回值一致。
如果需要纪要和逐字稿，登录后再补跑一次 `export_share_minutes_via_cdp.py`。

### C1 我的录制（批量，需登录）

```bash
python3 "$SKILL_DIR/scripts/login_via_qr.py" tencent
python3 "$SKILL_DIR/scripts/download_my_recordings.py" --list-only [--start-from 2026-03-01]
python3 "$SKILL_DIR/scripts/download_my_recordings.py" [--start-from 2026-03-01] [--resource-types 0,1,2]
```

每场会议一个子目录，逐字稿会翻页拉全，最后生成 `录制目录.xlsx`。重跑时会跳过已完成的文件，并续传 `.part`。
（备用方案：DevTools 里复制 cURL，再用 `enumerate_records_from_curls.py` → `filter_recordings_manifest.py` → `resolve_and_download_recordings.py` → `export_recording_catalog.py`，用法与 macOS 版相同，只是默认目录改成了 `$MEETING_DOWNLOAD_ROOT`。）

### C4 HAR 备用流程

如果用户能在自己电脑的浏览器里登录，并导出 HAR 上传到服务器，可以直接用 cookie 模板，不在服务器上登录：

```bash
python3 "$SKILL_DIR/scripts/download_share_recordings_from_har.py" --har meeting.tencent.com.har --dry-run --manifest dryrun.json '<url1>' '<url2>'
python3 "$SKILL_DIR/scripts/download_share_recordings_from_har.py" --har meeting.tencent.com.har --manifest batch.json '<url1>' '<url2>'
```

HAR 属于敏感文件，用完即删（`shred -u`）。

---

## Part D：Zoom 录像下载

> ⚠️ 2026 年中 Zoom 改版后，旧的 API 直下法（`download_zoom_share_recording.py`）已失效；yt-dlp 也处理不了新的密码流程。
> 现在可用的路径是：**非 headless 的 Chrome 过密码 → 拿会话 cookie → yt-dlp/curl 下载**。
> 这部分在 Ubuntu 上**尚未实测**（macOS 上可用）；服务器上用 Xvfb 提供虚拟显示。

依赖：Node ≥ 22、`xvfb`、最新版或 nightly 版 `yt-dlp`（`python3 -m pip install --user -U --pre yt-dlp`）、`ffmpeg`。

### D1 启动非 headless Chrome

```bash
bash "$SKILL_DIR/scripts/start_chrome.sh" --headful [--proxy http://127.0.0.1:7890]   # 端口 9334
```

headless 会被 Zoom/Cloudflare 的反爬拦截，提交密码后返回**误导性的「此录制文件不存在」**。这不代表录像被删除、需要登录或 IP 被封。匿名访问加密码即可，不需要登录账号。

### D2 过密码，抓取 cookie 和音轨 URL

```bash
node "$SKILL_DIR/scripts/download_zoom_share_via_browser.mjs" \
  'https://us02web.zoom.us/rec/share/<token>' '<passcode>' <out_dir> 9334
```

产出 `zoom_cookies.txt`（会话 cookie）和 `zoom_audio_urls.json`（翻译音轨的签名 URL）。两者都是敏感文件，权限设为 `chmod 600`。

### D3 下载视频

```bash
yt-dlp --cookies <out_dir>/zoom_cookies.txt --video-password '<passcode>' \
  -f view_with_share 'https://us02web.zoom.us/rec/share/<token>' -o '<out_dir>/<标题>_video.mp4'
```

`view` 是摄像头画面；`view_with_share` 是屏幕加摄像头，通常选这个。

### D4 下载翻译音轨

```bash
curl -b <out_dir>/zoom_cookies.txt -H 'Referer: https://us02web.zoom.us/' '<url>' -o '<out_dir>/<标题>_audio__<lang>.m4a'   # 应返回 206
```

原声通常已经在视频的主音轨里。

### D5 验证

没有 `.part` 残留；`.m4a` 与视频时长一致（用 `ffprobe` 检查）。签名 URL 很快过期，过期后重跑 D2。

---

## Part E：飞书妙记（Feishu/Lark Minutes）视频下载

> 2026-09 实测：飞书妙记视频流使用动态鉴权（MSE/Service Worker 注入），页面外直接下载（curl/fetch 带 Referer+UA+cookie）返回 401。唯一可靠的下载方式是通过 Chrome CDP 在页面上下文内触发浏览器下载。

### E1 流程

前提：headless Chrome 已启动（端口 9333），无需额外登录——妙记分享链接本身就是公开的。

```bash
# 1. 打开妙记页面
C="python3 $SKILL_DIR/scripts/cdp_client.py"
$C open 'https://<domain>.feishu.cn/minutes/<id>?from=from_copylink'   # 输出 targetId
# 记录下输出的 targetId（如 E6630DBB0BD4FAC41B05A806953B1264）

# 2. 确认页面加载完成，定位 <video> 元素
$C eval <targetId> 'document.querySelector("video") ? "ok" : "no video"'
# 输出 should be "ok"

# 3. 获取视频流 URL（备用确认）
$C eval <targetId> 'document.querySelector("video").src'
# 典型值：https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/preview/<file_key>?preview_type=16&mount_point=minutes

# 4. 用浏览器内置下载机制下载（核心步骤）
#    在页面上下文中创建 <a> 元素指向 video.src，触发 click 下载
$C eval <targetId> '(function(){var v=document.querySelector("video");var a=document.createElement("a");a.href=v.src;a.download="<会议标题>.mp4";document.body.appendChild(a);a.click();document.body.removeChild(a);return "download triggered"})()'
```

触发后，Chrome 下载产物保存在 `~/Downloads/` 下（文件名可能是 `minutes_video.mp4` 或 `Unconfirmed <id>.crdownload`）。等待几秒后检查完成并改名：

```bash
ls -lh ~/Downloads/minutes_video.mp4 ~/Downloads/Unconfirmed* 2>/dev/null
# 完成后移动到会议录制目录
mv ~/Downloads/minutes_video.mp4 "$MEETING_DOWNLOAD_ROOT/<YYYYMMDD_HHMM>_<标题>.mp4"
```

### E2 验证

```bash
ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=codec_name,width,height \
  -of default=noprint_wrappers=1 "$MEETING_DOWNLOAD_ROOT/<文件名>.mp4"
```

典型产出：h264 1920x1080、aac 音频、时长与妙记页面一致。飞书妙记视频通常为单视频流（无多视角版本），不生成 AI 纪要或逐字稿文件。

### E3 注意事项

- **不要尝试 curl/fetch 直接下载**视频流 URL（`internal-api-drive-stream.feishu.cn`）——无论带什么请求头，页面外请求一律 401。该 URL 的鉴权绑定在页面会话（可能的 WebSocket token、Service Worker 或 MSE 分片签名）中
- **CDP Network 域抓不到视频请求**——飞书的视频播放器使用 MSE/Media Source Extensions 或 Service Worker，媒体分片不会触发常规 Network 请求事件。不要浪费时间去写抓包脚本
- **下载文件名不确定**——Chrome 内置下载的文件名可能以 `Unconfirmed` 开头，下载完可能自动重命名为 `minutes_video.mp4`。下载完成后检查 `~/Downloads/` 找到实际文件
- **下载触发后等待几秒**——文件大小通过页面内 HEAD 请求预知的（例如 271 MB），等待数秒后检查文件是否下载完成，不要立即返回
- **a.click() 下载比 MediaRecorder/录屏好**——直接拿到原始 h264 mp4，质量无损，不需要转码。这是优先级第一的方法
- **yt-dlp 不支持飞书**：yt-dlp 2026.08.19 的 1752 个提取器中无 feishu/lark/byte 相关提取器，`--cookies-from-browser chrome` 也因 profile 路径不标准而失败。不用再尝试 yt-dlp
- **飞书妙记是单视频流**：没有多视角版本（混合画面/共享屏幕/讲者摄像头），也不生成 AI 纪要或逐字稿文件。下载产物只有一个 mp4

---

## 下载后的收尾（所有平台）

1. `verify_meeting_dir.py <目录>` 全部通过：没有 `.part`；文件大小与 API 逐字节一致；ffprobe 能读出编码、分辨率和时长；逐字稿时间戳覆盖到视频结尾附近
2. 回写汇总表：下载状态改为 ✅，备注补充"单流/多流、含纪要+逐字稿"等信息
3. 向用户报告：目录路径、文件清单和大小、校验结果、会议信息（标题、主讲、时间、时长、访问控制）。**不要在报告里贴签名 URL、cookie 或 token**

---

## Bundled Scripts

| 脚本 | 作用 | Linux 状态 |
|------|------|-----------|
| `check_env.sh` | 环境自检（只读） | 新增 |
| `start_chrome.sh` | 启动 Chrome 调试实例（headless 或 Xvfb headful，自动加 --no-sandbox，可设代理） | 新增 |
| `cdp_client.py` | 纯标准库实现的 CDP 客户端和命令行工具；替代 macOS 的 localhost:3456 代理 | 新增 |
| `login_via_qr.py` | 截图二维码，等待扫码登录（腾讯会议/百度） | 新增 |
| `download_share_recordings_via_cdp.py` | C3 需登录的分享回放下载（`--with-minutes`） | 改为原生 CDP |
| `export_share_minutes_via_cdp.py` | 通过抓包导出 AI 纪要和逐字稿 | 新增 |
| `tencent_minutes.py` | 纪要/逐字稿的分页、合并和渲染 | 新增 |
| `verify_meeting_dir.py` | 下载后校验 | 新增 |
| `linux_env.py` | 路径、端口、UA 集中配置 | 新增 |
| `account_manager.py` | 多账号登录态管理（add/list/check/login/use/remove） | 新增 |
| `download_my_recordings.py` | C1 我的录制批量下载（逐字稿改为翻页拉全） | 改为原生 CDP |
| `download_public_share_recording.py` | C2 公开分享（新增 `--no-password`） | 路径/UA 已改 |
| `download_share_recordings_from_har.py` | C4 HAR 流程 | 路径/UA 已改 |
| `enumerate_records_from_curls.py` / `filter_recordings_manifest.py` / `resolve_and_download_recordings.py` / `export_recording_catalog.py` | cURL 批量流程 | 路径已改 |
| `organize_recordings.py` / `recording_naming.py` / `test_recording_naming.py` | 命名规范与历史整理 | 不变 |
| `download_zoom_share_via_browser.mjs` | Zoom 现行下载方法 | 默认端口改为 9334 |
| `download_zoom_share_recording.py` | Zoom 旧 API（已失效，仅作参考） | 路径已改 |

旧的 macOS 代理模式仍然保留：`--cdp-mode proxy --cdp-port 3456`。

---

## Safety Rules

- 回放密码、Zoom passcode、cURL 文件、HAR、cookie（包括 `zoom_cookies.txt`）、`we_meet_token`、`pwd_token`、签名 URL、`转写.raw.json` 之外的原始接口抓包，都属于敏感信息
- 不在聊天里粘贴完整的 cookie、token、签名 URL、HAR；`cdp_client.py cookies` 只输出 cookie 名
- 报告中的分享 URL 要去掉 query string
- 调试端口只监听 `127.0.0.1`，**不要把 9333/9334 暴露到公网**：能连上这两个端口，就等于拿到了已登录的账号
- `~/.cache/link-dl/` 里有登录态，权限保持 700；需要下线登录态时删除对应的 profile 目录
- `--no-sandbox` 只用于这个专用 profile，不要用它浏览其他网站
- 百度网盘的接口调用都在浏览器页面内执行，cookie 不离开浏览器
- 批量下载前检查磁盘空间（`check_env.sh`）
- 分享者关闭了下载按钮（`allow_download=false`，或 Zoom 返回 `disableDownload: true`）时，要提醒用户，由用户自行判断是否留存

## Multi-Account Pattern

每个账号 = 独立 Chrome profile + 独立 CDP 端口。用 `account_manager.py` 管理：

```bash
# 创建账号（自动分配端口，如 9340）
python3 scripts/account_manager.py add 卡卡西 --site tencent --notes "卡卡西微信号"

# 列出所有账号及登录状态
python3 scripts/account_manager.py list

# 启动 Chrome 并扫码登录（首次）
python3 scripts/account_manager.py login 卡卡西

# 切换到指定账号（写入环境变量）
python3 scripts/account_manager.py use 卡卡西
source ~/.cache/link-dl/current_account.env

# 检查登录状态
python3 scripts/account_manager.py check 卡卡西

# 移除账号（--purge 同时删除 profile）
python3 scripts/account_manager.py remove 卡卡西
```

**端口分配规则**：9340 起自动递增。每个账号的 profile 保存在 `~/.cache/link-dl/<账号名>-profile/`。

**登录态复用**：扫码一次后，只要 profile 目录不删、腾讯会议 cookie 不过期，就无需再扫码。`account_manager.py check` 可检测当前账号是否仍有效。

**批量下载时切换账号**：
```bash
# 账号 A 下载
python3 scripts/account_manager.py use 账号A
source ~/.cache/link-dl/current_account.env
python3 scripts/download_share_recordings_via_cdp.py --with-minutes '链接1' '链接2'

# 账号 B 下载（换端口，Chrome 实例独立）
python3 scripts/account_manager.py use 账号B
source ~/.cache/link-dl/current_account.env
python3 scripts/download_share_recordings_via_cdp.py --with-minutes '链接3' '链接4'
```

## Pitfalls

- **Agent 的 browser 工具报 "Chromium browser is missing"**：真实原因是沙箱问题，不是没装浏览器。改用 `start_chrome.sh`（自带 `--no-sandbox`）
- **动态页面不要用 curl 判断内容**：必须通过 CDP 获取渲染后的页面
- **auth_share_id 传错会报 2710500**：必须传分享页 UUID（long_url 里的 `id`），不能传 `encode_uni_record_id`
- **重放纪要或逐字稿接口报 2710401**：缺签名参数。改为取页面自己请求的响应体（export 脚本已经这样处理）
- **COS 下载返回 403**：检查 cookie（含 HttpOnly）、`Range: bytes=0-`、`/cw/` Referer、UA 是否都带上了；签名过期就重跑脚本，会重新签名
- **C2 脚本用于需登录的链接**：会报"用户鉴权失败"，改走 C3
- **逐字稿不完整**：`verify_meeting_dir.py` 会提示；manifest 里 `minutes.转写.txt.complete=false` 时，加大 `--wait` 后重跑 export
- **只有一个"混合画面"文件**：多数情况是录制本身就是单流，按 C3 第 7 步核对，不要当作下载失败
- **目录时间差了 8 小时**：服务器是 UTC 时区。脚本已固定按 UTC+8 命名，如需其他时区，设置 `LINK_DL_TZ_OFFSET`
- **新服务器上根目录不存在**：脚本会自动创建（旧版在这里会崩溃）
- **headless UA 带有 HeadlessChrome**：`start_chrome.sh` 已改写 UA，下载时也使用清理后的 UA
- **Zoom「此录制文件不存在」不代表录像已删除**：多半是 headless 或反爬造成的，换 Xvfb headful 真实交互后再验证
- **Zoom 翻译音轨**：优先从 `play/info/<fileId>` 的 `interpreterAudioList[]` 取；语言菜单的 DOM 可能是 `.vjs-language-control .vjs-pop-menu li[role=menuitemradio]`
- **Node < 22**：没有全局 WebSocket，Zoom 脚本会直接退出，需要升级 Node（例如 `nvm install 22`）
- **同名系列会议**：按"时间戳 + 标题"匹配目录，不会互相覆盖
- 用户没有提供提取码或密码，就按无密码处理，填 `-`，不要追问
- 百度链接可能被微信/QQ 截断或加了跳转，先识别出真实链接
- `/crm/` 和 `/cw/` 是同一个链接的两种形式（crm 会 302 跳转到 cw）
- 转存目标以网盘里实际的文件夹名为准，不要写死
- 签名 URL 过期后，重新 resolve 或重跑脚本

## 与 macOS 版的差异

| 项 | macOS 版 | Ubuntu 版 |
|----|---------|-----------|
| 浏览器驱动 | web-access 代理 `localhost:3456` / browser 工具 | `start_chrome.sh` + `cdp_client.py` 直连 9333 |
| 登录 | 用户在自己的 Chrome/Safari 里登录 | 截图二维码 → 用户手机扫码（`login_via_qr.py`） |
| 百度网盘 | Safari + AppleScript | Chrome CDP（未实测） |
| Zoom | 本机非 headless Chrome | Xvfb + headful Chrome，端口 9334（未实测） |
| 纪要/逐字稿 | 只拉逐字稿第一页 | 抓包导出纪要 + 翻页拉全逐字稿 |
| 下载 cookie | `document.cookie`（取不到 HttpOnly） | `Network.getCookies`（包含 HttpOnly） |
| 路径 | `/Users/gaosheng/...`、iCloud Obsidian 汇总表 | `~/Downloads/会议录制`、`~/Documents/链接汇总/`，可用环境变量改 |
| 目录时间 | 本机时区 | 固定 UTC+8 |

## Useful References

详细 API 结构和排错笔记：`references/workflow.md`（末尾有 Linux 实战小节）
