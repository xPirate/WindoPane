"""Headline alerts from RSS / Atom feeds.

Only titles are read: each feed is polled on its own interval, and any
headline that wasn't there before becomes an alert that pops up on the wall
or scrolls along the ticker. The first time a feed is checked its current
headlines are only remembered, so adding a feed doesn't flood the screen.
Remembered headlines survive restarts (alerts-seen.json next to the config).
"""

import email.utils
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import __version__

log = logging.getLogger(__name__)

TIMEOUT_S = 20
MAX_BYTES = 4 * 1024 * 1024
TICK_S = 5
SEEN_PER_FEED = 400          # remembered headline ids per feed
MAX_NEW_PER_POLL = 5         # a burst beyond this is remembered, not shown
MAX_AGE_S = 2 * 86400        # headlines dated older than this never alert
USER_AGENT = "Mozilla/5.0 (compatible; windoPane/%s; RSS headline alerts)" % __version__

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


class FeedError(Exception):
    pass


# ---------------------------------------------------------------------- parsing
def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el):
    if el is None:
        return ""
    return "".join(el.itertext())


def clean_title(text):
    """Headline text without HTML tags, entities or runs of whitespace."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)  # double-escaped feeds
    return re.sub(r"\s+", " ", text).strip()


def parse_date(text):
    """Epoch seconds from an RSS (RFC 822) or Atom (ISO 8601) date, else None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        iso = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _child(el, *names):
    """First direct child whose local name is in names."""
    for kid in el:
        if _local(kid.tag) in names:
            return kid
    return None


def _item(el):
    title = clean_title(_text(_child(el, "title")))
    link_el = _child(el, "link")
    link = ""
    if link_el is not None:
        link = (link_el.get("href") or _text(link_el)).strip()
        if _local(el.tag) == "entry":  # Atom: prefer rel="alternate"
            for kid in el:
                if _local(kid.tag) == "link" and kid.get("rel", "alternate") == "alternate" and kid.get("href"):
                    link = kid.get("href").strip()
                    break
    guid = _text(_child(el, "guid", "id")).strip()
    date = _text(_child(el, "pubDate", "published", "updated", "date", "issued", "modified"))
    return {
        "id": guid or link or title,
        "title": title,
        "link": link,
        "published": parse_date(date),
    }


