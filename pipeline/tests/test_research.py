"""The pipeline's pure logic: the quote check, grouping and page text. No network, no DB."""

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
