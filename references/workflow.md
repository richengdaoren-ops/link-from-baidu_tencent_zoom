# Tencent Meeting Recording Workflow Reference

## Logged-In My Records API Shape

- List: `POST /wemeet-tapi/v2/meetlog/dashboard/my-record-list`
- Detail versions: `GET /wemeet-tapi/v2/meetlog/public/record-detail/get-multi-record-info`
- File resources: `GET /wemeet-tapi/v2/meetlog/public/record-detail/get-multi-record-file`
- Signed download URL: `GET /wemeet-tapi/v2/meetlog/public/record-detail/sign-multi-record-file`

Common identifiers:

- List `record_type == "cloud_record"` identifies cloud recordings.
- List `encode_record_id` is the share/auth record id used as `auth_share_id`.
- Detail `base_infos[]` contains versions/streams.
- Detail version `recording_id` is used as `record_id` for file resources.
- File resource `resource_type` should match version `stream_type`.

## Public Share Replay API Shape

Public share replay links use `https://meeting.tencent.com/crm/<short_code>` or `https://meeting.tencent.com/cw/<short_code>`. `/crm` redirects to `/cw`; the `/cw` page is a Next.js meeting-record app.

1. Fetch `/cw/<short_code>` and parse the SSR data.
   - `serverData.code == 4033` is expected before password unlock.
   - `short_url_info.long_url` contains the real share id query parameter, usually `id=<uuid>&from=3&is-single=false&record_type=2`.
   - Adding `pwd` to the `/cw` URL does not unlock SSR. Password validation is a separate API step.
2. Verify the password:
   - `GET /wemeet-tapi/v2/meetlog/public/permission/auth`
   - Important query fields: `share_id`, `enter_from=share`, `pwd`, plus common web parameters such as `c_app_id`, `c_os_model`, `c_os`, `c_os_version`, `c_app_version`, `c_instance_id`, and `platform`.
   - Success returns `code == 0` and `data.pwd_token`; treat `pwd_token` as a secret.
3. Fetch recording detail:
   - `POST /wemeet-tapi/v2/meetlog/public/detail/common-record-info`
   - Body includes `sharing_id`, `is_single`, `pwd`, `forward_cgi_path=shares`, `enter_from=share`, and `short_url_code`.
   - `data.recordings[].id` is the `record_id` for file APIs.
   - `data.meeting_info.origin_subject` and `data.meeting_info.subject` can be Base64-encoded UTF-8 meeting titles. Decode them and use the title in output filenames.
4. Optionally fetch multi-stream metadata:
   - `GET /wemeet-tapi/v2/meetlog/public/record-detail/get-multi-record-info`
   - Important query fields: `pwd`, `auth_share_id`, `uni_record_share_id`, and `activity_uid`.
5. Fetch file resources:
   - `GET /wemeet-tapi/v2/meetlog/public/record-detail/get-multi-record-file`
   - Important query fields: `record_id`, `auth_share_id`, `pwd`, and `activity_uid`.
   - The observed public-share response included `resource_type=0`, `resource_type=1`, and `resource_type=2`.
6. Sign each playback resource:
   - `GET /wemeet-tapi/v2/meetlog/public/record-detail/sign-multi-record-file`
   - Important query fields: `record_id`, `resource_id`, `resource_type`, `auth_share_id`, `pwd`, and `activity_uid`.
   - The returned `data.url` is a COS signed URL; never print or paste the full URL because its query string includes a secret token.

Do not use `download-multi-record-file` for public links where download permission is disabled. In the tested case it returned `无此文件的下载权限`, while signed playback URLs still worked.

## Public Share Download Requirements

Public-share signed playback URLs can return `403` for an ordinary complete GET. Download by mimicking the player request:

```bash
curl -L --fail --retry 3 \
  -r 0- \
  -e 'https://meeting.tencent.com/cw/<short_code>' \
  -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135 Safari/537.36' \
  -o '<local_file>.mp4' \
  '<signed_cos_url>'
```

Implementation requirements:

- Always include `Range: bytes=0-` for a new public-share download.
- Resume `.part` files with `Range: bytes=<existing>-`.
- Send `Referer: https://meeting.tencent.com/cw/<short_code>`.
- Send a browser User-Agent.
- Compare the final local file size with the API `size` value.
- Include the decoded meeting title in local filenames when `common-record-info` provides it.
- Name Tencent Meeting video files as `<完整会议标题>_混合画面.mp4`, `<完整会议标题>_共享屏幕.mp4`, or `<完整会议标题>_讲者摄像头.mp4`. For multi-segment recordings, insert `_第NN段` before the view label.
- When checking completion, prefer an explicit manifest `local_path`, then the full-title filename, then legacy `mixed.mp4` / `screen.mp4` / `speaker.mp4` names so historical downloads are not duplicated.
- To batch-rename already-downloaded folders to this scheme (Tencent CDP / public share / Zoom / historical client export), run `scripts/organize_recordings.py` (dry-run first, then `--apply` with manifest backup); see SKILL.md.
- Save only redacted signed URLs in manifests; remove query strings containing `token=`.