def parse_feed(data):
    """(feed title, [items]) from RSS 2.0, RSS 1.0 (RDF) or Atom bytes.
    Items keep the feed's order; each has id, title, link and published."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        head = data[:400].decode("utf-8", "replace").lower() if isinstance(data, bytes) else ""
        if "<html" in head:
            raise FeedError("that address is a web page, not an RSS or Atom feed") from exc
        raise FeedError("not a readable feed (%s)" % exc) from exc
    kind = _local(root.tag)
    if kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("RSS feed without a channel")
        title = clean_title(_text(_child(channel, "title")))
        entries = [el for el in channel if _local(el.tag) == "item"]
    elif kind == "RDF":
        channel = _child(root, "channel")
        title = clean_title(_text(_child(channel, "title"))) if channel is not None else ""
        entries = [el for el in root if _local(el.tag) == "item"]
    elif kind == "feed":
        title = clean_title(_text(_child(root, "title")))
        entries = [el for el in root if _local(el.tag) == "entry"]
    else:
        raise FeedError("not an RSS or Atom feed (root element <%s>)" % kind)
    items = [it for it in (_item(el) for el in entries) if it["title"]]
    return title, items


def matches(title, match):
    """True when the title contains any of the comma-separated words (or no filter)."""
    words = [w.strip().lower() for w in (match or "").split(",") if w.strip()]
    if not words:
        return True
    low = title.lower()
    return any(w in low for w in words)


# ---------------------------------------------------------------------- fetching
def fetch(url, etag=None, modified=None):
    """(status, body, etag, last_modified). status 304 means unchanged."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/rdf+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
    }
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FeedError("feed is larger than %d MB" % (MAX_BYTES // 1048576))
            return resp.status, body, resp.headers.get("ETag"), resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", etag, modified
        raise FeedError("the site answered HTTP %d %s" % (exc.code, exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise FeedError("can't reach the feed: %s" % getattr(exc, "reason", exc)) from exc
    except (OSError, ValueError) as exc:
        raise FeedError("can't read the feed: %s" % exc) from exc


def probe(url, match=""):
    """For the web UI's Test button: the feed's title and newest headlines."""
    _status, body, _e, _m = fetch(url)
    title, items = parse_feed(body)
    shown = [it for it in items if matches(it["title"], match)]
    return {"title": title, "count": len(items), "matching": len(shown), "items": shown[:5]}


# ---------------------------------------------------------------------- watcher
class AlertWatcher(threading.Thread):
    """Polls every enabled feed on its interval and raises new headlines."""

    def __init__(self, ctl, fetcher=None, state_path=None):
        super().__init__(name="alerts", daemon=True)
        self.ctl = ctl
        self.fetch = fetcher or fetch
        self.state_path = state_path or os.path.join(
            os.path.dirname(os.path.abspath(ctl.path)), "alerts-seen.json")
        self.wake = threading.Event()
        self.force = set()          # feed ids to check on the next pass
        self.next_check = {}        # feed id -> monotonic time
        self.state = self._load()   # feed id -> {"url", "seen": [...], "etag", "modified"}
        self.status = {}            # feed id -> what the web UI shows
        self.latest = {}            # feed id -> newest few items (for "Show latest")
        self._dirty = False
        self._lock = threading.Lock()
        ctl.alert_watcher = self

    # -- persistence
    def _load(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        if not self._dirty:
            return
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.state_path)
            self._dirty = False
        except OSError as exc:
            log.warning("could not save alert state: %s", exc)

    # -- requests from the web UI
    def check_now(self, feed_id):
        self.force.add(feed_id)
        self.wake.set()

    def feed_status(self, feed_id):
        return dict(self.status.get(feed_id) or {})

    # -- polling
    def poll_once(self, now=None):
        now = time.monotonic() if now is None else now
        cfg = self.ctl.cfg.get("alerts") or {}
        feeds = list(cfg.get("feeds") or [])
        ids = {f["id"] for f in feeds}
        for fid in list(self.state):
            if fid not in ids:
                del self.state[fid]
                self._dirty = True
        for fid in list(self.next_check):
            if fid not in ids:
                del self.next_check[fid]
                self.status.pop(fid, None)
                self.latest.pop(fid, None)
        for feed in feeds:
            fid = feed["id"]
            forced = fid in self.force
            if not forced and (not cfg.get("enabled", True) or not feed.get("enabled", True)):
                continue
            if not forced and now < self.next_check.get(fid, 0):
                continue
            self.force.discard(fid)
            self.next_check[fid] = now + max(1, int(feed.get("every_minutes") or 5)) * 60
            self.check_feed(feed)
        self._save()

    def check_feed(self, feed):
        fid = feed["id"]
        st = self.state.get(fid)
        if st is None or st.get("url") != feed["url"]:
            st = {"url": feed["url"], "seen": None, "etag": None, "modified": None}
            self.state[fid] = st
            self._dirty = True
        status = self.status.setdefault(fid, {})
        status["checked"] = time.time()
        try:
            code, body, etag, modified = self.fetch(feed["url"], st.get("etag"), st.get("modified"))
            if code == 304 and st.get("seen") is not None:
                status.update(ok=True, error=None, unchanged=True)
                return []
            title, items = parse_feed(body)
        except FeedError as exc:
            status.update(ok=False, error=str(exc))
            log.info("feed %s: %s", feed.get("name") or feed["url"], exc)
            return []
        except Exception as exc:  # never let one feed stop the others
            status.update(ok=False, error="unexpected error: %s" % exc)
            log.exception("feed %s", feed["url"])
            return []
        st["etag"], st["modified"] = etag, modified
        status.update(ok=True, error=None, unchanged=False, title=title, count=len(items))
        self.latest[fid] = [it for it in items if matches(it["title"], feed.get("match"))][:5]
        if st["seen"] is None:
            # first look at this feed: remember what's there, alert on nothing
            st["seen"] = [it["id"] for it in items][:SEEN_PER_FEED]
            self._dirty = True
            status["seeded"] = True
            log.info("feed %s: watching (%d headlines already posted)", feed.get("name") or feed["url"], len(items))
            return []
        seen = set(st["seen"])
        fresh = [it for it in items if it["id"] not in seen]
        if not fresh:
            return []
        st["seen"] = ([it["id"] for it in fresh] + st["seen"])[:SEEN_PER_FEED]
        self._dirty = True
        cutoff = time.time() - MAX_AGE_S
        shown = [it for it in fresh
                 if matches(it["title"], feed.get("match")) and not (it["published"] and it["published"] < cutoff)]
        # newest first when the feed dates its items, else feed order (usually newest first)
        if all(it["published"] for it in shown):
            shown.sort(key=lambda it: it["published"], reverse=True)
        if len(shown) > MAX_NEW_PER_POLL:
            log.info("feed %s: %d new headlines, showing the newest %d",
                     feed.get("name"), len(shown), MAX_NEW_PER_POLL)
            shown = shown[:MAX_NEW_PER_POLL]
        # oldest of the batch first, so the newest ends up on top of the stack
        for it in reversed(shown):
            self.ctl.raise_alert(feed, it["title"], it["link"], it["published"])
        if shown:
            status["last_alert"] = time.time()
        return shown

    def show_latest(self, feed):
        """Put the feed's newest headline on screen now (the web UI's test button)."""
        items = self.latest.get(feed["id"])
        if not items:
            _code, body, _e, _m = self.fetch(feed["url"], None, None)
            _title, all_items = parse_feed(body)
            items = [it for it in all_items if matches(it["title"], feed.get("match"))][:5]
            self.latest[feed["id"]] = items
        if not items:
            raise FeedError("the feed has no headlines%s" % (" matching the filter" if feed.get("match") else ""))
        it = items[0]
        return self.ctl.raise_alert(feed, it["title"], it["link"], it["published"], test=True)

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception:
                log.exception("alert watcher")
            self.wake.wait(TICK_S)
            self.wake.clear()
