"""Turn a configured stream into something a tile can play.

* "web" streams are shown in a browser tile (dashboards, dump1090/tar1090
  maps, weather radar pages, ...).
* "video" streams are played by libVLC. Pages on sites yt-dlp understands
  (YouTube, Twitch, Kick, Vimeo, ...) are first resolved to a direct media
  URL with yt-dlp; everything else (RTSP cameras, HLS .m3u8, UDP/SRT feeds,
  plain files) goes straight to VLC.
"""

import logging
import re
import shutil
import subprocess
import json
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

VIDEO_SCHEMES = {"rtsp", "rtsps", "rtmp", "rtmps", "rtp", "udp", "srt", "mms",
                 "mmsh", "file", "tcp", "dshow", "v4l2", "alsa"}
ADSB_SCHEMES = {"http", "https", "tcp", "sbs"}
WEB_SCHEMES = {"http", "https"}
ALLOWED_SCHEMES = VIDEO_SCHEMES | WEB_SCHEMES | {"sbs"}

MEDIA_EXT = re.compile(
    r"\.(m3u8|m3u|mpd|mp4|m4v|mkv|webm|mov|ts|flv|avi|mp3|aac|ogg|opus|mjpe?g|h264|h265)$",
    re.I)

# Sites whose pages are videos rather than dashboards. Anything else on
# http(s) defaults to a web tile; pick "video" explicitly to force yt-dlp.
YTDLP_HOSTS = (
    "youtube.com", "youtu.be", "twitch.tv", "kick.com", "vimeo.com",
    "dailymotion.com", "rumble.com", "facebook.com", "fb.watch", "x.com",
    "twitter.com", "tiktok.com", "bilibili.com", "streamable.com",
    "livestream.com", "ustream.tv", "angelthump.com", "picarto.tv",
    "trovo.live", "soundcloud.com", "nasa.gov",
)


class ResolveError(Exception):
    pass


class NotLive(ResolveError):
    """A channel that isn't broadcasting right now (or a scheduled stream)."""

    def __init__(self, message, detail="Not live right now"):
        super().__init__(message)
        self.detail = detail


VIDEO_MODES = ("auto", "single", "playlist", "live")

# yt-dlp's wording for "nothing is live right now" across sites
_NOT_LIVE_RE = re.compile(
    r"not currently live|is not live|is offline|not live right now|UserNotLive|"
    r"will begin in|premieres? in|live event will begin|has not started|upcoming", re.I)
_STARTS_RE = re.compile(r"(?:begin|premieres?|starts?)\s+in\s+([^.]+)", re.I)
_TWITCH_NON_CHANNEL = {"videos", "directory", "settings", "downloads", "jobs", "p", "search",
                       "subscriptions", "inventory", "wallet", "drops"}


def video_mode(stream):
    """single, playlist or live for a stream; "auto" decides from the URL.

    Auto: youtube.com/playlist?list=... plays the playlist; a channel's
    /live page (youtube.com/@name/live) and Twitch/Kick channel pages wait
    for the channel to go live. Everything else is a single video/stream.
    """
    mode = stream.get("mode") or "auto"
    if mode in ("single", "playlist", "live"):
        return mode
    url = stream.get("url") or ""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    path = p.path.rstrip("/")
    if _host_matches(host, ("youtube.com",)):
        if path == "/playlist" and "list" in parse_qs(p.query):
            return "playlist"
        if path.endswith("/live"):
            return "live"
    segments = [x for x in path.split("/") if x]
    if _host_matches(host, ("twitch.tv", "kick.com")) and len(segments) == 1 \
            and segments[0].lower() not in _TWITCH_NON_CHANNEL:
        return "live"
    return "single"


def not_live_error(message):
    """NotLive for yt-dlp's "nothing live" messages, else None."""
    if not _NOT_LIVE_RE.search(message or ""):
        return None
    m = _STARTS_RE.search(message)
    detail = ("Scheduled: starts in " + m.group(1).strip()) if m else "Not live right now"
    return NotLive(message, detail)