Default public-share output layout:

```text
$MEETING_DOWNLOAD_ROOT/<YYYYMMDD_HHMM>_<decoded meeting title>/   (macOS legacy: /Users/gaosheng/Downloads/腾讯会议回放-<title>-<short_code>/)
```

Every public replay link gets its own folder. The default `public_share_manifest.json` is saved in that same folder.

## Logged-In Share Replay HAR Flow

Some `/crm/<short_code>` or `/cw/<short_code>` replay links are share pages, but they only resolve under a logged-in Tencent Meeting browser session. In that case unauthenticated script calls can still parse `short_url_info.long_url`, but `permission/auth` may return `用户鉴权失败`. If the page renders in Safari/Chrome, export a HAR from Web Inspector/DevTools Network and use it as an auth template.

Observed flow for logged-in share replay links:

1. Export HAR from a logged-in replay page after reloading with XHR/Fetch visible.
2. Parse a request such as `get-multi-record-file` from the HAR and reuse its non-secret common query parameters plus request headers, especially the `Cookie` header. The HAR itself is sensitive.
3. Fetch `/cw/<short_code>` and parse `short_url_info.long_url`. Newer pages may store `long_url` as a bare query string such as `id=<uuid>&from=3&is-single=false&record_type=2`, not as a full URL; parse the whole string as query when `urlparse(long_url).query` is empty.
4. Call `POST /wemeet-tapi/v2/meetlog/public/detail/common-record-info` with the captured cookie and share body fields. This can return full `recordings[]` for logged-in users even when public/password flow fails.
5. Call `GET /wemeet-tapi/v2/meetlog/public/record-detail/get-multi-record-file` for each `recordings[].id`.
6. Prefer the newer multi-stream signer:
   - `GET /wemeet-cloudrecording-webapi/v1/sign`
   - Important query fields: `id=<recordings[].sharing_id>`, `source=shares`, `sharing_id=<share_id>`, `need_multi_stream=1`, `enter_from=share`, plus common web params.
   - Response can include `data.multi_stream_recordings[].sign_url` for `stream_type=1` screen and `stream_type=2` speaker.
7. If the newer signer returns no signed streams but `get-multi-record-file` reports a single resource, fall back to:
   - `GET /wemeet-tapi/v2/meetlog/public/record-detail/sign-multi-record-file`
   - Use `record_id`, `resource_id`, `resource_type`, `auth_share_id`, empty `pwd`, and empty `activity_uid`.
8. Download signed COS URLs with Range, browser User-Agent, `/cw/<short_code>` referer, and the captured Tencent Meeting cookie. The newer `ylz.cos.meeting.tencent.com` URLs may return `403` if the cookie is not included, even when the URL contains a `token=` query parameter.

For skill design, prefer the HAR workflow over direct cookie database reads. Direct Safari cookie access is blocked by macOS privacy controls and is a higher-risk credential path. HAR export is explicit, scoped to the current Tencent Meeting page, and works across Safari/Chrome. Safari Apple Events JavaScript can be an optional convenience path, but it requires enabling `Allow JavaScript from Apple Events` and is less reliable as a default.

Logged-in share HAR downloads use the same per-link folder layout under `$MEETING_DOWNLOAD_ROOT` as public replay links. Each filename includes the decoded meeting title and stream type.

## Logged-In Download Layout

The logged-in “我的录制” flow downloads all resolved recording files into one default folder:

```text
$MEETING_DOWNLOAD_ROOT/<YYYYMMDD_HHMM>_<title>/   (per-meeting folders on Linux)
```

`resolve_and_download_recordings.py` uses a flat file layout in that folder. Filenames include start time, meeting title, and stream label to avoid collisions. `export_recording_catalog.py` writes the complete meeting information list/catalog into the same folder by default:

```text
腾讯会议录制目录.csv
腾讯会议录制目录.md
腾讯会议录制目录.json
```

## Zoom Share Recording API Shape

Zoom share recording links use `https://<cluster>.zoom.us/rec/share/<token>` plus a passcode. The tested flow uses the same `requests.Session` for API calls and CloudFront downloads so Zoom cookies travel with every request.

1. Fetch the share page:
   - `GET /rec/share/<token>`
   - Extract `meetingId` from the page, falling back to the URL token when needed.
