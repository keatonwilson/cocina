"""The pipeline logic that needs no network or DB: quotes, grouping, page text, robots, noai, auto-review, writeups."""

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
    claim = lambda value: [{"predicate": "uses", "value": value}]  # noqa: E731
    proposals = [{"id": 1, "claim": claim("cheese")}, {"id": 2, "claim": claim("ham")},
                 {"id": 3, "claim": claim("salsa"), "flag": "conflict: an earlier run"},
                 {"id": 4, "claim": claim("Tortilla de Maíz")}, {"id": 5, "claim": [], "evidence": []}]
    flags = [Flag(proposal=2, kind="conflict", why="corn vs flour (P9)"),
             Flag(proposal=3, kind="unsupported", why="re-flagged"),
             Flag(proposal=99, kind="unsupported", why="an id we never sent")]
    rejected = {("uses", "tortilla de maiz"): "conflict: restates R7"}   # the model missed this one
    accept, new = split(proposals, flags, rejected)
    assert accept == [1, 5]                               # 3 still waits for a human
    assert new == {2: "conflict: corn vs flour (P9)", 4: "conflict: restates R7"}  # 99 ignored


def test_a_writeup_must_cite_its_card_and_not_copy_its_quotes():
    from cocina.write import problems
    card = {"claims": [
        {"id": 1, "sources": [{"quote": "Chavindecas are two large corn tortillas grilled with meat and cheese inside."}]},
        {"id": 2, "sources": [{"quote": "a Calentano dish"}]},
    ]}
    ok = [{"text": "Chavindeca is a grilled snack of corn tortillas around meat and cheese.", "claims": [1]}]
    assert problems(ok, card) == []
    copied = [{"text": "They are two large corn tortillas grilled with meat and cheese, locals say.", "claims": [1]}]
    assert problems(copied, card) == ["sentence 1 copies 8+ words of a source quote"]
    stray = [{"text": "It is from Michoacán.", "claims": [1, 9]}, {"text": "Delicious.", "claims": []}]
    assert problems(stray, card) == ["sentence 1 cites claims the card doesn't carry: [9]", "sentence 2 cites nothing"]


def test_all_rows_pages_past_the_row_cap():
    from cocina.db import all_rows
    data = list(range(2500))

    class Query:  # stands in for a PostgREST request capped at 1000 rows
        def range(self, start, end):
            self.rows = data[start:end + 1][:1000]
            return self

        def execute(self):
            return type("Response", (), {"data": self.rows})()

    assert all_rows(Query) == data


def _item(en, es, aliases_es=(), classes=()):
    return {"labels": {"en": {"value": en}, "es": {"value": es}},
            "aliases": {"es": [{"value": a} for a in aliases_es]},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": c}}}} for c in classes]}}


def test_wikidata_links_only_one_exact_food_item():
    from cocina.wikidata import pick
    birria = _item("Birria", "birria", ["birria de chivo"])
    film = _item("Birria", "Birria (película)")               # same name, not a food
    stew = _item("Birria stew", "estofado")                   # a near miss
    food = lambda item: item is not film  # noqa: E731
    assert pick("Birria", {"Q1": birria, "Q2": film, "Q3": stew}, food) == "Q1"
    assert pick("Birria", {"Q1": birria, "Q4": _item("birria", "Birria")}, lambda i: True) is None  # two: ambiguous
    assert pick("Pozole", {"Q1": birria}, food) is None


def test_wikidata_names_become_quotable_claims_for_new_names_only():
    from cocina.research import quote_found
    from cocina.wikidata import page_and_claims
    page, claims = page_and_claims("Q1", _item("Birria", "Birria", ["birria de chivo", "Birria"]), {"birria"})
    assert page["url"] == "https://www.wikidata.org/wiki/Q1"
    assert [c["value"] for c in claims] == ["birria de chivo"]
    assert claims[0]["quote"] == "Also known as (es): birria de chivo"
    assert quote_found(claims[0]["quote"], page["text"])


def test_wikidata_food_walks_up_the_subclass_tree(monkeypatch):
    from cocina import wikidata
    tree = {"Q746549": {"claims": {"P279": [{"mainsnak": {"datavalue": {"value": {"id": "Q2095"}}}}]}},   # dish ⊂ food
            "Q11424": {"claims": {}}}                                                                    # film
    monkeypatch.setattr(wikidata, "_entities", lambda ids, props: {q: tree[q] for q in ids})
    monkeypatch.setattr(wikidata, "_parents", {})
    assert wikidata.is_food(_item("Birria", "Birria", classes=["Q746549"]))
    assert not wikidata.is_food(_item("Birria", "Birria", classes=["Q11424"]))


def test_wikidata_retry_after_is_seconds_capped_at_a_minute():
    from cocina.wikidata import _wait
    assert _wait("120") == 60
    assert _wait(" 7 ") == 7
    assert _wait("Wed, 21 Oct 2026 07:28:00 GMT") == 5   # an HTTP date: don't crash, wait a little
    assert _wait("\xb2") == 5                             # isdigit() would let this through to int()
    assert _wait(None) == 5


def test_a_name_that_belongs_to_another_entity_is_a_clash():
    from cocina.db import fold
    from cocina.review import name_clash
    others = {}
    for slug, names in {"taco-arabe": ["Taco Arabe"], "al-pastor": ["Al Pastor"], "carne-asada": ["Carne asada"],
                        "flauta": ["Flauta", "taquito"], "birria": ["Birria"]}.items():
        for n in names:
            others.setdefault(fold(n), set()).add(slug)
    assert name_clash("tacos árabes", "Al Pastor", others) == "also the name of taco-arabe"     # plural folded
    assert name_clash("Taquitos", "Tacos dorados", others) == "also the name of flauta"
    assert name_clash("tacos al pastor", "Taco", others, family=True) == "names a kind of Taco made with al-pastor, not another name for it"
    assert name_clash("Tacos carne asada", "Taco", others, family=True).endswith("made with carne-asada, not another name for it")
    assert name_clash("fish tacos", "Taco", others, family=True) == "names a kind of Taco, not another name for it"
    assert name_clash("Tacos", "Taco", others, family=True) is None        # just the plural
    others.setdefault("taco", set()).add("taco")
    assert name_clash("tacos al pastor", "Al Pastor", others) is None      # a fine name for Al Pastor itself
    assert name_clash("red tacos", "Quesabirria", others) is None          # a real other name
    assert name_clash("birria taco", "Quesabirria", others) is None        # its own name isn't in it
