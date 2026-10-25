"""Fetch a page politely and keep the text that quotes are checked against."""

import hashlib
import re
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

UA = "cocina-research/0.1 (+https://github.com/keatonwilson/cocina)"
TIMEOUT = 20
MAX_BYTES = 3_000_000
SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button"}
BREAK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote"}


class Skipped(Exception):
    """The page can't or mustn't be used (robots, noai, not HTML, HTTP error)."""


class _Text(HTMLParser):
    # ponytail: crude main-text extraction, swap in trafilatura if pages come back noisy
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.noai = False
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = {k: (v or "").lower() for k, v in attrs}
            if a.get("name") in ("robots", "googlebot", "ccbot") and "noai" in a.get("content", ""):
                self.noai = True
        if tag in BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str, bool]:
    """Return (title, text, noai) for an HTML page."""
    p = _Text()
    p.feed(html)
    lines = (" ".join(line.split()) for line in "".join(p.parts).splitlines())
    return " ".join(p.title.split()), "\n".join(line for line in lines if line), p.noai


def _get(url: str) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, "", b""


_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


def allowed(url: str) -> bool:
    """robots.txt fetched with our own UA (the stdlib reader's default UA is often blocked)."""
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            status, _, body = _get(root + "/robots.txt")
        except (urllib.error.URLError, TimeoutError):
            status, body = 404, b""
        if status in (401, 403):
            rp.disallow_all = True
        elif status >= 400:
            rp.allow_all = True
        else:
            rp.parse(body.decode("utf-8", "replace").splitlines())
        _robots[root] = rp
    return _robots[root].can_fetch(UA, url)


def fetch_page(url: str) -> dict:
    """Fetch one page → {url, title, text, content_hash, fetched_at}. Raises Skipped."""
    if not allowed(url):
        raise Skipped("disallowed by robots.txt")
    try:
        status, ctype, body = _get(url)
    except (urllib.error.URLError, TimeoutError) as e:
        raise Skipped(f"unreachable: {e}") from e
    if status >= 400:
        raise Skipped(f"HTTP {status}")
    if "html" not in ctype and "text/plain" not in ctype:
        raise Skipped(f"not a web page ({ctype or 'no content type'})")
    m = re.search(r"charset=([\w-]+)", ctype)
    raw = body.decode(m.group(1) if m else "utf-8", "replace")
    if "html" in ctype:
        title, text, noai = html_to_text(raw)
    else:
        title, text, noai = "", raw, False
    if noai:
        raise Skipped("page opts out of AI use (noai)")
    if len(text) < 200:
        raise Skipped("no readable text")
    return {
        "url": url,
        "title": title,
        "text": text,
        # Hash of the extracted text, so the auditor sees content changes, not ad churn
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