2. Validate the passcode:
   - `POST /rec/validate_meet_passwd`
   - Body includes `id=<meetingId>`, `passwd=<passcode>`, and `action=viewdetailpage`.
   - Success sets Zoom auth cookies in the session.
3. Resolve the play page:
   - `GET /nws/recording/1.0/play/share-info/<meetingId>`
   - Response contains a `/rec/play/<fileId>` URL.
4. Fetch the play page and enumerate file id candidates:
   - `GET /rec/play/<fileId>?...`
   - Extract `fileId`, `fid`, `recordingId`, data attributes, and URL-token fallbacks.
5. Fetch media metadata:
   - `GET /nws/recording/1.0/play/info/<fileId>?<browser-like query>`
   - Browser-like query includes `eagerLoadZvaPages`, `accessLevel=meeting`, `canPlayFromShare=true`, `from=share_recording_detail`, `continueMode=true`, `oldStyle=true`, `componentName=rec-play`, `originRequestUrl`, and `originDomain`.
6. Optionally probe interpreted audio ids:
   - `GET /nws/recording/1.0/play/separate-audio/<fileId>?<same query>`
   - Any additional file ids discovered are fetched with the same `play/info` endpoint.

Known media fields:

- `viewMp4Url`, `mp4Url`, `viewMp4WithshareUrl`: main video. Deduplicate by URL path.
- `interpreterAudioList[].audioUrl`: Playback Language / interpreted audio `.m4a` tracks. `language` values observed include `CN`, `US`, and `RU`.
- `ccUrl`, `captionUrl`, `transcriptUrl`, `vttUrl`: captions or transcripts, often relative paths.
- `chapterUrl`: chapter metadata, often a relative path.
- `viewMp4UrlThumbnail.thumbnail_urls[]`: timeline preview images. Do not download by default.

## Zoom Download Requirements

- Download with the same authenticated `requests.Session`; standalone `curl` can fail with `403` because it lacks session cookies.
- Resume partial files with `Range: bytes=<existing>-`.
- Prefer `--passcode-env` over `--passcode` so the Zoom passcode is not recorded in shell history.
- Store sensitive `play_info` JSON files under `.session/` with `0600` permissions. These can contain CloudFront signed URLs and must not be pasted into chat.
- Do not print signed URLs or their `Policy`, `Signature`, or `Key-Pair-Id` query parameters.
- If the recording shows `disableDownload: true`, warn that the Zoom UI did not explicitly enable downloading; the user is responsible for deciding whether retention is appropriate.

Default Zoom output layout:

```text
$MEETING_DOWNLOAD_ROOT/<YYYYMMDD_HHMM>_<meeting title>/
```

Every Zoom share link gets its own folder. Filenames include the parsed meeting title, media type, and language for interpreted audio tracks:

```text
<meeting title>__video__<original>.mp4
<meeting title>__audio__中文-CN__<original>.m4a
<meeting title>__audio__English-US__<original>.m4a
<meeting title>__audio__Русский-RU__<original>.m4a
<meeting title>__caption__<original>.vtt
<meeting title>__chapter__<original>.json
manifest.json
```

## Scope Filtering Ideas

- Latest/top N: keep the first N cloud recordings after enumeration, preserving list order.
- Date range: filter by `start_time` milliseconds.
- Title keyword: filter `title` case-insensitively.
- Version type: filter `stream_type == 1` for shared screen, `stream_type == 2` for speaker.

## Catalog Fields

Exported rows should include:

- Title
- Start time
- Meeting code
- Version type
- Duration
- Size
- Local folder
- Local filename
- Local full path
- Completion status
- Recording/share ids for audit

## Failure Recovery

- If download stops, rerun the same `--download` command. Complete files are skipped and `.part` files resume.
- If a URL returns forbidden/expired, rerun `--resolve`; if that fails, recapture `file.curl`.
- If list/detail calls fail, recapture all cURLs after confirming the browser account is still logged in.
- For public share links, rerun `download_public_share_recording.py` with the same URL and password so it can re-sign playback URLs immediately before downloading.
- For logged-in share HAR links, re-export a fresh HAR if API calls or COS downloads return `403`; cookies and signed URLs can expire. Rerun `download_share_recordings_from_har.py`; complete files are skipped and `.part` files resume.
- For Zoom links, rerun `download_zoom_share_recording.py` if CloudFront returns `403`; this refreshes Zoom session cookies and signed media URLs. Complete files are skipped or resumed.
- For Zoom links missing Playback Language audio, first run `--inspect`; if the user has a DevTools `play/info/<fileId>` request for interpreted audio, pass the file id with `--extra-file-id`.
- If public share API calls fail with parameter errors, retry with `--api-param KEY=VALUE` overrides captured from the browser request.
- If disk is low, pause download, move finished folders, then rerun.

