"""muse2api configuration."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

_DEFAULT_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Load the project-root .env file automatically (does not override existing
    system environment variables)."""
    base = os.environ.get("MUSE2API_HOME") or _DEFAULT_BASE_DIR
    env_path = os.path.join(base, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _detect_chromium() -> str:
    """Prefer MUSE2API_CHROMIUM; if unset or missing, auto-detect an installed
    Chromium/Chrome."""
    configured = os.environ.get("MUSE2API_CHROMIUM", "").strip()
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured
    candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if os.path.isfile(c) or shutil.which(c):
            return c
    return configured or "chromium"


@dataclass
class Config:
    base_dir: str = field(default_factory=lambda: _env("MUSE2API_HOME", _DEFAULT_BASE_DIR))
    host: str = field(default_factory=lambda: _env("MUSE2API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("MUSE2API_PORT", "18610")))

    # Browser section
    chromium: str = field(default_factory=_detect_chromium)
    cdp_port: int = field(default_factory=lambda: int(_env("MUSE2API_CDP_PORT", "19210")))
    home_dir: str = field(default_factory=lambda: _env("MUSE2API_HOME_DIR", os.path.expanduser("~")))
    extra_path: str = field(default_factory=lambda: _env("MUSE2API_EXTRA_PATH", "/snap/bin"))
    site_url: str = field(default_factory=lambda: _env("MUSE2API_SITE", "https://muse.ai/"))
    login_wait: int = field(default_factory=lambda: int(_env("MUSE2API_LOGIN_WAIT", "60")))

    # Browser profile root.
    # Pitfall: snap-packaged Chromium is confined by AppArmor and can only read/write
    # paths under $HOME. Passing --user-data-dir=/opt/... or /tmp/... does not fail,
    # but is silently ignored and the browser falls back to
    # ~/snap/chromium/common/chromium. The path must therefore stay under $HOME
    # (avoid dot-prefixed hidden directories; snap's home interface excludes them).
    profile_root: str = field(
        default_factory=lambda: _env("MUSE2API_PROFILE_ROOT",
                                     os.path.join(os.path.expanduser("~"),
                                                  "muse2api-profiles")))

    # Authentication
    api_key: str = field(default_factory=lambda: _env("MUSE2API_KEY", ""))
    # Separate credential for code-changing admin actions (/admin/update/upgrade and
    # /admin/repo/push). The inference key is handed to every downstream client, so it
    # must not be able to publish code. Empty means "fall back to api_key" for upgrade,
    # and "push is disabled" for /admin/repo/push.
    maintainer_key: str = field(
        default_factory=lambda: _env("MUSE2API_MAINTAINER_KEY", ""))

    # Public base URL (shown as the "endpoint" in the admin page; when empty the
    # page falls back to the current origin)
    public_base: str = field(
        default_factory=lambda: _env("MUSE2API_PUBLIC_BASE", ""))

    # Origins allowed to call the import endpoint cross-origin (used when the
    # cookie helper is submitted from a muse.ai page)
    cors_origins: str = field(
        default_factory=lambda: _env("MUSE2API_CORS_ORIGINS",
                                     "https://muse.ai,https://www.muse.ai"))

    # Generation
    image_timeout: int = field(default_factory=lambda: int(_env("MUSE2API_IMAGE_TIMEOUT", "240")))
    video_timeout: int = field(default_factory=lambda: int(_env("MUSE2API_VIDEO_TIMEOUT", "600")))
    chat_timeout: int = field(default_factory=lambda: int(_env("MUSE2API_CHAT_TIMEOUT", "300")))
    # How many generation/chat tasks may run at once. They share ONE Chromium process
    # and one CDP connection, each on its own tab (thread-local page), so this is
    # concurrent tab work, not concurrent browsers.
    #
    # Default 1: the browser is a single shared resource, so serialising by default is
    # the honest setting. Raising it lets several tabs interleave and can help when
    # requests spend most of their time waiting on Muse, but it adds contention on the
    # shared CDP connection rather than adding browsers. Measure before raising it.
    max_concurrency: int = field(default_factory=lambda: max(1, int(_env("MUSE2API_CONCURRENCY", "1"))))

    # Admission caps: how many tasks of each kind may be queued or processing at once.
    # Both paths are capped — video used to be unbounded, which let N requests spawn N
    # threads and N task records while the browser could only drain one at a time.
    image_queue_max: int = field(
        default_factory=lambda: max(1, int(_env("MUSE2API_IMAGE_QUEUE_MAX", "8"))))
    video_queue_max: int = field(
        default_factory=lambda: max(1, int(_env("MUSE2API_VIDEO_QUEUE_MAX", "4"))))

    # Chat/responses have no persisted task row like image/video do, so this bounds
    # in-flight /v1/chat/completions + /v1/responses requests directly (counted for the
    # full request duration, including streaming) rather than a store.tasks scan.
    # Each request is lighter than an image/video generation (no file I/O, no task
    # polling) but still holds one account lease and a worker thread for up to
    # MUSE2API_CHAT_TIMEOUT, so a higher default than image/video is reasonable.
    chat_queue_max: int = field(
        default_factory=lambda: max(1, int(_env("MUSE2API_CHAT_QUEUE_MAX", "16"))))

    # Keep at most this many finished task records (0 disables pruning). Each task write
    # rewrites the whole file, so unbounded history makes every update slower.
    task_retention: int = field(
        default_factory=lambda: max(0, int(_env("MUSE2API_TASK_RETENTION", "200"))))

    # Delete generated media older than this many days (0 = keep forever). Files that a
    # queued/processing task still references are never deleted.
    media_retention_days: int = field(
        default_factory=lambda: max(0, int(_env("MUSE2API_MEDIA_RETENTION_DAYS", "0"))))

    # Reference-media input defaults to caller-supplied data and public URLs only.
    # Only trusted single-user deployments should explicitly set these to 1 to
    # restore server-local files or private-network fetching.
    ref_image_local_files: bool = field(
        default_factory=lambda: _env("MUSE2API_REF_IMAGE_LOCAL_FILES", "0").strip() == "1")
    ref_image_private_fetch: bool = field(
        default_factory=lambda: _env("MUSE2API_REF_IMAGE_PRIVATE_FETCH", "0").strip() == "1")

    # Tool-calling (function calling) protocol adapter, disabled by default.
    # Observed: the muse.ai assistant explicitly refuses to emit "pseudo tool calls"
    # (verbatim: "I won't output 'pseudo tool calls' in this JSON format"), and
    # forcing the protocol pollutes normal answers. The default therefore only keeps
    # "parameter compatibility" (a client sending tools does not get a 422); nothing
    # is injected or parsed. If upstream behavior changes, set
    # MUSE2API_TOOL_PROTOCOL=1 to enable this adapter and retry.
    tool_protocol: bool = field(
        default_factory=lambda: _env("MUSE2API_TOOL_PROTOCOL", "0").strip() != "0")

    # Keepalive: whether to run automatic keepalive for disabled accounts
    # (disabled by default). When enabled, accounts manually disabled in the admin
    # console still get background keepalive and VM wake-ups, preserving session
    # liveness, but they never join normal image/video/chat rotation.
    keepalive_disabled_accounts: bool = field(
        default_factory=lambda: _env("MUSE2API_KEEPALIVE_DISABLED_ACCOUNTS", "0").strip() != "0")

    # Background Chromium health check always runs (piggybacked on the keepalive
    # loop's ~15-minute tick) and recycles a crashed or CDP-unresponsive browser.
    # This field adds an *optional* extra trigger: proactively recycle on uptime
    # alone, even if currently healthy. 0 (default) disables it -- only genuinely
    # unhealthy browsers get recycled out of the box.
    browser_max_uptime_hours: float = field(
        default_factory=lambda: max(0.0, float(_env("MUSE2API_BROWSER_MAX_UPTIME_HOURS", "0"))))

    # Detects a "wedged page" that is_healthy() cannot see on its own: is_healthy()
    # only pings the browser-process-level /json/version endpoint, so a single hung
    # renderer/page behind an otherwise-healthy browser process is invisible to it.
    # This counts consecutive generation failures across every image/video/chat
    # request (reset to 0 on any success) and adds a third recycle trigger once the
    # streak reaches this threshold. 0 disables only this trigger -- crash/CDP-
    # unresponsive detection and, if configured, uptime-based recycling still run.
    browser_max_consecutive_failures: int = field(
        default_factory=lambda: max(0, int(_env("MUSE2API_BROWSER_MAX_CONSECUTIVE_FAILURES", "5"))))

    @property
    def data_dir(self) -> str:
        return os.path.join(self.base_dir, "data")

    @property
    def media_dir(self) -> str:
        return os.path.join(self.data_dir, "media")

    @property
    def download_dir(self) -> str:
        return os.path.join(self.data_dir, "downloads")

    @property
    def profile_dir(self) -> str:
        """Browser profile used for generation."""
        return os.path.join(self.profile_root, "generate")

    @property
    def accounts_file(self) -> str:
        return os.path.join(self.data_dir, "accounts.json")

    @property
    def tasks_file(self) -> str:
        return os.path.join(self.data_dir, "tasks.json")

    def ensure_dirs(self):
        for d in (self.base_dir, self.data_dir, self.media_dir, self.download_dir,
                  self.profile_dir):
            os.makedirs(d, exist_ok=True)


CFG = Config()
