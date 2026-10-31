"""The auditor's judgement of each page and each gone quote (no network, no DB)."""

from email.message import Message

from cocina import fetch
from cocina.audit import check, decide, kind
from cocina.fetch import Skipped

TEXT = "Memelas are thick oval masa cakes from Oaxaca, pinched at the edge and topped with asiento."
OLD = {"text": TEXT, "content_hash": "old", "fetched_at": "2026-10-25T00:00:00+00:00"}
QUOTES = [{"evidence": 1, "claim": 10, "quote": "thick oval masa cakes from Oaxaca"},
          {"evidence": 2, "claim": 11, "quote": "topped with asiento"},
          {"article": "memela", "quote": "pinched at the edge"}]


def page(text=TEXT, h="new"):
    return lambda url: {**OLD, "text": text, "content_hash": h}


def fails(why):
    def f(url):
        raise Skipped(why)
    return f


def src(failures=0):
    return {"url": "https://example.com/memela", "content_hash": "old", "failures": failures, "text": TEXT,
            "quotes": QUOTES}


def test_each_kind_of_page():
    same = check(src(), page(h="old"))
    assert (same["outcome"], same["gone"], same["page"]) == ("same", [], None)
    changed = check(src(), page(TEXT + " Some new words at the end."))
    assert (changed["outcome"], changed["gone"]) == ("changed", []) and changed["page"]   # stored: every quote holds
    dropped = check(src(), page(TEXT.replace("topped with asiento", "topped with beans")))
    assert dropped["outcome"] == "dropped" and dropped["gone"] == [QUOTES[1]] and dropped["page"] is None
    assert check(src(), fails("page opts out of AI use (noai)"))["gone"] == QUOTES      # an opt-out: all gone
    for why in ("disallowed by robots.txt", "redirect to https://x.example/a is disallowed by robots.txt", "HTTP 403"):
        assert check(src(), fails(why)) == {"outcome": "blocked", "gone": [], "page": None, "error": why}


def test_another_page_served_in_its_place_is_not_a_rewrite():
    wall = "Pardon our interruption. Something about your browser made us think you were a robot. Enable cookies."
    r = check(src(), page(wall))
    assert (r["outcome"], r["gone"], r["page"]) == ("unlike", [], None)   # unchecked: nothing retired, nothing stored
    rewrite = "Memelas: thick masa cakes from Oaxaca, pinched at the edge, then topped with beans and cheese."
    assert check(src(), page(rewrite))["outcome"] == "dropped"           # the same page, reworded: its quotes are gone


def test_a_page_dies_on_its_third_failure_in_a_row():
    for why in ("HTTP 404", "unreachable: URLError('timed out')", "robots.txt can't be read (unreachable)"):
        assert kind(why) == "failed"
        assert check(src(failures=1), fails(why))["outcome"] == "failing"
        assert check(src(failures=1), fails(why))["gone"] == []
        assert check(src(failures=2), fails(why))["outcome"] == "dead"
        assert check(src(failures=2), fails(why))["gone"] == QUOTES


def test_a_site_that_is_down_is_not_refusing_us(monkeypatch):
    replies = {"https://down.example": (503, Message(), b"", ""), "https://gone.example": OSError("no route"),
               "https://rules.example": (200, Message(), b"User-agent: *\nDisallow: /\n", "")}

    def fake_get(url, opener=None):
        reply = replies[url.removesuffix("/robots.txt")]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(fetch, "_get", fake_get)
    monkeypatch.setattr(fetch, "_robots", {})
    monkeypatch.setattr(fetch, "_robots_down", {})
    whys = {}
    for host in replies:
        try:
            fetch.fetch_page(host + "/page")
        except Skipped as e:
            whys[host] = str(e)
    assert whys == {"https://down.example": "robots.txt can't be read (HTTP 503)",
                    "https://gone.example": "robots.txt can't be read (unreachable)",
                    "https://rules.example": "disallowed by robots.txt"}
    assert [kind(w) for w in whys.values()] == ["failed", "failed", "blocked"]


def test_a_claim_loses_a_quote_alone_only_when_another_backs_it():
    gone = {10: [QUOTES[0]], 11: [QUOTES[1]], 12: [{"evidence": 3, "claim": 12, "quote": "…"}]}
    auto, person = decide(gone, backing={10: 1}, skip={12})
    assert auto == {10: [QUOTES[0]]}     # retired now: the claim keeps another credited quote
    assert person == {11: [QUOTES[1]]}   # its last quote: a person decides; 12 was kept before