## Linux / Ubuntu Server Notes (2026-09 field run)

Field run: a logged-in-only share replay (`/cw/<code>`, no password, `allow_download=false`) was fully downloaded on an Ubuntu server by an agent without macOS tooling. Output: 931 MB mixed-view mp4 (byte-exact vs API size, h264 2160x1080 + AAC, 94m13s), AI summary, 82-paragraph transcript over 2 pages.

Environment:

- The agent's built-in browser tool reported "Chromium browser is missing". Real cause: Chrome's sandbox cannot start in a non-root VM where AppArmor restricts user namespaces. Fix: launch Chrome yourself with `--no-sandbox --headless=new --remote-debugging-port=9333` (`scripts/start_chrome.sh`) and drive it over raw CDP WebSocket (`scripts/cdp_client.py`, stdlib only).
- The mac-only `localhost:3456` CDP proxy does not exist on Linux. `cdp_client.legacy_call()` emulates its `/targets`, `/new`, `/navigate`, `/eval`, `/close` endpoints so `download_my_recordings.py` and `download_share_recordings_via_cdp.py` run unchanged (`--cdp-mode native`, the default).
- Login: user scanned a QR code once (`login.html` → `user-center`). On a headless server take a CDP screenshot of the login page and send it to the user (`scripts/login_via_qr.py tencent`). The profile under `~/.cache/link-dl/main-profile` keeps the session.
- Network: direct Google CDN ~26 KB/s vs ~10.8 MB/s via local mihomo proxy `127.0.0.1:7890`. Use the proxy for Chrome install and for large downloads (`HTTPS_PROXY`).

Logged-in share replay (C3) specifics confirmed on Linux:

- Anonymous `permission/auth` with empty password → `用户鉴权失败`; anonymous `common-record-info` → code 403 with empty `recordings` but a masked title and creator. This signature means "login required", not "password required".
- `download_public_share_recording.py` previously refused an empty password env var; it now supports `--no-password` (still fails with 鉴权失败 for login-only links, which is the signal to switch to C3).
- Resolve `share_id` from `/cw` SSR `long_url`. With the logged-in cookie, `common-record-info` returns `recordings[0].id`.
- `get-multi-record-file`: `auth_share_id` must be the share page UUID. Passing `encode_uni_record_id` returns `2710500`. The correct parameters were confirmed by capturing the page's own requests (`Network.enable` + `Network.getResponseBody`).
- Signed COS URL host: `ylz.cos.meeting.tencent.com/.../TM-<ts>-<meetingid>-recording-1.mp4?token=...`. Plain `curl` GET → 403. It needs the Tencent Meeting cookie + `Range: bytes=0-` + `/cw/<code>` Referer + browser UA → 206. Use `Network.getCookies` (includes HttpOnly) rather than `document.cookie`.
- Stream completeness check: `get-multi-record-file` returned 1 file; `get-multi-record-info.base_infos` was empty; `v1/sign` had no `multi_stream_recordings`; the page player loaded one mp4. All four agree → single mixed stream (`resource_type=0`) is the whole recording.

Summary and transcript:

- Replaying these requests outside the page fails with `2710401` (missing `c_timestamp` / `c_nonce` / `trace-id` signature params). Read the page's own responses via `Network.getResponseBody` instead (`scripts/export_share_minutes_via_cdp.py`).
- `query-summary-and-note` → `official_template_summary` (38 sections in the field run) and `graphic_template_summary` (structured visual summary). Field layouts inside these are not fixed; the renderer is tolerant and raw JSON is always kept.
- `minutes/detail` pagination: first page `limit=20&start_pid=0&fview=1`; next pages `pid=<last_pid>&fview=0`; continue while `more:true`. The exporter first replays the page's own URL in-page with refreshed signature params, then falls back to scrolling the transcript panel.
- `query-timeline.timeline_infos` may be empty (sharer did not generate chapters); use the graphic summary for structure.
- Transcript text format: `[HH:MM:SS] 说话人：内容`; raw word-level data saved as `转写.raw.json`.

Verification (`scripts/verify_meeting_dir.py`): no `.part`; local size == API size; `ffprobe` codec/resolution/duration; transcript last timestamp near the end of the video (field run: 00:29–93:46 vs 94:13, ending with the closing remarks).

Linux path conventions: `~/Downloads/会议录制/` for recordings, `~/Documents/链接汇总/【长期关注】百度、腾讯、ZOOM链接汇总.md` for the link table (override with `MEETING_DOWNLOAD_ROOT` / `LINK_SUMMARY_TABLE`). Folder timestamps use UTC+8 regardless of server timezone.
