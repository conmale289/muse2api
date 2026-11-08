# muse2api — API Reference

Complete reference for every endpoint exposed by the service, with `curl` examples and
sample responses.

- [1. Basics](#1-basics)
- [2. Text / chat](#2-text--chat)
- [3. Images](#3-images)
- [4. Video](#4-video)
- [5. Media and models](#5-media-and-models)
- [6. Health](#6-health)
- [7. Admin](#7-admin)
- [8. Errors](#8-errors)
- [9. Limits and behaviour](#9-limits-and-behaviour)

---

## 1. Basics

### Base URL

```
http://<host>:18610
```

Every `/v1/*` path is OpenAI-shaped, so an OpenAI SDK works by changing two values:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:18610/v1", api_key="m2a_...")
```

### Authentication

All `/v1/*` and most `/admin/*` endpoints require a Bearer key:

```
Authorization: Bearer $MUSE2API_KEY
```

The key starts with `m2a_`. It is generated on first start if `MUSE2API_KEY` is empty and
printed to the log. Read it any time with `GET /admin/apikey`.

Two endpoints accept a **different** key:

| Endpoint | Credential |
|---|---|
| `POST /admin/repo/push` | `MUSE2API_MAINTAINER_KEY` only. **403 if unset.** |
| `POST /admin/update/upgrade`, `/admin/repo/pull` | `MUSE2API_MAINTAINER_KEY` if set, otherwise the normal API key (with a startup warning). |

### No authentication

`GET /healthz`, `GET /readyz`, `GET /v1/media/{name}`, `GET /`, `GET /admin`,
`GET /admin/extension`, `GET /admin/extension/files`, `GET /admin/cookie-helper`,
`GET /admin/update/check`, `GET /admin/repo/status`.

### Content types

| Where | Accepted |
|---|---|
| JSON endpoints | `application/json` |
| `/v1/images/edits` | `application/json` **or** `multipart/form-data` |
| Responses | JSON, except `/v1/videos/{id}/content` (binary) and SSE endpoints (`text/event-stream`) |

### Environment used in the examples

```bash
export BASE="http://localhost:18610"
export KEY="m2a_your_key_here"
```

### Compatibility map

| muse2api | OpenAI equivalent |
|---|---|
| `POST /v1/chat/completions` | Chat Completions |
| `POST /v1/responses` | Responses API |
| `POST /v1/images/generations` | Images: create |
| `POST /v1/images/edits` | Images: edit |
| `POST /v1/videos` | Videos: create |
| `GET /v1/videos/{id}` | Videos: retrieve |
| `GET /v1/videos/{id}/content` | Videos: download content |
| `DELETE /v1/videos/{id}` | Videos: delete |
| `GET /v1/models` | Models: list |
| `GET /v1/models/{id}` | Models: retrieve |

`POST /v1/images/tasks` + `GET /v1/images/tasks/{id}` are an **extension**: the same
generation, but asynchronous so a reverse proxy cannot time it out.

---

## 2. Text / chat

### 2.1 `POST /v1/chat/completions`

OpenAI-compatible chat. The Muse web app is an auto-routing agent with no model picker, so
`model` only affects downstream compatibility — it does not change routing. Common names
(`gpt-4o`, `gpt-5`, `claude-sonnet-4`, `deepseek-chat`, …) are mapped automatically;
unknown names pass through unchanged.

**Request body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `messages` | array | `[]` | `role`, `content` (string or content-part array) |
| `model` | string | `muse-spark` | Alias or arbitrary name |
| `stream` | bool | `false` | SSE when `true` |
| `stream_options` | object | — | `{"include_usage": true}` adds a final usage chunk |
| `temperature` | float | — | Accepted, not forwarded upstream |
| `max_tokens` | int | — | Accepted, not forwarded upstream |
| `timeout` | int | `MUSE2API_CHAT_TIMEOUT` (300) | Seconds |
| `tools` | array | — | Only used when `MUSE2API_TOOL_PROTOCOL=1` |
| `prompt` | string | — | Compatibility shortcut for a single user turn |

Text-only. An `image_url` content part returns **400** rather than being silently dropped.

#### Non-streaming

```bash
curl -sS "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [
      {"role": "system", "content": "You are terse."},
      {"role": "user", "content": "Name the tallest mountain on Earth."}
    ]
  }'
```

```json
{
  "id": "chatcmpl-9f1c2d3e4b5a69788796a5b4c3d2e1f0",
  "object": "chat.completion",
  "created": 1762000000,
  "model": "muse-spark",
  "choices": [
    {
      "index": 0,
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "Mount Everest, 8,849 m."
      }
    }
  ],
  "usage": {
    "prompt_tokens": 18,
    "completion_tokens": 8,
    "total_tokens": 26
  }
}
```

> `usage` is an **estimate** (~4 characters per token). Muse exposes no real counts.

#### Streaming

```bash
curl -N "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "Count to three."}]
  }'
```

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1762000000,"model":"muse-spark","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1762000000,"model":"muse-spark","choices":[{"index":0,"delta":{"content":"One"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1762000000,"model":"muse-spark","choices":[{"index":0,"delta":{"content":", two"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1762000000,"model":"muse-spark","choices":[{"index":0,"delta":{"content":", three."},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1762000000,"model":"muse-spark","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":1762000000,"model":"muse-spark","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}

data: [DONE]
```

#### Tool calling (opt-in)

The Muse assistant refuses to emit pseudo tool-call JSON, so this is **off by default**.
Enable it with `MUSE2API_TOOL_PROTOCOL=1`; the protocol prompt is then injected and the
model's JSON is parsed back into `tool_calls`.

```bash
curl -sS "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "messages": [{"role": "user", "content": "Weather in Paris?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }]
  }'
```

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1762000000,
  "model": "muse-spark",
  "choices": [
    {
      "index": 0,
      "finish_reason": "tool_calls",
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_3a9f8b7c6d5e4f3a2b1c",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"city\": \"Paris\"}"
            }
          }
        ]
      }
    }
  ],
  "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54}
}
```

Streaming tool calls arrive as two chunks per call: one with `id`/`name`/empty `arguments`,
then one with the full `arguments`.

### 2.2 `POST /v1/responses`

The Responses API used by newer Codex builds. Text in, text out, with real streaming events.

**Request body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `input` | string or array | — | String, or items with `role`/`content`, `input_text`, `function_call_output`, `function_call` |
| `instructions` | string | — | Prepended as the system turn |
| `model` | string | `muse-spark` | |
| `stream` | bool | `false` | |
| `max_output_tokens` | int | — | Accepted, not forwarded upstream |
| `timeout` | int | 300 | Seconds |
| `tools` | array | — | Same opt-in protocol as chat |
| `store` | bool | — | Accepted, not forwarded upstream |

#### Non-streaming

```bash
curl -sS "$BASE/v1/responses" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-spark",
    "instructions": "Answer in one sentence.",
    "input": "What is a reverse proxy?"
  }'
```

```json
{
  "id": "resp_7c1e2f3a4b5c6d7e8f9a0b1c",
  "object": "response",
  "created_at": 1762000000,
  "status": "completed",
  "model": "muse-spark",
  "output": [
    {
      "type": "message",
      "id": "msg_1a2b3c4d5e6f7a8b9c0d1e2f",
      "role": "assistant",
      "status": "completed",
      "content": [
        {
          "type": "output_text",
          "text": "A reverse proxy forwards client requests to backend servers and returns their responses.",
          "annotations": []
        }
      ]
    }
  ],
  "usage": {"input_tokens": 12, "output_tokens": 25, "total_tokens": 37}
}
```

#### Streaming

```bash
curl -N "$BASE/v1/responses" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"muse-spark","stream":true,"input":"Say hi."}'
```

```
event: response.created
data: {"type":"response.created","response":{"id":"resp_...","object":"response","created_at":1762000000,"status":"in_progress","model":"muse-spark","output":[],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}

event: response.output_item.added
data: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg_...","type":"message","role":"assistant","status":"in_progress","content":[]}}

event: response.content_part.added
data: {"type":"response.content_part.added","item_id":"msg_...","output_index":0,"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_...","output_index":0,"content_index":0,"delta":"Hi"}

event: response.output_text.done
data: {"type":"response.output_text.done","item_id":"msg_...","output_index":0,"content_index":0,"text":"Hi!"}

event: response.output_item.done
data: {"type":"response.output_item.done","output_index":0,"item":{"id":"msg_...","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"Hi!","annotations":[]}]}}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_...","object":"response","created_at":1762000000,"status":"completed","model":"muse-spark","output":[{"type":"message","id":"msg_...","role":"assistant","status":"completed","content":[{"type":"output_text","text":"Hi!","annotations":[]}]}],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}
```

With `tools` and `MUSE2API_TOOL_PROTOCOL=1`, the stream instead emits
`response.function_call_arguments.delta` / `.done` and a `function_call` output item:

```json
{
  "type": "function_call",
  "id": "call_3a9f8b7c6d5e4f3a2b1c",
  "call_id": "call_3a9f8b7c6d5e4f3a2b1c",
  "name": "get_weather",
  "arguments": "{\"city\": \"Paris\"}",
  "status": "completed"
}
```

A failure mid-stream ends with:

```
event: response.failed
data: {"type":"response.failed","response":{"id":"resp_...","status":"failed","output":[],...}}
```

---

## 3. Images

### 3.1 `POST /v1/images/generations`

Text-to-image, and image-to-image when reference fields are present.

**Request body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | **required** | |
| `model` | string | `muse-image` | `dall-e-3`, `gpt-image-1`, … all map here |
| `n` | int | `1` | **Only `1` is supported**; `n>1` returns 400 |
| `size` | string | — | `1024x1024`, `1024x1792`, `1792x1024`, `1280x720`, … |
| `aspect_ratio` | string | — | `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, `21:9` |
| `response_format` | string | `url` | `url` or `b64_json` |
| `timeout` | int | 240 | 1–600 seconds |
| `async` | bool | `false` | Return a task id instead of blocking |
| `image` | string | — | Legacy single reference (data URL or public URL) |
| `images` | array | — | Ordered references; may mix image/video/audio/document |
| `reference_image` | string | — | Alias of `image` |
| `videos`, `audios`, `files` | array | — | Extra reference assets |
| `extra` | string | — | Appended verbatim to the prompt |

Reference limits: 10 assets, 40 MiB each, 100 MiB total.

#### Text-to-image (URL)

```bash
curl -sS "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "dall-e-3",
    "prompt": "a lighthouse at dusk, long exposure",
    "size": "1024x1792",
    "response_format": "url"
  }'
```

```json
{
  "created": 1762000000,
  "data": [
    {
      "revised_prompt": "a lighthouse at dusk, long exposure",
      "url": "https://api.example.com/v1/media/3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d.png",
      "size": "1024x1792",
      "kind": "image",
      "bytes": 842133
    }
  ]
}
```

`size` reports the dimensions Muse actually produced when they were captured, not the
requested ones.

#### Base64 response

```bash
curl -sS "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a red apple on a table","response_format":"b64_json"}'
```

```json
{
  "created": 1762000000,
  "data": [
    {
      "revised_prompt": "a red apple on a table",
      "size": "1024x1024",
      "kind": "image",
      "bytes": 512004,
      "b64_json": "iVBORw0KGgoAAAANSUhEUgAABAAAAAQACAIAAADwf7zU..."
    }
  ]
}
```

#### Image-to-image

```bash
curl -sS "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "turn this into a watercolour painting",
    "images": ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."],
    "aspect_ratio": "1:1"
  }'
```

Same response shape as above.

#### Asynchronous

```bash
curl -sS "$BASE/v1/images/generations" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a paper boat","async":true}'
```

```json
{
  "id": "task_4b3c2d1e0f9a8b7c6d5e",
  "task_id": "task_4b3c2d1e0f9a8b7c6d5e",
  "object": "image.task",
  "status": "queued",
  "progress": 0,
  "created_at": 1762000000
}
```

Returns **202 Accepted**.

### 3.2 `POST /v1/images/tasks`

Identical request body to `/v1/images/generations`, but always asynchronous. Accepts an
optional `Idempotency-Key` header (max 256 chars).

```bash
curl -sS -i "$BASE/v1/images/tasks" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: poster-v3" \
  -d '{"prompt":"a paper boat","size":"1024x1024"}'
```

```
HTTP/1.1 202 Accepted
Idempotency-Key: poster-v3
Idempotent-Replayed: false
Content-Type: application/json
```

```json
{
  "id": "task_4b3c2d1e0f9a8b7c6d5e",
  "task_id": "task_4b3c2d1e0f9a8b7c6d5e",
  "object": "image.task",
  "status": "queued",
  "progress": 0,
  "created_at": 1762000000
}
```

Repeating the request with the same key and the same body returns the same task with
`Idempotent-Replayed: true`. Reusing the key with a different body returns **409**.

### 3.3 `GET /v1/images/tasks/{task_id}`

```bash
curl -sS "$BASE/v1/images/tasks/task_4b3c2d1e0f9a8b7c6d5e" \
  -H "Authorization: Bearer $KEY"
```

While running:

```json
{
  "id": "task_4b3c2d1e0f9a8b7c6d5e",
  "object": "task",
  "kind": "image",
  "prompt": "a paper boat",
  "api_prompt": "全新文生图创作（…）：a paper boat，本次请求未附带参考素材，…",
  "status": "processing",
  "progress": 20,
  "created_at": 1762000000,
  "updated_at": 1762000012,
  "size": "1024x1024",
  "response_format": "url",
  "result": null,
  "error": null
}
```

When complete, the OpenAI-shaped result is merged in:

```json
{
  "id": "task_4b3c2d1e0f9a8b7c6d5e",
  "object": "task",
  "kind": "image",
  "prompt": "a paper boat",
  "api_prompt": "全新文生图创作（…）：a paper boat，本次请求未附带参考素材，…",
  "status": "completed",
  "progress": 100,
  "created_at": 1762000000,
  "updated_at": 1762000026,
  "account": "a1b2c3d4e5f6",
  "elapsed": 14.2,
  "url": "https://api.example.com/v1/media/9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f.png",
  "size": "1024x1024",
  "response_format": "url",
  "result": {
    "filename": "9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f.png",
    "size": 640112,
    "kind": "image",
    "url": "https://api.example.com/v1/media/9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f.png",
    "w": 1024,
    "h": 1024
  },
  "error": null,
  "created": 1762000026,
  "data": [
    {
      "revised_prompt": "a paper boat",
      "url": "https://api.example.com/v1/media/9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f.png",
      "size": "1024x1024",
      "kind": "image",
      "bytes": 640112
    }
  ]
}
```

On failure `status` is `failed` and `error` holds the reason.

The task object also carries internal bookkeeping keys (`image_request_key`,
`image_request_hash`, `api_prompt`); treat them as opaque and read only the fields
documented here.

### 3.4 `POST /v1/images/edits`

Image editing. Accepts multipart (like OpenAI) **or** JSON.

**Multipart fields:** `prompt`, `model`, `size`, `aspect_ratio`, `response_format`,
`timeout`, `n`, `async`, and repeatable `image` / `image[]` / `video` / `audio` / `file`.

**JSON fields:** the same names as `/v1/images/generations`, plus `images`,
`reference_image`, `image_url`, `image`, `videos`, `audios`, `files`.

#### Multipart

```bash
curl -sS "$BASE/v1/images/edits" \
  -H "Authorization: Bearer $KEY" \
  -F "prompt=replace the background with a beach" \
  -F "size=1024x1024" \
  -F "image=@./input.png;type=image/png"
```

```json
{
  "created": 1762000000,
  "data": [
    {
      "revised_prompt": "replace the background with a beach",
      "url": "https://api.example.com/v1/media/1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d.png",
      "size": "1024x1024",
      "kind": "image",
      "bytes": 733920
    }
  ]
}
```

#### JSON with a data URL

```bash
curl -sS "$BASE/v1/images/edits" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "add a small boat in the foreground",
    "images": ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."],
    "response_format": "url"
  }'
```

Same response shape.

#### Asynchronous edit

```bash
curl -sS "$BASE/v1/images/edits" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"make it night","images":["data:image/png;base64,..."],"async":true}'
```

Returns the same `202` task object as §3.2. Poll it with `GET /v1/images/tasks/{id}`.

---

## 4. Video

### 4.1 `POST /v1/videos`

Creates an asynchronous video task. `POST /v1/videos/generations` is an alias.

**Request body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | **required** | |
| `model` | string | `muse-video` | `sora`, `sora-2`, `veo-3`, `kling`, `runway`, … all map here |
| `duration` | int | 6 | 1–60. Treated as a preference, not a guarantee |
| `size` | string | — | e.g. `1280x720`, `720x1280`, `1024x1792` |
| `aspect_ratio` | string | — | `16:9`, `9:16`, `1:1`, … |
| `resolution` | string | — | Free-form quality hint |
| `timeout` | int | 600 | 10–3600 seconds |
| `image` | string | — | First-frame reference |
| `images` | array | — | Multiple references; first is the primary start frame |
| `image_url`, `reference_image` | string | — | Aliases of `image` |
| `videos`, `audios`, `files` | array | — | Extra reference assets |
| `extra` | string | — | Appended verbatim |

Accepts an optional `Idempotency-Key` header.

```bash
curl -sS "$BASE/v1/videos" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sora-2",
    "prompt": "a wave breaking on black sand at sunrise",
    "duration": 8,
    "aspect_ratio": "16:9",
    "size": "1280x720"
  }'
```

`size` in the response is echoed from the request until the file is rendered; after
completion it is replaced with the measured dimensions when they are known.

```json
{
  "id": "task_7f6e5d4c3b2a190807f6e5d4",
  "task_id": "task_7f6e5d4c3b2a190807f6e5d4",
  "object": "video",
  "kind": "video",
  "prompt": "a wave breaking on black sand at sunrise",
  "api_prompt": "全新文生视频创作（纯文本全新生成）：生成一个16:9横屏宽屏视频（…，时长约 8 秒）：a wave breaking on black sand at sunrise，本次请求未附带参考素材，…",
  "status": "queued",
  "progress": 10,
  "created_at": 1762000000,
  "updated_at": 1762000000,
  "duration": 8,
  "size": "1280x720",
  "model": "muse-video",
  "seconds": "8",
  "completed_at": null,
  "expires_at": null,
  "error": null,
  "result": null
}
```

Returns **200 OK**.

Image-to-video:

```bash
curl -sS "$BASE/v1/videos" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "the camera slowly pushes in",
    "duration": 6,
    "images": ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUg..."]
  }'
```

### 4.2 `GET /v1/videos/{task_id}`

`GET /v1/videos/generations/{task_id}` is an alias.

```bash
curl -sS "$BASE/v1/videos/task_7f6e5d4c3b2a190807f6e5d4" \
  -H "Authorization: Bearer $KEY"
```

In progress:

```json
{
  "id": "task_7f6e5d4c3b2a190807f6e5d4",
  "object": "video",
  "status": "in_progress",
  "progress": 40,
  "seconds": "8",
  "size": "1280x720",
  "model": "muse-video",
  "completed_at": null,
  "expires_at": null,
  "error": null,
  "created_at": 1762000000,
  "updated_at": 1762000045
}
```

Completed:

```json
{
  "id": "task_7f6e5d4c3b2a190807f6e5d4",
  "task_id": "task_7f6e5d4c3b2a190807f6e5d4",
  "object": "video",
  "kind": "video",
  "prompt": "a wave breaking on black sand at sunrise",
  "status": "completed",
  "progress": 100,
  "model": "muse-video",
  "seconds": "7.96",
  "size": "1280x720",
  "completed_at": 1762000160,
  "expires_at": null,
  "error": null,
  "created_at": 1762000000,
  "updated_at": 1762000160,
  "account": "a1b2c3d4e5f6",
  "elapsed": 158.4,
  "url": "https://api.example.com/v1/media/5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4",
  "video": {
    "url": "https://api.example.com/v1/media/5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4"
  },
  "result": {
    "url": "https://api.example.com/v1/media/5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4",
    "filename": "5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4",
    "bytes": 4812033,
    "kind": "video",
    "w": 1280,
    "h": 720
  }
}
```

`seconds` is the **measured** clip length when the container is readable, otherwise the
requested duration. `status` is one of `queued`, `in_progress`, `completed`, `failed`.

Failed:

```json
{
  "id": "task_...",
  "object": "video",
  "status": "failed",
  "progress": 15,
  "error": {"message": "Expected a video but extracted an image (.webp); refusing to deliver an image as video"},
  "completed_at": null
}
```

### 4.3 `GET /v1/videos/{task_id}/content`

Downloads the rendered file. This is what the OpenAI SDK's `videos.content(id)` calls.

```bash
curl -sS -OJ "$BASE/v1/videos/task_7f6e5d4c3b2a190807f6e5d4/content" \
  -H "Authorization: Bearer $KEY"
```

```
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Disposition: attachment; filename="5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4"
```

- **409** while the task has no file yet (`Video is not ready yet`)
- **404** for an unknown id or a missing file

`video/webm` and `video/quicktime` are returned for those extensions.

### 4.4 `DELETE /v1/videos/{task_id}`

Deletes the task record and its media file.

```bash
curl -sS -X DELETE "$BASE/v1/videos/task_7f6e5d4c3b2a190807f6e5d4" \
  -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "task_7f6e5d4c3b2a190807f6e5d4",
  "object": "video.deleted",
  "deleted": true
}
```

---

## 5. Media and models

### 5.1 `GET /v1/media/{name}`

Serves a generated file. **No authentication** — the file name is a random UUID, so the URL
acts as a capability link.

```bash
curl -sS -o out.png "$BASE/v1/media/9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f.png"
```

- **400** for a name containing `/`, `\` or `..`
- **404** when the file does not exist

### 5.2 `GET /v1/models`

```bash
curl -sS "$BASE/v1/models" -H "Authorization: Bearer $KEY"
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "muse-spark",
      "object": "model",
      "owned_by": "muse",
      "description": "Muse Spark — text/code chat (free web quota, streaming supported)",
      "context_window": 32768,
      "supports_streaming": true,
      "supports_vision": false,
      "modalities": ["text"]
    },
    {
      "id": "muse-image",
      "object": "model",
      "owned_by": "muse",
      "description": "Muse Image — text-to-image / image editing (free web quota)",
      "context_window": 4096,
      "supports_streaming": false,
      "supports_vision": true,
      "modalities": ["text", "image"]
    },
    {
      "id": "muse-video",
      "object": "model",
      "owned_by": "muse",
      "description": "Muse Video — text-to-video / image-to-video (free web quota)",
      "context_window": 4096,
      "supports_streaming": false,
      "supports_vision": true,
      "modalities": ["text", "image", "video"]
    }
  ]
}
```

### 5.3 `GET /v1/models/{model_id}`

Resolves aliases, so a client validating `dall-e-3` gets the real capability back.

```bash
curl -sS "$BASE/v1/models/dall-e-3" -H "Authorization: Bearer $KEY"
```

```json
{
  "id": "muse-image",
  "object": "model",
  "owned_by": "muse",
  "description": "Muse Image — text-to-image / image editing (free web quota)",
  "context_window": 4096,
  "supports_streaming": false,
  "supports_vision": true,
  "modalities": ["text", "image"]
}
```

Unknown model → **404**.

---

## 6. Health

### 6.1 `GET /healthz`

```bash
curl -sS "$BASE/healthz"
```

```json
{"status": "ok", "time": 1762000000}
```

### 6.2 `GET /readyz`

```bash
curl -sS "$BASE/readyz"
```

```json
{"status": "ready", "accounts": 2, "browser_running": true}
```

`status` is `no_account` when the pool is empty.

---

## 7. Admin

Admin responses use `{"detail": "..."}` for errors (not the OpenAI envelope).

### 7.1 `GET /admin/status`

Overview used by the console.

```bash
curl -sS "$BASE/admin/status" -H "Authorization: Bearer $KEY"
```

```json
{
  "accounts": [
    {
      "id": "a1b2c3d4e5f6",
      "label": "main",
      "cookies_exp": {"hatch_sess": 1762500000},
      "expires_at": 1762400000,
      "expiry_anchor": null,
      "enabled": true,
      "created_at": 1761900000,
      "last_used": 1762000000.5,
      "last_keepalive": 1761999000,
      "use_count": 12,
      "ok": true,
      "note": "Session valid · auto-keepalive (VM: RUNNING)",
      "synced_at": 1762000000,
      "checked_at": 1761999000,
      "quota": {
        "found": true,
        "plan": "Free plan",
        "weekly_reset": "Nov 12",
        "weekly_used_pct": 29,
        "extra_left": "1B tokens left",
        "extra_used_pct": 3,
        "extra_expires": "never",
        "checked_at": 1761999000
      },
      "proxy": null,
      "cookie_count": 12,
      "essential_ok": true,
      "expires_estimated": false
    }
  ],
  "stats": {
    "total": 1,
    "enabled": 1,
    "disabled": 0,
    "healthy": 1,
    "error": 0,
    "expiring": 0,
    "expired": 0
  },
  "tasks": [],
  "media_count": 3,
  "browser_running": true,
  "essential_cookies": ["hatch_sess"],
  "base_url": "https://api.example.com/v1",
  "corrupt_files": [],
  "config": {
    "site": "https://muse.ai/",
    "cdp_port": 19210,
    "image_timeout": 240,
    "video_timeout": 600,
    "host": "0.0.0.0",
    "port": 18610,
    "public_base": "https://api.example.com"
  }
}
```

`corrupt_files` is non-empty when a data file had to be quarantined at startup; the
original is preserved at the reported `backup` path.

### 7.2 `GET /admin/accounts`

```bash
curl -sS "$BASE/admin/accounts" -H "Authorization: Bearer $KEY"
```

```json
{
  "accounts": [
    {
      "id": "a1b2c3d4e5f6",
      "label": "main",
      "enabled": true,
      "expires_at": 1762400000,
      "ok": true,
      "cookie_count": 12,
      "essential_ok": true,
      "expires_estimated": false,
      "proxy": {"server": "socks5://127.0.0.1:1080", "username": "", "has_password": false}
    }
  ],
  "stats": {"total": 1, "enabled": 1, "disabled": 0, "healthy": 1, "error": 0, "expiring": 0, "expired": 0}
}
```

### 7.3 `POST /admin/accounts`

Import one account, or many at once. Only `hatch_sess` is mandatory.

| Field | Type | Notes |
|---|---|---|
| `cookies` | object | `name → value` |
| `cookie_header` | string | Raw `a=1; b=2` header; merged over `cookies` |
| `expires` | object | `name → unix timestamp` |
| `label` | string | Display name |
| `batch` | string | Multi-line `label \| cookie-string`, one account per line |
| `proxy` | object | `{"server","username","password"}` |

```bash
curl -sS "$BASE/admin/accounts" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "main",
    "cookie_header": "hatch_sess=eyJ...; hatch_vml=eyJ...; _fbp=fb.1...",
    "expires": {"hatch_sess": 1762500000}
  }'
```

```json
{
  "added": [
    {
      "id": "a1b2c3d4e5f6",
      "label": "main",
      "cookie_count": 3,
      "expires_at": 1762400000
    }
  ],
  "count": 1,
  "essential_missing": [],
  "warning": ""
}
```

Batch import:

```bash
curl -sS "$BASE/admin/accounts" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"batch":"main | hatch_sess=eyJ...; hatch_vml=eyJ...\nalt | hatch_sess=eyJ..."}'
```

```json
{
  "added": [
    {"id": "a1b2c3d4e5f6", "label": "main", "cookie_count": 2, "expires_at": 1762400000},
    {"id": "f6e5d4c3b2a1", "label": "alt", "cookie_count": 1, "expires_at": 1762400000}
  ],
  "count": 2,
  "essential_missing": [],
  "warning": ""
}
```

When the core cookie is absent the import still succeeds but reports it:

```json
{
  "added": [{"id": "a1b2c3d4e5f6", "label": "main", "cookie_count": 1, "expires_at": 1762400000}],
  "count": 1,
  "essential_missing": ["hatch_sess"],
  "warning": "Missing core cookies: hatch_sess; this account may not be able to generate"
}
```

### 7.4 `PATCH /admin/accounts/{aid}`

Change the label, enable/disable, or set/clear the proxy.

```bash
curl -sS -X PATCH "$BASE/admin/accounts/a1b2c3d4e5f6" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"label":"main-2","enabled":true,"proxy":{"server":"socks5://127.0.0.1:1080","username":"u","password":"p"}}'
```

```json
{
  "id": "a1b2c3d4e5f6",
  "label": "main-2",
  "enabled": true,
  "note": "Proxy changed; test again",
  "proxy": {"server": "socks5://127.0.0.1:1080", "username": "u", "has_password": true}
}
```

Returns **409** if the account is currently running a task and you change its proxy.

### 7.5 `DELETE /admin/accounts/{aid}`

Removes the account and its cookies from the pool. Generated media is not touched.

```bash
curl -sS -X DELETE "$BASE/admin/accounts/a1b2c3d4e5f6" \
  -H "Authorization: Bearer $KEY"
```

```json
{"deleted": true, "id": "a1b2c3d4e5f6"}
```

Unknown id → **404** `{"detail": "Account not found"}`.

### 7.6 `POST /admin/accounts/{aid}/proxy/test`

Tests egress and the Muse session through a proxy, without saving it.

```bash
curl -sS "$BASE/admin/accounts/a1b2c3d4e5f6/proxy/test" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"proxy":{"server":"socks5://127.0.0.1:1080","username":"u","password":"p"}}'
```

```json
{
  "ok": true,
  "proxy_configured": true,
  "proxy_reachable": true,
  "exit_ip": "198.51.100.7",
  "session_ok": true,
  "workspace_status": "assigned",
  "message": "Proxy egress and Muse session checks passed; verify generation with an account test and a real task",
  "tested_saved_proxy": false
}
```

On failure the same object comes back with `ok: false` and a `message` explaining which
half failed (`proxy_reachable` false = egress problem, `session_ok` false = Muse rejected
the cookies through that proxy).

Omit the body to test the proxy already saved on the account.

### 7.7 `POST /admin/accounts/{aid}/test`

Opens muse.ai for real and verifies the session. Also refreshes the quota cache when the
panel is readable.

```bash
curl -sS "$BASE/admin/accounts/a1b2c3d4e5f6/test" -H "Authorization: Bearer $KEY"
```

```json
{
  "ok": true,
  "message": "Session valid; generation should work",
  "synced": {"hatch_vml": "eyJ...", "_fbp": "fb.1..."},
  "quota": {
    "found": true,
    "plan": "Free plan",
    "weekly_reset": "Nov 12",
    "weekly_used_pct": 29,
    "extra_left": "1B tokens left",
    "extra_used_pct": 3,
    "extra_expires": "never",
    "checked_at": 1762000000
  },
  "quota_error": null
}
```

A failed probe still returns **200** with `ok: false`:

```json
{"ok": false, "message": "Session expired or invalid (HTTP 401)"}
```

`POST /admin/accounts/{aid}/relogin` is an alias of this endpoint.

### 7.8 `POST /admin/accounts/{aid}/cookies`

Replace cookies after a re-login, without recreating the account.

```bash
curl -sS "$BASE/admin/accounts/a1b2c3d4e5f6/cookies" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"cookie_header":"hatch_sess=eyJ...; hatch_vml=eyJ...","expires":{"hatch_sess":1762500000}}'
```

```json
{"ok": true, "id": "a1b2c3d4e5f6", "cookie_count": 2, "expires_at": 1762400000}
```

### 7.9 `POST /admin/relogin`

Refreshes whichever account the pool picks next.

```bash
curl -sS -X POST "$BASE/admin/relogin" -H "Authorization: Bearer $KEY"
```

```json
{"ok": true, "account": "a1b2c3d4e5f6"}
```

Failure returns **502** with `{"ok": false, "error": "..."}`.

### 7.10 Quota

`POST /admin/accounts/{aid}/quota` · `POST /admin/quota`

```bash
# one account, live read
curl -sS -X POST "$BASE/admin/accounts/a1b2c3d4e5f6/quota" -H "Authorization: Bearer $KEY"

# any available account
curl -sS -X POST "$BASE/admin/quota" -H "Authorization: Bearer $KEY"
```

```json
{
  "found": true,
  "plan": "Free plan",
  "weekly_reset": "Nov 12",
  "weekly_used_pct": 29,
  "extra_left": "1B tokens left",
  "extra_used_pct": 3,
  "extra_expires": "never",
  "checked_at": 1762000000
}
```

### 7.11 Keepalive

`GET /admin/keepalive/status` · `POST /admin/accounts/keepalive` ·
`POST /admin/accounts/{aid}/keepalive`

```bash
# status of the background daemon
curl -sS "$BASE/admin/keepalive/status" -H "Authorization: Bearer $KEY"

# run a pass now (force=true by default)
curl -sS -X POST "$BASE/admin/accounts/keepalive" -H "Authorization: Bearer $KEY"

# one account
curl -sS -X POST "$BASE/admin/accounts/a1b2c3d4e5f6/keepalive" -H "Authorization: Bearer $KEY"
```

```json
{
  "running": false,
  "last_run": 1761999000,
  "last_result": null,
  "next_run": 1762000800
}
```

```json
{
  "checked_at": 1762000000,
  "refreshed_count": 1,
  "refreshed": [
    {"id": "a1b2c3d4e5f6", "label": "main", "ok": true, "expires_at": 1762400000}
  ],
  "skipped_count": 1,
  "skipped": [
    {"id": "f6e5d4c3b2a1", "label": "alt", "reason": "Session still fresh and recently kept alive"}
  ]
}
```

If a sweep is already running, the all-accounts call returns
`{"status": "busy", "message": "A keepalive task is already running; try again shortly"}`.

### 7.12 API key

```bash
curl -sS "$BASE/admin/apikey" -H "Authorization: Bearer $KEY"
```

```json
{
  "api_key": "m2a_9f8e7d6c5b4a39281706f5e4d3c2b1a0",
  "base_url": "https://api.example.com/v1",
  "models_url": "https://api.example.com/v1/models",
  "media_url": "https://api.example.com/v1/media/{name}"
}
```

```bash
curl -sS -X POST "$BASE/admin/apikey/rotate" -H "Authorization: Bearer $KEY"
```

```json
{
  "ok": true,
  "api_key": "m2a_1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d",
  "previous": "m2a_9f8e7d6c5b4a39281706f5e4d3c2b1a0",
  "message": "New key generated and applied immediately; the old key is now invalid, update downstream projects"
}
```

### 7.13 Tasks

`GET /admin/tasks` · `DELETE /admin/tasks/{tid}` · `POST /admin/tasks/clear`

```bash
# list (default 50, newest first)
curl -sS "$BASE/admin/tasks?limit=20" -H "Authorization: Bearer $KEY"

# delete one
curl -sS -X DELETE "$BASE/admin/tasks/task_4b3c2d1e0f9a8b7c6d5e" -H "Authorization: Bearer $KEY"

# keep only the newest 10
curl -sS "$BASE/admin/tasks/clear" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"keep":10}'
```

```json
{"tasks": [{"id": "task_4b3c2d1e0f9a8b7c6d5e", "kind": "image", "status": "completed", "progress": 100, "created_at": 1762000000}]}
```

```json
{"deleted": true}
```

```json
{"removed": 37}
```

### 7.14 Media

`GET /admin/media` · `POST /admin/media/delete` · `DELETE /admin/media/{name}`

```bash
curl -sS "$BASE/admin/media" -H "Authorization: Bearer $KEY"
```

```json
{
  "media": [
    {
      "name": "5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4",
      "url": "https://api.example.com/v1/media/5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4",
      "bytes": 4812033,
      "mtime": 1762000160,
      "kind": "video"
    }
  ],
  "count": 1
}
```

```bash
# bulk delete
curl -sS "$BASE/admin/media/delete" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"names":["5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4"]}'

# single delete
curl -sS -X DELETE "$BASE/admin/media/5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4" \
  -H "Authorization: Bearer $KEY"
```

```json
{"removed": 1, "errors": []}
```

```json
{"status": "ok", "deleted": "5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b.mp4"}
```

### 7.15 Extension, cookie helper and console

```bash
curl -sS -OJ "$BASE/admin/extension"          # muse2api-extension.zip
curl -sS "$BASE/admin/extension/files"
curl -sS -OJ "$BASE/admin/cookie-helper?download=1"   # get_muse_cookie.py
```

`GET /` and `GET /admin` serve the HTML console itself (no authentication; the page asks
for the key). `GET /admin/extension` is the zip download the console links to, and
`GET /admin/extension/files` lists its contents.

```json
{
  "files": [
    "_locales/en/messages.json",
    "manifest.json",
    "popup.html",
    "popup.js"
  ]
}
```

### 7.16 Update check and upgrade

`GET /admin/update/check` needs no authentication (`GET /admin/repo/status` is an alias).
`POST /admin/update/upgrade` uses `maintainer_auth`; `POST /admin/repo/pull` is an alias.

```bash
curl -sS "$BASE/admin/update/check?force=true"
```

```json
{
  "repo_url": "https://github.com/czg86389-hub/muse2api",
  "has_git": true,
  "local_version": "1.6.9",
  "remote_version": "1.6.9",
  "local_sha": "a1b2c3d",
  "local_msg": "docs: api reference",
  "local_ts": 1762000000,
  "remote_sha": "a1b2c3d",
  "remote_msg": "docs: api reference",
  "remote_time": "2026-11-08T10:00:00Z",
  "has_update": false,
  "up_to_date": true,
  "highlights": ["…"],
  "highlights_en": ["…"],
  "recent_commits": [
    {"sha": "a1b2c3d", "message": "docs: api reference", "url": "https://github.com/…", "date": "2026-11-08T10:00:00Z"}
  ],
  "checked_at": 1762000000
}
```

```bash
curl -sS -X POST "$BASE/admin/update/upgrade" \
  -H "Authorization: Bearer $MAINTAINER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"restart":true}'
```

```json
{
  "ok": true,
  "via": "git",
  "message": "Successfully updated to the latest version 1.6.9 (a1b2c3d)",
  "status": {"has_update": false, "up_to_date": true, "…": "…"}
}
```

With `restart: true` the process exits ~0.5 s later; the container/supervisor brings it back.

### 7.17 `POST /admin/repo/push`

Maintainer-only. Requires `MUSE2API_MAINTAINER_KEY`; returns **403** when it is unset, and
**401** if the inference key is used instead.

```bash
curl -sS -X POST "$BASE/admin/repo/push" \
  -H "Authorization: Bearer $MAINTAINER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"chore: sync from node"}'
```

```json
{
  "ok": true,
  "committed": true,
  "message": "Successfully committed and pushed to the GitHub repo",
  "status": {"has_update": false, "up_to_date": true, "…": "…"}
}
```

`.env` and `data/` are never pushed. The token is supplied per command and is not written to
`.git/config`.

---

## 8. Errors

### `/v1/*` — OpenAI envelope

```json
{
  "error": {
    "message": "n=2 is not supported: Muse returns one image per generation. Send the request n times if you need several.",
    "type": "invalid_request_error",
    "param": null,
    "code": 400
  }
}
```

| `type` | Status |
|---|---|
| `invalid_request_error` | 400, 422 |
| `not_found_error` | 404 |
| `rate_limit_error` | 429 |
| `server_error` | 5xx |

### `/admin/*` — FastAPI envelope

```json
{"detail": "Account not found"}
```

### Validation failures on `/v1/*`

```json
{
  "error": {
    "message": "Request validation failed: [{'type': 'missing', 'loc': ('body', 'prompt'), 'msg': 'Field required'}]",
    "type": "invalid_request_error",
    "param": null,
    "code": 422
  }
}
```

### Status code summary

| Code | Meaning here |
|---|---|
| 200 | Success, or a failed account probe (`{"ok": false}`) |
| 202 | Image task accepted |
| 400 | Bad request: `n>1`, image content on chat, bad reference, `Idempotency-Key` too long |
| 401 | Missing/invalid API key, or an expired Muse session |
| 403 | `repo/push` without a maintainer key |
| 404 | Unknown task, model, account, media file |
| 409 | Idempotency key reused with a different body; account busy while changing its proxy; video not ready |
| 422 | Schema validation |
| 429 | Queue full (images or video) |
| 502 | Upstream generation failed |
| 500 | Internal error |

---

## 9. Limits and behaviour

### Queue caps

| Setting | Default | Effect |
|---|---|---|
| `MUSE2API_IMAGE_QUEUE_MAX` | 8 | 429 once this many image tasks are queued/processing |
| `MUSE2API_VIDEO_QUEUE_MAX` | 4 | 429 once this many video tasks are queued/processing |
| `MUSE2API_CONCURRENCY` | 1 | Tasks that may run at once (serialised by default) |

### Timeouts

| Setting | Default | Applies to |
|---|---|---|
| `MUSE2API_IMAGE_TIMEOUT` | 240 s | Image generation |
| `MUSE2API_VIDEO_TIMEOUT` | 600 s | Video generation |
| `MUSE2API_CHAT_TIMEOUT` | 300 s | Chat and Responses |

Per-request `timeout` overrides the default within the validated range.

### Reference media

- Up to **10** assets per request
- **40 MiB** per asset, **100 MiB** total
- Accepted: images, video, audio, documents — supplied as a `data:` URL, a public `https://`
  URL, or multipart. Server-local files and private-network fetches are **disabled** unless
  `MUSE2API_REF_IMAGE_LOCAL_FILES=1` / `MUSE2API_REF_IMAGE_PRIVATE_FETCH=1`.

### Idempotency

`Idempotency-Key` is honoured on `POST /v1/images/tasks`, `/v1/images/generations` (async
path) and `/v1/videos`. Responses echo:

```
Idempotency-Key: <your key>
Idempotent-Replayed: true|false
```

The key is namespaced per kind, so a video key can never collide with an image key.

### Polling

Poll a task every 1–3 s. `progress` reports observed milestones only (Muse exposes no
percentage), so a value can stay flat for a while and then jump to 100.

### Streaming

Both chat and Responses stream over SSE. Disable proxy buffering (`proxy_buffering off;` in
nginx) or the typewriter effect will arrive in one burst at the end.

### `n` on images

Only `n=1` is supported. `n>1` returns 400 rather than silently returning one image.

### Chat vision

`image_url` / `input_image` content parts return 400. Use `/v1/images/edits` with the image
as a reference instead.
