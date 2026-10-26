"""The pipeline logic that needs no network or DB: quote check, grouping, page text, robots, noai, auto-review."""

import urllib.request
from email.message import Message

import pytest

from cocina import fetch
from cocina.fetch import html_to_text
from cocina.research import group_claims, quote_found

PAGE = """Chavindecas come from Chavinda, Michoacán.\nThey are two corn tortillas
with meat and cheese, elsewhere called “sincronizadas”."""


def test_quote_check_keeps_real_quotes_and_drops_invented_ones():
    assert quote_found("Chavindecas come from Chavinda, Michoacán.", PAGE)
    # whitespace, line breaks and curly quotes don't matter
    assert quote_found('two corn tortillas with meat and cheese, elsewhere called "sincronizadas"', PAGE)
    assert not quote_found("Chavindecas come from Puebla.", PAGE)
    assert not quote_found("Chavinda", PAGE)  # too short to stand as evidence


def test_agreeing_sources_become_one_claim_with_evidence_from_each():
    claims = [
        {"page": 1, "predicate": "originates_in", "value": "Chavinda, Michoacán", "object_name": None, "quote": "a"},
        {"page": 2, "predicate": "originates_in", "value": "chavinda, michoacan", "object_name": "Michoacán", "quote": "b"},
        {"page": 3, "predicate": "originates_in", "value": "Chavinda,  Michoacán", "object_name": None, "quote": "c"},
        {"page": 3, "predicate": "originates_in", "value": "Chavinda, Michoacán", "object_name": None, "quote": "c2"},
        {"page": 1, "predicate": "uses", "value": "corn tortilla", "object_name": None, "quote": "d"},
    ]
    groups = group_claims(claims)
    origin = next(g for g in groups if g["predicate"] == "originates_in")
    assert len(groups) == 2
    assert [e["page"] for e in origin["evidence"]] == [1, 2, 3]  # one evidence row per page
    assert origin["object_name"] == "Michoacán"


def test_page_text_skips_chrome_and_sees_noai():
    title, text, noai = html_to_text(
        "<html><head><title>Birria</title><meta name='robots' content='noai, noimageai'>"
        "<script>var x=1</script></head><body><nav>Home | Recipes</nav>"
        "<p>Birria is from Jalisco.</p><footer>© Site</footer></body></html>"
    )
    assert title == "Birria"
    assert text == "Birria is from Jalisco."
    assert noai


def test_robots_txt_follows_rfc_9309(monkeypatch):
    replies = {
        "https://gone.example": (404, Message(), b"", ""),           # missing: allowed
        "https://down.example": (503, Message(), b"", ""),           # server error: disallowed
        "https://private.example": (403, Message(), b"", ""),        # forbidden: disallowed
        "https://unreachable.example": ConnectionResetError(),       # no answer: disallowed
        "https://rules.example": (200, Message(), b"User-agent: *\nDisallow: /private\n", ""),
    }

    def fake_get(url, opener=None):
        reply = replies[url.removesuffix("/robots.txt")]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(fetch, "_get", fake_get)
    monkeypatch.setattr(fetch, "_robots", {})
    assert fetch.allowed("https://gone.example/a")
    assert not fetch.allowed("https://down.example/a")
    assert not fetch.allowed("https://private.example/a")
    assert not fetch.allowed("https://unreachable.example/a")
    assert "https://unreachable.example" not in fetch._robots  # asked again next time
    assert fetch.allowed("https://rules.example/a")
    assert not fetch.allowed("https://rules.example/private/a")


def test_noai_header_skips_the_page(monkeypatch):
    headers = Message()
    headers["Content-Type"] = "text/html"
    headers["X-Robots-Tag"] = "noai"
    monkeypatch.setattr(fetch, "allowed", lambda url: True)
    monkeypatch.setattr(fetch, "_get", lambda url, opener=None: (200, headers, b"<p>" + b"words " * 100 + b"</p>", url))
    with pytest.raises(fetch.Skipped, match="noai"):
        fetch.fetch_page("https://example.com/page")


def test_a_redirect_is_followed_only_where_robots_allows(monkeypatch):
    monkeypatch.setattr(fetch, "allowed", lambda url: "blocked" not in url)
    req = urllib.request.Request("https://ok.example/a")
    with pytest.raises(fetch.Skipped, match="redirect"):
        fetch._Polite().redirect_request(req, None, 302, "Found", Message(), "https://blocked.example/b")
    assert fetch._Polite().redirect_request(req, None, 302, "Found", Message(), "https://ok.example/b")


def test_auto_review_accepts_only_what_it_was_asked_about_and_did_not_flag():
    from types import SimpleNamespace as Flag

    from cocina.review import split
    proposals = [{"id": 1}, {"id": 2}, {"id": 3, "flag": "conflict: an earlier run"}]
    flags = [Flag(proposal=2, kind="conflict", why="corn vs flour (P9)"),
             Flag(proposal=3, kind="unsupported", why="re-flagged"),
             Flag(proposal=99, kind="unsupported", why="an id we never sent")]
    accept, new = split(proposals, flags)
    assert accept == [1]                                  # 3 still waits for a human
    assert new == {2: "conflict: corn vs flour (P9)"}     # 3 keeps its first flag, 99 is ignored
