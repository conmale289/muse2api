# MUSE2API

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker" alt="Docker" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI API Compatible" />
  <img src="https://img.shields.io/badge/License-MIT-orange" alt="License" />
  <a href="https://linux.do/" target="_blank"><img src="https://img.shields.io/badge/Community-LINUX%20DO-111827?logo=linux&logoColor=white" alt="LINUX DO" /></a>
</p>

<p align="center">
  [English] | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  🤝 This open-source project links to and endorses the <b><a href="https://linux.do/" target="_blank">LINUX DO community (https://linux.do/)</a></b> — a new ideal community (sincere, friendly, united, professional).
</p>

Reverse-engineers the frontier multimodal capabilities of the **[muse.ai](https://muse.ai/)** web app into a standard **OpenAI-compatible RESTful API**. Through headless-browser CDP tunneling, warm-standby WebSocket tunnel reuse, and dynamic session management, it natively supports text chat (2–3 second streaming first-token latency), text-to-image, image-to-image editing, text-to-video, and first-frame image-to-video, plus multi-account pool affinity rotation, fully automatic 48-hour session renewal and cloud VM wake keepalive, and a companion one-click Chrome cookie-import extension.

---

## 🎬 Recommended: COOL AI — AI video & image studio for e-commerce

Upload one product photo and get talking-head sales videos, story-style ad shorts and a
complete product detail-page image set.

- **Talking-head sales videos**: script, shots and lines written for you, generated in one click
- **Story ads**: twist, dialogue and product-placement shorts, scripted and storyboarded automatically
- **Product detail images**: hero, lifestyle, selling-point, detail and comparison images from one white-background photo
- **More tools**: product retouching, GIF animations, digital humans, subtitle removal, upscaling

👉 Try it: [www.coolai.mov](https://www.coolai.mov/?utm_source=github&utm_medium=readme&utm_campaign=muse2api)


## Language

The admin console and the browser extension default to **English**. A Chinese translation
is still shipped and can be selected with the `中文 / English` switch in the top bar; the
choice is stored in `localStorage` under `muse2api_lang` (`en` or `zh`).

Documentation is English by default. The original Chinese documentation is preserved
unchanged in [`README.zh-CN.md`](README.zh-CN.md) and
[`extension/README.zh-CN.md`](extension/README.zh-CN.md).

---

## 🌟 Core Features

- 💬 **Standard chat endpoints (Chat Completions & Responses API)**
  - Fully compatible with `/v1/chat/completions` and the `/v1/responses` protocol used by default in newer Codex.
  - Native SSE typewriter streaming (`stream=True`) and synchronous full responses, with built-in warm-tab and Noise WebSocket tunnel affinity reuse. **First-token latency for continuous chat is only 2–3 seconds.**
  - Multi-turn conversation context and System Prompt support.
  - Built-in smart model alias mapping: common names such as `gpt-4o`, `gpt-5`, `claude-sonnet-4`, and `deepseek-chat` are routed automatically.
- 🎨 **High-quality image generation and editing (Images Generations & Edits)**
  - Fully compatible with `/v1/images/generations` (text-to-image) and `/v1/images/edits` (image-to-image/reference-image editing).
  - Supports `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, and other aspect ratios, single- or multi-image reference uploads, and clean watermark-free output.
  - Supports both `url` and `b64_json` response formats, with built-in fast detection of plain-text refusals to prevent queue deadlocks.
  - Image editing returns only newly generated media: upload previews and historical attachments are excluded, and the download is bound to the selected result. Reference upload or prompt delivery is explicitly confirmed; the original image is no longer returned silently.
- 🎬 **Text-to-video / image-to-video (Videos)**
  - Natively connects to Muse's top-tier video model. Duration requests of 5 / 6 / 8 / 10 seconds are passed through (actual output duration depends on upstream Muse), along with `9:16` portrait and `16:9` landscape generation.
  - Supports uploading a single first-frame reference image, or multiple content/style reference images through the `images` array; in multi-image mode the first image is the primary start frame.
  - Asynchronous task architecture (`/v1/videos` creates a task, `/v1/videos/{task_id}` polls status).
  - Built-in media service `/v1/media/{filename}` that persists generated MP4 / WebP assets.
- ⚡ **True multi-account parallelism**
  - Uses Chromium's native `Target.createBrowserContext` stealth isolation containers so tasks run concurrently in separate sandboxes.
  - Multiple accounts can generate images, generate videos, and stream chat simultaneously without blocking each other; cookies and sessions are 100% isolated.
  - Built-in smart account conflict scheduler: the same account is mutually exclusive to prevent collisions, while multiple accounts run in parallel.
  - `MUSE2API_CONCURRENCY` raises the concurrency limit according to machine capacity (default: 2).
- 🔄 **Multi-account pool with warm-connection affinity scheduling**
  - Import an unlimited matrix of Muse accounts.
  - Warm-Tab Affinity and LRU scheduling balance 2-second response times with even consumption across accounts.
  - When an account's quota is exhausted or its session fails, it is marked automatically and traffic fails over to a healthy backup account with zero perceived delay.
- 🛡️ **Session health checks and cloud VM keepalive**
  - Background requests to `/api/session` check the session and call `/api/hatch/vm/wake` as needed to wake the workspace; when an account proxy is configured, these requests use the same egress as the browser.
  - Only cookies and expiry times actually returned by upstream are saved; a successful check never inflates validity. If upstream does not renew, the session is revoked, or the account is restricted, confirm on the website and re-import.
- 🌐 **Per-account HTTP / SOCKS5 proxy**
  - Each account can use a different HTTP or SOCKS5 proxy with credentials; browser contexts and session requests are isolated per account, and proxy failures never silently fall back to a direct connection.
- 🧩 **Companion one-click Chrome cookie-import extension**
  - No manual F12 packet capture. One click on the extension icon extracts the current browser login state (including `HttpOnly` core cookies and real expiry times) and securely pushes it to the account pool.
- 🖥️ **Modern dark operations console (Web Console) with live online upgrades**
  - A ready-to-use web UI for live service health, account-pool quota and status, one-click pool-wide keepalive, task progress replay, media library management, and online API debugging.
  - **Live update broadcast and one-click upgrade across all nodes**: when the official GitHub repository publishes a new version or fix, every deployed node shows an update banner at the top of the admin console. Click **"⚡ One-click online upgrade and restart"** to pull the latest code and restart smoothly (local `.env` configuration and account data are preserved automatically).

---

## 🚀 Quick Deployment

### Option 1: Docker Compose (recommended; one command)

1. **Clone the repository and enter the directory**:
   ```bash
   git clone https://github.com/czg86389-hub/muse2api.git
   cd muse2api
   ```

2. **Configure environment variables (optional)**:
   ```bash
   cp .env.example .env
   # Edit .env as needed; set MUSE2API_KEY to your own admin key
   ```

   Docker Compose reads `MUSE2API_KEY` from `.env`; it is no longer overridden by the example value in the Compose file.

3. **Start the container**:
   ```bash
   docker compose up -d
   ```

4. **Open the admin console**:
   Browse to `http://<your-server-ip>:18610/admin?key=<your-MUSE2API_KEY>`

> **Upgrading from a version that ran as root.** The image now runs as an unprivileged
> user (uid/gid 1000 by default) so the API process cannot write outside `/app`. A `data/`
> directory created by the old root container is owned by root and must be handed over
> once, or the service will fail to write its account pool:
>
> ```bash
> sudo chown -R 1000:1000 data
> docker compose up -d --build
> ```
>
> If your host user is not uid 1000, build with your own ids instead:
> `MUSE2API_UID=$(id -u) MUSE2API_GID=$(id -g) docker compose up -d --build`.

---

### Option 2: Bare metal / VPS deployment (Ubuntu / Debian)

1. **Install system dependencies and Chromium**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y chromium fonts-wqy-zenhei python3 python3-pip python3-venv
   ```

2. **Set up a Python virtual environment**:
   ```bash
   cd /opt/muse2api
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure the systemd service**:
   ```bash
   sudo cp deploy/muse2api.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now muse2api
   ```

4. **Nginx reverse proxy (important: avoid 502 timeouts and streaming stalls)**:
   If you use Nginx / BT Panel / 1Panel as a reverse proxy, increase `proxy_read_timeout` to `600s` and disable `proxy_buffering` (see `deploy/nginx.example.conf`):
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:18610;
       proxy_read_timeout 600s;      # Avoid Nginx 502 Bad Gateway during long image/video generation
       proxy_send_timeout 600s;
       proxy_buffering off;          # Keep SSE chat typewriter streaming at zero added latency
       client_max_body_size 200M;    # Allow large video/audio reference uploads
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   }
   ```

---

## 🧩 Account Import (Chrome extension / userscript)

> **v1.6.6 users must upgrade the extension manually: Chrome/Edge extension 1.0.2+, userscript 1.1.2+.**
> A server update does not replace already-loaded extensions. Download the ZIP from this Release, overwrite the files in the original extension folder, and click "Reload" at chrome://extensions / edge://extensions; upgrade each browser profile separately. The popup title should show **v1.0.2**.
> [Download extension 1.0.2](https://github.com/czg86389-hub/muse2api/releases/download/v1.6.6/muse2api-chrome-extension-v1.6.6.zip). Only `hatch_sess` is required for import; missing `hatch_vml` or `hatch_gw` does not prove the session is logged out. After re-importing, click the account's "Test" button again.
>
> **Manual extension upgrade required:** extension 1.0.2+ / userscript 1.1.2+. A server update does not upgrade unpacked extensions. Replace files in the original folder and click Reload, separately for every browser profile.

### Option A: Chrome / Edge extension
The project ships a dedicated Chrome cookie-import extension (in `extension/`), so you can skip manual F12 cookie extraction:

1. Open Chrome / Edge and go to `chrome://extensions`, then enable **Developer mode** in the top-right corner.
2. Click **Load unpacked** and select the `extension` directory in this project.
3. Log in to [muse.ai](https://muse.ai/) in that browser until the main chat screen appears.
4. Click the extension icon in the top-right corner and enter your service address (for example `http://1.2.3.4:18610`) and `MUSE2API_KEY`.
5. Click **Read and import**, and the cookies are synced into the pool within seconds.

### Option B: Tampermonkey userscript (cross-browser, no extension install)
For users who do not want to enable Developer mode or who use Firefox / Safari, the userscript is the recommended one-click install:

👉 **[⚡ One-click install the userscript (Raw UserJS)](https://raw.githubusercontent.com/czg86389-hub/muse2api/main/tools/muse2api_cookie_importer.user.js)**

1. Install the [Tampermonkey](https://www.tampermonkey.net/) extension in your browser.
2. Click the **"⚡ One-click install the userscript"** link above; Tampermonkey opens its install window automatically. Click "Install" (no manual copy-paste needed).
3. Open [muse.ai](https://muse.ai/). A **"⚡"** floating icon appears in the bottom-right corner; click it to expand the configuration panel and push the cookies into the pool with one click (with automatic clipboard fallback).

---

## 📖 API Examples

> The complete endpoint reference — every route, `curl` request and sample response —
> lives in **[docs/API.md](docs/API.md)**. The section below is a short tour.

### Per-account proxy

In the account pool, click **Proxy** next to an account:

1. Select **Use a proxy** and paste a complete URL, for example `socks5://your-user:your-password@proxy.example.com:1080`. Username and password are parsed automatically; you can also expand the optional manual credential fields and fill them separately.
2. Click **Test connection** immediately; there is no need to save or reopen the window first. Results show proxy connectivity, egress IP, and Muse session status separately. Testing a different unsaved configuration does not change the account's saved proxy, health status, or proxy test record; testing the exact saved configuration updates that proxy's test record. Once confirmed, click **Save** to apply it on the next request.
3. Then use the account's **Test** button to confirm the browser can load the chat input box. A successful session check does not guarantee that upstream will allow a specific image or video generation.

HTTP proxy endpoints (optional Basic auth) and SOCKS5 proxy endpoints (optional username/password auth) are supported, for example `http://proxy.example.com:8080`, `socks5://proxy.example.com:1080`, or `socks5h://proxy.example.com:1080`. SOCKS5 target hostnames are resolved by the proxy; HTTPS sites are supported as usual. **HTTPS proxy endpoints, VLESS, VMess, and subscription URLs are not supported directly**; when using Clash/Mihomo and similar tools, enter an HTTP or SOCKS5 proxy endpoint reachable from the deployment server. `127.0.0.1:7890` refers to the deployment server itself, not the computer that imported the cookies. Older accounts without a proxy keep their existing network behavior.

Chat, image generation, video generation, browser downloads, account checks, quota queries, session renewal, and VM wake-ups all use the account proxy. A dropped proxy, authentication failure, or browser-context creation failure is reported as an error and **never silently falls back to a direct connection**. Switching accounts also switches proxies. External reference images are prefetched according to the existing security policy before account selection, and public-address, redirect, and DNS-rebinding protections still apply; arbitrary reference-image URLs are not guaranteed to pass through the account proxy.

Changing a proxy while the account is busy returns HTTP 409; wait for the current task to finish. After a change, the next request rebuilds the connection and does not reuse the old node. Proxy connectivity and account session state are separate: HTTP 407 or a connection timeout does not immediately mark the cookie as invalid, and a proxy test never overwrites the account's existing authentication result.

Credentials embedded in the full URL take precedence over the manual credential fields; special characters such as `@`, `#`, and `/` in usernames or passwords must be percent-encoded. After saving, the address is normalized to a form without credentials, so reopening does not echo the password.

The proxy password is stored together with cookies in the local `data/accounts.json`; protect this directory and its backups. API responses, logs, and pages never echo the password. When editing, leave the password blank to keep it as long as the address and username are unchanged; if you change the address or username, re-enter the password. Select **Default network** or **Remove proxy** to clear the configuration and restore the default network.

Admin API example (authentication required):

```http
PATCH /admin/accounts/{account_id}
Content-Type: application/json
Authorization: Bearer <MUSE2API_KEY>

{
  "proxy": {
    "server": "http://proxy.example.com:8080",
    "username": "your-proxy-user",
    "password": "your-proxy-password"
  }
}
```

Omitting `proxy` leaves the setting unchanged; sending `{"proxy": null}` removes the proxy. `POST /admin/accounts` also accepts the same `proxy` object, including setting the same proxy for a batch import. Responses contain only the server, username, and `has_password`; the password is never returned.

```http
POST /admin/accounts/{account_id}/proxy/test
Authorization: Bearer <MUSE2API_KEY>
```

With no request body, the saved proxy is tested. To test an unsaved proxy, send `Content-Type: application/json` and `{"proxy":{"server":"socks5://your-user:your-password@proxy.example.com:1080"}}`; `server` also accepts a complete HTTP/SOCKS5h URL, and legacy split credential fields remain compatible. A response with `tested_saved_proxy=false` means the tested configuration differs from the saved proxy and the account's saved content is unchanged; when `true`, the current proxy's test record is updated.

After upgrading from 1.6.8, accounts that previously had no saved test result need to click "Test connection" once. An empty record shows "Proxy not tested"; only after a real test does it show "Proxy connected" or "Proxy failed", and an unknown state is never automatically marked as success.

When testing before saving, the server may reuse a real test result from the last five minutes in the current process; it must be the same account, the same node and credentials, and the same login cookies. Changing the configuration, switching cookies, expiry, or a service restart discards the old success. Only real results are reused, and a failed test is never changed to success; this does not mean the account's browser or generation capability has been verified.

The test reaches a cookie-free HTTPS egress check through the selected proxy, then verifies the Muse session; the returned `proxy_reachable`, `exit_ip`, `session_ok`, and `workspace_status` represent each layer's result. With neither a temporary configuration nor a saved proxy, the endpoint returns 400; while the account is busy it returns 409. This operation generates no content and consumes no image/video quota.

**English:** Each account can use its own HTTP proxy with optional Basic authentication or a SOCKS5 proxy with optional username/password authentication. Select **Use a proxy**, paste a full URL such as `socks5://your-user:your-password@proxy.example.com:1080`, and click **Test connection** immediately. Saving and reopening are not required. Tests of a different unsaved route leave the saved route, account health and saved test status unchanged. Testing the exact saved configuration updates its connectivity record; click **Save** separately to apply the route. Manual credential fields remain optional. Credentials in the URL take precedence; percent-encode special characters. Both `socks5://` and `socks5h://` proxy origins are accepted, and target hostnames are resolved by the proxy. HTTPS sites are supported; HTTPS proxy endpoints, VLESS/VMess and subscription URLs are not supported directly. The proxy address must be reachable from the deployment server. A proxy failure never falls back to direct access. Unconfigured accounts retain their existing network behavior. Active accounts cannot change proxies (HTTP 409); the next request recreates the context after a saved change. Keep the password blank only to preserve it with an unchanged server and username. Neither API responses nor logs expose it. Saving the exact configuration may adopt a server-observed result from the last five minutes in the same process, bound to the account, route, credentials and cookies. Changed/expired inputs never inherit success, and failed results remain failed. The test API accepts an optional `{"proxy":{"server":"socks5://your-user:your-password@proxy.example.com:1080"}}` body for a draft test; an empty body retains saved-proxy testing. The proxy test reports connectivity, egress IP and Muse session status separately; it does not prove generation capability or replace the account's browser test.

---

All protected endpoints require this header:
```http
Authorization: Bearer <your-MUSE2API_KEY>
```

### 1. Chat Completions

```bash
curl -X POST "http://localhost:18610/v1/chat/completions" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [
      {"role": "user", "content": "Write a short poem about cyberpunk and neon rain at night."}
    ],
    "stream": false
  }'
```

### 2. Image Generation

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "A mechanical Shiba Inu wearing an anti-gravity spacesuit, cinematic lighting, 8K resolution",
    "aspect_ratio": "16:9",
    "response_format": "url"
  }'
```

**Example response**:
```json
{
  "created": 1790148495,
  "data": [
    {
      "revised_prompt": "A mechanical Shiba Inu wearing an anti-gravity spacesuit, cinematic lighting, 8K resolution",
      "url": "http://localhost:18610/v1/media/img_abc123.webp",
      "kind": "image",
      "bytes": 54210
    }
  ]
}
```

For multiple reference images, use the `images` array (up to 10 assets; each up to 40 MiB and 100 MiB total; see "Reference Assets" below). The service uploads them one by one in array order and sends the prompt only after all attachments are confirmed by the web page. `images` cannot be mixed with legacy single-image fields such as `image` / `reference_image`:

```bash
curl -X POST "http://localhost:18610/v1/images/generations" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-image",
    "prompt": "Combine the subject of the first image with the color palette of the second image into a poster",
    "images": [
      {"url": "https://example.com/subject.png"},
      {"url": "https://example.com/style.png"}
    ],
    "response_format": "url"
  }'
```

**Image edit: one image + prompt.** Send the file as multipart `image`, or put a data URL /
public URL in the JSON `images` array:

```bash
curl -X POST "http://localhost:18610/v1/images/edits" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -F "prompt=replace the background with a beach at sunset" \
  -F "image=@input.png"
```

```bash
curl -X POST "http://localhost:18610/v1/images/edits" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"add a small boat in the foreground","images":["data:image/png;base64,iVBORw0KGgo..."],"aspect_ratio":"1:1"}'
```

The multipart endpoint also accepts repeated `image` fields for several references:

```bash
curl -X POST "http://localhost:18610/v1/images/edits" \
  -H "Authorization: Bearer m2a_your_secret_key" \
  -F "prompt=Combine the two reference images into a new image" \
  -F "image=@subject.png" \
  -F "image=@style.png"
```

### 3. Text-to-video / image-to-video (Videos)

- **Step 1: Create a generation task**
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "Golden maple leaves drift gently in the breeze as sunlight filters through the branches",
      "duration": 5,
      "aspect_ratio": "16:9"
    }'
  ```
  Returns a task ID: `{"id": "task_xyz789", "status": "queued"}`

  **Image-to-video: one image + prompt.** Put the image in `images` (a `data:` URL, a public
  `https://` URL, or `{"url": "..."}`); the first image becomes the start frame:
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "the camera slowly pushes in while the leaves fall",
      "images": ["data:image/png;base64,iVBORw0KGgo..."],
      "duration": 6,
      "aspect_ratio": "16:9"
    }'
  ```

  Multi-image reference video uses the same ordered `images` array (multiple images as video references, not video-file references):
  ```bash
  curl -X POST "http://localhost:18610/v1/videos" \
    -H "Authorization: Bearer m2a_your_secret_key" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "Make the subject of the first image move through the scene and palette of the second image",
      "images": [
        {"url": "https://example.com/start.png"},
        {"url": "https://example.com/style.png"}
      ],
      "duration": 6,
      "aspect_ratio": "16:9"
    }'
  ```

- **Step 2: Poll task progress**
  ```bash
  curl "http://localhost:18610/v1/videos/task_xyz789" \
    -H "Authorization: Bearer m2a_your_secret_key"
  ```
  When complete (`status` is `queued` / `in_progress` / `completed` / `failed`):
  ```json
  {
    "id": "task_xyz789",
    "object": "video",
    "status": "completed",
    "progress": 100,
    "url": "http://localhost:18610/v1/media/vid_xyz789.mp4",
    "video": {"url": "http://localhost:18610/v1/media/vid_xyz789.mp4"},
    "seconds": "5.04",
    "size": "1280x720",
    "completed_at": 1790148600,
    "error": null
  }
  ```

  Download the file directly with `GET /v1/videos/task_xyz789/content`, or delete the task
  and its media with `DELETE /v1/videos/task_xyz789`.

---

## 🎞️ Reference Assets: Image / Video / Audio / Document

Image generation, image editing, image tasks, and video endpoints all accept reference assets, with the same limits as the muse.ai web app:

| Item | Limit |
|---|---|
| Count | Up to 10 per request (all fields combined) |
| Single size | 40 MiB; 100 MiB total |
| Image | jpg / png / gif / webp / heic / heif / avif |
| Video | mp4 / mov / webm / 3gp, duration up to 120 seconds (mp4/mov are pre-validated) |
| Audio | mp3 / wav / m4a / ogg / flac / aac / opus |
| Document | pdf / doc / docx |

- JSON fields: `images` (may mix any asset types), `videos`, `audios`, `files`, uploaded in that order. Each item can be a URL, Data URL, Base64, or an object such as `{"url": ...}` / `{"video_url": ...}` / `{"audio_url": ...}`.
- Type is taken from the Data URL / response headers / multipart-declared MIME; when the declaration is empty or `application/octet-stream`, the file signature is used.
- Multipart `/v1/images/edits` accepts repeated `image`, `video`, `audio`, and `file` fields.
- If a video task's first asset is an image, it is used as the primary start frame; the output is never the uploaded asset itself (excluded by content hash).

```bash
curl -X POST "http://localhost:18610/v1/videos"   -H "Authorization: Bearer m2a_your_secret_key"   -H "Content-Type: application/json"   -d '{
    "prompt": "Follow the camera rhythm of the reference video and the mood of the audio to generate a new city night scene",
    "videos": [{"url": "https://example.com/ref.mp4"}],
    "audios": [{"url": "https://example.com/bgm.mp3"}],
    "duration": 8,
    "size": "16:9"
  }'
