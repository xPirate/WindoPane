"""A stand-in for the yt_dlp module, for tests: answers like YouTube does
for a playlist, its videos, and a channel's /live page."""

import os


class DownloadError(Exception):
    pass


MEDIA = os.environ.get("FAKE_YTDLP_MEDIA", "http://127.0.0.1:8765")
LIVE_FLAG = os.environ.get("FAKE_YTDLP_LIVE_FLAG", "/nonexistent/live-flag")
CALLS = []

PLAYLIST = {
    "_type": "playlist", "title": "Test playlist",
    "entries": [
        {"_type": "url", "id": "AAA", "url": "https://www.youtube.com/watch?v=AAA", "title": "First video"},
        {"_type": "url", "id": "GONE", "url": "https://www.youtube.com/watch?v=GONE", "title": "Removed video"},
        {"_type": "url", "id": "PRIV", "url": "https://www.youtube.com/watch?v=PRIV", "title": "[Private video]"},
        {"_type": "url", "id": "BBB", "url": "https://www.youtube.com/watch?v=BBB", "title": "Second video"},
    ],
}
_LONG = bool(os.environ.get("FAKE_YTDLP_LONG"))
VIDEOS = {"AAA": ("clip_a.mp4" if _LONG else "short_a.mp4", "First video"),
          "BBB": ("clip_b.mp4" if _LONG else "short_b.mp4", "Second video")}


class YoutubeDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        CALLS.append((url, bool(self.opts.get("extract_flat"))))
        if "playlist?list=" in url:
            if not self.opts.get("extract_flat"):
                raise DownloadError("test: playlists must be listed flat")
            return PLAYLIST
        if "watch?v=" in url:
            vid = url.split("watch?v=")[1].split("&")[0]
            if vid not in VIDEOS:
                raise DownloadError("ERROR: [youtube] %s: Video unavailable" % vid)
            name, title = VIDEOS[vid]
            return {"id": vid, "title": title, "is_live": False, "url": "%s/%s" % (MEDIA, name),
                    "http_headers": {"User-Agent": "test"}}
        if url.rstrip("/").endswith("/live"):
            if os.path.exists(LIVE_FLAG):
                return {"id": "LIVE1", "title": "Channel live now", "is_live": True,
                        "url": "%s/hls/live.m3u8" % MEDIA}
            if "upcoming" in url:
                raise DownloadError("ERROR: [youtube] LIVE2: This live event will begin in 25 minutes.")
            raise DownloadError("ERROR: [youtube:tab] @testchan: The channel is not currently live")
        raise DownloadError("ERROR: Unsupported URL: " + url)


class version:  # noqa: N801 - mirrors yt_dlp.version
    __version__ = "fake"