class Media:
    """What VLC should open: a URL plus per-media options."""

    def __init__(self, url, options=None, title=None, live=None, via="direct"):
        self.url = url
        self.options = options or []
        self.title = title
        self.live = live
        self.via = via

    def __repr__(self):
        return "Media(%r via %s)" % (self.url[:80], self.via)


def validate_url(url):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL is required")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("unsupported URL scheme %r (use one of: %s)"
                         % (parsed.scheme, ", ".join(sorted(ALLOWED_SCHEMES))))
    if parsed.scheme.lower() in WEB_SCHEMES and not parsed.netloc:
        raise ValueError("URL has no host")
    return url


def _host_matches(host, domains):
    host = (host or "").lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def wants_ytdlp(url):
    return _host_matches(urlparse(url).hostname, YTDLP_HOSTS)


def classify(stream):
    """Return "video", "web" or "adsb" for a stream dict."""
    from .adsb import is_adsb_url
    kind = stream.get("kind", "auto")
    url = stream.get("url", "")
    scheme = urlparse(url).scheme.lower()
    if kind == "adsb":
        return "adsb"
    if kind == "auto" and is_adsb_url(url):
        return "adsb"
    if kind == "web":
        return "web" if scheme in WEB_SCHEMES else "video"
    if kind == "video":
        return "video"
    if scheme not in WEB_SCHEMES:
        return "video"
    path = urlparse(url).path
    if MEDIA_EXT.search(path) or wants_ytdlp(url):
        return "video"
    return "web"


def _format_selector(max_height):
    h = int(max_height)
    # Prefer H.264 (hardware-decoded on Pi 4, cheapest in software on Pi 5),
    # muxed audio+video first so VLC only needs one URL.
    return "/".join([
        "best[height<=%d][vcodec^=avc1][acodec!=none]" % h,
        "best[height<=%d][vcodec^=avc][acodec!=none]" % h,
        "bestvideo[height<=%d][vcodec^=avc1]+bestaudio[acodec^=mp4a]" % h,
        "best[height<=%d][acodec!=none][vcodec!=none]" % h,
        "bestvideo[height<=%d]+bestaudio" % h,
        "best[height<=%d]" % h,
        "best",
    ])


def _vlc_header_options(headers):
    opts = []
    if not headers:
        return opts
    ua = headers.get("User-Agent")
    ref = headers.get("Referer")
    if ua:
        opts.append(":http-user-agent=%s" % ua)
    if ref:
        opts.append(":http-referrer=%s" % ref)
    return opts


def _media_from_info(info):
    title = info.get("title")
    live = info.get("is_live")
    requested = info.get("requested_formats")
    if requested and len(requested) >= 2:
        video = next((f for f in requested if f.get("vcodec") not in (None, "none")), requested[0])
        audio = next((f for f in requested if f is not video), requested[1])
        opts = _vlc_header_options(video.get("http_headers") or info.get("http_headers"))
        opts.append(":input-slave=%s" % audio["url"])
        return Media(video["url"], opts, title=title, live=live, via="yt-dlp")
    url = info.get("url")
    if not url:
        raise ResolveError("yt-dlp returned no playable URL")
    opts = _vlc_header_options(info.get("http_headers"))
    return Media(url, opts, title=title, live=live, via="yt-dlp")


def _ytdlp_module():
    try:
        import yt_dlp  # noqa: F401
        return yt_dlp
    except ImportError:
        return None