```

For Nginx reverse proxies, raise `client_max_body_size` to about 200M, otherwise large assets are rejected with 413.

---

## ⚙️ Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `MUSE2API_KEY` | auto-generated | Admin console and API authentication key (starts with `m2a_`) |
| `MUSE2API_HOST` | `127.0.0.1` | Listen IP (use `0.0.0.0` inside Docker) |
| `MUSE2API_PORT` | `18610` | Service port |
| `MUSE2API_PUBLIC_BASE` | empty | Public full-URL prefix; when empty, the frontend detects the request origin |
| `MUSE2API_CHROMIUM` | `chromium` | Full path to the browser executable |
| `MUSE2API_CDP_PORT` | `19210` | Internal CDP debugging port |
| `MUSE2API_CONCURRENCY` | `2` | Maximum concurrent generation tasks (raise freely with server capacity; default 2) |
| `MUSE2API_IMAGE_TIMEOUT` | `240` | Image generation timeout (seconds) |
| `MUSE2API_VIDEO_TIMEOUT` | `600` | Video generation timeout (seconds) |
| `MUSE2API_CHAT_TIMEOUT` | `300` | Chat generation timeout (seconds) |
| `MUSE2API_CHAT_QUEUE_MAX` | `16` | Maximum in-flight `/v1/chat/completions` + `/v1/responses` requests; extra requests get HTTP 429 |
| `MUSE2API_REF_IMAGE_LOCAL_FILES` | `0` | Server-local paths are disabled by default; only trusted single-user deployments should set `1` to restore |
| `MUSE2API_REF_IMAGE_PRIVATE_FETCH` | `0` | Only public URLs are allowed by default, with per-hop DNS/redirect checks, pinned checked IPs, and no environment proxy; set `1` explicitly to allow private/local URLs |
| `MUSE2API_BROWSER_MAX_UPTIME_HOURS` | `0` | Proactively recycle the shared Chromium after this many hours of uptime, even if healthy (`0` disables); crash/unresponsive detection always runs regardless |
| `MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES` | `5` | Proactively recycle the shared Chromium after this many consecutive plain generation failures across image/video/chat (`0` disables this trigger only); detects a wedged page/renderer that the crash/CDP-unresponsive check cannot see on its own |

Both switches default to `0`: all image generation, image editing, image tasks, and video endpoints reject server-local paths and private/local URLs. Data URLs, valid Base64, and public URLs work normally. Set them to `1` only when the caller is confirmed trusted and the capability is genuinely needed.
Policy rejection, download failure, and MIME or size violations return HTTP 400 before the task is submitted, so no background task is created and then failed later.

Reference-image security regression check (simulated DNS and HTTP; no real network or account):

```bash
python tests/test_ref_image_safety.py
```

### Monitoring

`GET /metrics` returns Prometheus text exposition, gated by the same `Authorization: Bearer <MUSE2API_KEY>` header as other endpoints; a Prometheus scrape config needs an `authorization: {credentials: <key>}` (or equivalent `bearer_token`) entry for this target.

---

## 🔒 Security and Privacy

- **Zero data leakage**: all data (including account credentials, task queues, and media files) is persisted in the local `data/` directory. No third-party telemetry or external relay services are used.
- **Open-source compliance**: this project is for technical exchange, system automation operations research, and automated testing only. Do not use it in ways that violate the Meta platform terms of service or any applicable laws and regulations.

---

## 🤝 Community Recognition and Links

This project links to and highly endorses the **[LINUX DO community](https://linux.do/)**. Thanks to the community for the discussion, feedback, and support:

- 🌐 **[LINUX DO community (https://linux.do/)](https://linux.do/)** — a new ideal community (sincere, friendly, united, professional; building a community we are proud of together)

---

## 👥 Contributors

Thanks to the following developers for their code contributions and feature improvements to MUSE2API (in PR merge order):

- 🌟 **[@cpt-kenvie](https://github.com/cpt-kenvie)** (PR [#2](https://github.com/czg86389-hub/muse2api/pull/2)) — Fixed Docker Compose `.env` variable loading and example-config override issues
- 🌟 **[@djs-91](https://github.com/djs-91)** (PR [#9](https://github.com/czg86389-hub/muse2api/pull/9)) — Fixed admin page address resolution when `MUSE2API_PUBLIC_BASE` is unset and the Base URL collapses
- 🌟 **[@toby-bridges](https://github.com/toby-bridges)** (PR [#8](https://github.com/czg86389-hub/muse2api/pull/8)) — Adapted to the live muse.ai Chinese UI, fixed attachment-upload confirmation and Stop-state misdetection, and hardened video-result vs. cover selection
- 🌟 **[@LF2023](https://github.com/LF2023)** (PR [#18](https://github.com/czg86389-hub/muse2api/pull/18)) — Fixed video generation matching a cover still frame by tid alone; normalized expected matching and misselection prevention
- 🌟 **[@wuliaolaozi](https://github.com/wuliaolaozi)** (PR [#27](https://github.com/czg86389-hub/muse2api/pull/27)) — Adapted quota queries to Chinese and English, and fixed quota reading and admin-page error messages
- 🌟 **[@CarloCPP](https://github.com/CarloCPP)** (PR [#6](https://github.com/czg86389-hub/muse2api/pull/6), [#26](https://github.com/czg86389-hub/muse2api/pull/26)) — Contributed the Tampermonkey one-click cookie-import script, relaxed required-cookie validation to the session credential, and added one-click installation
- 🌟 **[@gr219](https://github.com/gr219)** (PR [#31](https://github.com/czg86389-hub/muse2api/pull/31)) — Added bilingual Chinese/English dynamic switching for the admin console and Chrome extension
- 🌟 **[@Yeadon8888](https://github.com/Yeadon8888)** (PR [#32](https://github.com/czg86389-hub/muse2api/pull/32)) — Added ordered multi-reference-image generation, editing, and image-to-video support
- 🌟 **[@pandalaohe](https://github.com/pandalaohe)** (PR [#24](https://github.com/czg86389-hub/muse2api/pull/24)) — Provided the local-file and private-URL security policy, redirect re-checking, and DNS-rebinding protection for reference images
- 🌟 **[@QuartzWarrior](https://github.com/QuartzWarrior)** (PR [#25](https://github.com/czg86389-hub/muse2api/pull/25)) — Contributed the per-account HTTP proxy foundation, authentication forwarding, configuration UI, and isolation regression tests
- [@Roins-hub](https://github.com/Roins-hub): new Muse composer compatibility and video/audio/document reference assets (PR #40).

PRs and Issues are welcome; let's keep making the project stronger and easier to use.

---

## 📄 License

Released under the [MIT License](LICENSE).


## Media Selection Regression Test

Fixes the problems where uploaded reference images are mistaken for generated results and downloads are overwritten by other media on the page.
The test uses an isolated temporary Chromium profile and a locally synthesized DOM; it does not access Muse, read accounts, or consume generation quota.
After installing project dependencies and Chromium, run from the repository root:

```bash
python tests/test_media_selection.py
```

If the browser is not on `PATH`, set `MUSE2API_CHROMIUM` to the executable path.
Coverage includes upload-preview exclusion, result deduplication, historical-attachment exclusion, precise-source download, video-source preservation,
prohibition of global download fallback, and aborting when reference-image reading fails. Final image-editing quality still requires acceptance testing against the real API.


## v1.5.2: Long-running image generation behind a reverse proxy/CDN

The synchronous OpenAI image endpoint remains compatible, but a CDN/client may cut off a long request before generation finishes.
Production clients should use short submits plus polling instead of extending HTTP timeouts and regenerating:

1. `POST /v1/images/tasks`: same JSON as `/v1/images/generations`; use `reference_image` for a single reference image or the `images` array for multiple references. Adding a stable `Idempotency-Key` request header is recommended.
2. Receive HTTP 202 and an `id`, then call `GET /v1/images/tasks/{id}` every 3 seconds.
3. When `status=completed`, read `url` or `data[0]`; when `status=failed`, show `error`. If polling fails, retry the poll only; do not POST a new generation.
4. The same idempotency key with the same input returns the original task; the same key with different input returns 409; a full queue returns 429. One process shares one browser and accepts at most 8 unfinished image tasks.

The original `/v1/images/generations` JSON and `/v1/images/edits` JSON/multipart also accept `async=true` to switch to the same async processing. Default synchronous behavior is unchanged.
`timeout` is the budget for queueing and generation waiting (1–600 seconds); browser initialization and retrieval have their own timeouts. Clients should continue polling until the task reaches a terminal state.
Task metadata is persisted; unfinished image tasks are marked failed on process restart and are never silently regenerated.
The admin page's image endpoint test also uses task polling.

Regression checks (do not consume real generation quota):

```bash
python tests/test_async_images.py
python tests/test_vm_wait.py engine.py --assert
```

Fixed a race where shared-browser exception handling reset other tasks after unlocking; leftover `Connecting...` / `Still sending` text in the page sidebar is no longer treated as evidence that the current image generation failed within 16 seconds.


## v1.5.3: Account-state anomalies and self-hosted troubleshooting

Keepalive requests originate from the **deployment server's egress**, not from the browser that exported the cookies. The author's server working does not prove that another server's network, region, proxy configuration, or the same account session is also fine.

- `/api/session HTTP 401`: upstream rejected authentication. First confirm the account can log in at the muse.ai website, then re-import the cookies.
- `HTTP 403`: access denied, possibly due to account permissions, server egress, or region/access restrictions; **this is not equivalent to an expired cookie**. Check the full error and deployment network; do not repeatedly re-import blindly.
- `HTTP 429`, `5xx`, timeout, non-JSON, or no `assigned` in the response: this keepalive is unconfirmed; do not falsely report success or overwrite the last confirmed account state. The note shows diagnostics. A previous "unchecked" or "error" state is not forcibly changed to "available".
- Browser page-load timeouts are no longer treated directly as authentication failure; only explicit authentication failure marks the account as errored.
- "Available" is the most recent confirmed result and does not guarantee the current network is reachable; "remaining validity" is a cookie time/estimate, not proof that the server-side session is valid. A successful check no longer adds 48 hours out of thin air; renewal follows the cookies actually returned.
- After updating to v1.5.3 and restarting, click the account's "Test" button to reconfirm. Errors written by older versions are not unconditionally washed into normal. Hover over the note to see the full error; when reporting, include the HTTP status, version, and deployment environment, and **do not publish cookies, API keys, or admin-page links containing keys**.

Bare-metal Git deployment: run `git pull --ff-only`, then restart the service. Docker Compose: `git pull --ff-only && docker compose up -d --build`. Save local customizations first and keep `.env` and `data/`.

Regression test that uses no real account and consumes no generation quota:

```bash
python tests/test_session_health.py
```
# MUSE2API 1.6.1

## Release Notes (2026-10-13)

- Fixed cross-account quota query bleed: quota queries are bound to the corresponding account's browser page, preventing wrong balances and usage from appearing between accounts.
- When quota reading fails or no valid data is read, the old cache is kept and the failure is reported explicitly, so invalid results are not treated as live quota.
- Fixed false keepalive-success reports: distinguish "wake request accepted" from "VM running", and report wake success only when the rechecked state is RUNNING.
- The admin page shows quota query time and expiry hints; account tests report session validity and quota-read status separately.
- Added offline regression tests covering account quota isolation and VM wake-state confirmation.

Reference-asset validation notes: videos must contain parseable duration metadata no longer than 120 seconds (MP4/MOV/3GP/WebM);
missing or corrupt duration metadata is rejected. PDF page counts, document parsing, and account permissions are still validated by upstream Muse;
this project's attachment confirmation only means the file entered the input area, not that upstream can read every file type.
Chromium and the service must be able to access the same asset files; temporary assets live in the browser profile directory and are cleaned up after the task.