def playlist_entries(url, limit=1000, timeout=90):
    """Videos in a playlist (public or unlisted: no login), without
    resolving each one. Returns [{"url", "title", "id"}]."""
    mod = _ytdlp_module()
    if mod is not None:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": "in_playlist", "playlistend": limit, "socket_timeout": 20,
                "logger": _QuietLogger()}
        try:
            with mod.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise ResolveError(_short(str(exc))) from exc
    else:
        exe = shutil.which("yt-dlp")
        if not exe:
            raise ResolveError("yt-dlp is not installed")
        try:
            out = subprocess.run([exe, "-J", "--flat-playlist", "--no-warnings", "--playlist-end", str(limit), url],
                                 capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ResolveError("yt-dlp timed out listing the playlist") from exc
        if out.returncode != 0:
            raise ResolveError(_short(out.stderr.strip() or "yt-dlp failed"))
        info = json.loads(out.stdout)
    entries = []
    for e in (info.get("entries") or []) if info.get("_type") == "playlist" or "entries" in info else [info]:
        if not e:
            continue
        title = e.get("title") or ""
        if title in ("[Private video]", "[Deleted video]"):
            continue
        link = e.get("url") or e.get("webpage_url")
        if link and not link.startswith(("http://", "https://")) and e.get("id"):
            link = "https://www.youtube.com/watch?v=" + e["id"]
        elif not link and e.get("id"):
            link = "https://www.youtube.com/watch?v=" + e["id"]
        if link:
            entries.append({"url": link, "title": title, "id": e.get("id") or link})
    if not entries:
        raise ResolveError("playlist is empty, private, or not available without signing in")
    return {"title": info.get("title") or "", "entries": entries}


def _resolve_with_ytdlp(url, max_height, timeout=45):
    fmt = _format_selector(max_height)
    mod = _ytdlp_module()
    if mod is not None:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": fmt,
            "socket_timeout": 20,
            "logger": _QuietLogger(),
        }
        try:
            with mod.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp raises many types
            msg = _short(str(exc))
            raise (not_live_error(str(exc)) or ResolveError(msg)) from exc
    else:
        exe = shutil.which("yt-dlp")
        if not exe:
            raise ResolveError("yt-dlp is not installed")
        try:
            out = subprocess.run(
                [exe, "-J", "--no-playlist", "--no-warnings", "-f", fmt, url],
                capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ResolveError("yt-dlp timed out") from exc
        if out.returncode != 0:
            err = out.stderr.strip() or "yt-dlp failed"
            raise not_live_error(err) or ResolveError(_short(err))
        info = json.loads(out.stdout)
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            raise ResolveError("playlist is empty")
        info = entries[0]
    return _media_from_info(info)


class _QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.info("yt-dlp: %s", msg)


def _short(text, limit=200):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    text = text.replace("ERROR: ", "").strip().splitlines()[-1] if text.strip() else text
    return text if len(text) <= limit else text[: limit - 1] + "…"


def resolve(stream, max_height=720, caching_ms=2000):
    """Resolve a video stream to a Media. Blocking; call from a worker thread."""
    url = stream["url"]
    caching = [":network-caching=%d" % caching_ms]
    scheme = urlparse(url).scheme.lower()
    use_ytdlp = scheme in WEB_SCHEMES and (
        wants_ytdlp(url) or (stream.get("kind") == "video" and not MEDIA_EXT.search(urlparse(url).path))
    )
    if use_ytdlp:
        try:
            media = _resolve_with_ytdlp(url, max_height)
        except NotLive:
            raise
        except ResolveError:
            if wants_ytdlp(url):
                raise
        else:
            if video_mode(stream) == "live" and media.live is False:
                # the channel's page now points at a finished broadcast
                raise NotLive("not live (latest broadcast has ended)")
            media.options += caching
            return media
        log.info("yt-dlp could not handle %s; handing it to VLC directly", url)
    opts = list(caching)
    if scheme in ("rtsp", "rtsps") and stream.get("rtsp_tcp", True):
        opts.append(":rtsp-tcp")
    return Media(url, opts, via="direct")


def ytdlp_version():
    mod = _ytdlp_module()
    if mod is not None:
        try:
            from yt_dlp.version import __version__
            return __version__
        except ImportError:
            return "unknown"
    exe = shutil.which("yt-dlp")
    if exe:
        try:
            return subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
    return None


def has_js_runtime():
    return bool(shutil.which("deno") or shutil.which("node") or shutil.which("bun"))


__all__ = ["Media", "ResolveError", "NotLive", "classify", "resolve", "validate_url",
           "video_mode", "playlist_entries", "not_live_error",
           "ytdlp_version", "has_js_runtime", "wants_ytdlp"]
