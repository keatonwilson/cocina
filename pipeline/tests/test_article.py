"""Article checks and the edit file: no network, no DB."""

import copy

from cocina.article import fill_locators, locate, parse, problems, render

BOOK = """[p. 112] In the 1930s Lebanese immigrants in Puebla sold shawarma cooked on a vertical spit.
[p. 113] Their Mexican-born children swapped the lamb for pork and marinated it with chiles."""
WEB = "Tacos al pastor are usually served with pineapple, onion and cilantro on small corn tortillas."
TEXTS = {41: BOOK, 207: WEB, 9: None}   # 9: a book entered by hand, with no excerpts


def sentence(text, *cites):
    return {"text": text, "cites": [{"source": s, "quote": q, "locator": None} for s, q in cites]}


SECTIONS = [
    {"heading": "From shawarma to the trompo", "paragraphs": [[
        sentence("Lebanese families brought spit-roasted meat to Puebla.",
                 (41, "Lebanese immigrants in Puebla sold shawarma cooked on a vertical spit")),
        sentence("The next generation used pork and a chile marinade.",
                 (41, "swapped the lamb for pork and marinated it with chiles")),
    ], [
        sentence("It comes with pineapple, onion and cilantro.",
                 (207, "served with pineapple, onion and cilantro"), (9, "a quote typed from the book")),
    ]]},
]


def test_a_clean_article_has_no_problems_and_books_get_their_page():
    sections = copy.deepcopy(SECTIONS)
    fill_locators(sections, TEXTS)
    cites = [c for p in sections[0]["paragraphs"] for e in p for c in e["cites"]]
    assert [c["locator"] for c in cites] == ["p. 112", "p. 113", None, None]
    assert problems(sections, TEXTS) == {}
    assert locate("not in the book at all", BOOK) is None


def test_checks_catch_uncited_invented_and_copied_sentences():
    bad = [{"heading": None, "paragraphs": [[
        sentence("Al pastor is beloved."),
        sentence("Pastor came from Greece.", (41, "Greek immigrants in Puebla sold gyros on a spit")),
        sentence("Tacos al pastor are usually served with pineapple, onion and cilantro.",
                 (207, "served with pineapple, onion and cilantro")),
        sentence("Some say otherwise.", (99, "a source that doesn't exist")),
    ]]}]
    out = problems(bad, TEXTS)
    assert "it cites nothing" in out[(0, 0, 0)]
    assert any("isn't in source 41" in w for w in out[(0, 0, 1)])
    assert any("copies 8" in w for w in out[(0, 0, 2)])
    assert any("no source 99" in w for w in out[(0, 0, 3)])
    assert problems([], TEXTS) == {(): ["the article has no sentences"]}


def test_the_edit_file_round_trips():
    sections = copy.deepcopy(SECTIONS)
    fill_locators(sections, TEXTS)
    text = render("How al pastor came to Mexico", sections, {(0, 0, 1): ["just a test"]}, {41: "Pilcher · Planet Taco"})
    assert "% !! just a test" in text
    title, back = parse(text)
    assert title == "How al pastor came to Mexico"
    assert back == sections


def test_an_owner_edit_parses_new_cites_and_paragraphs():
    title, sections = parse('''# Pastor

Intro without a heading. [1]

## Later
One sentence. [1][2]
Two sentence. [2]

---
[1] 41 p. 112 | "Lebanese immigrants in Puebla sold shawarma"
[2] 9 | "a "quoted" phrase"
''')
    assert sections[0]["heading"] is None
    assert [len(p) for s in sections for p in s["paragraphs"]] == [1, 2]
    assert sections[1]["paragraphs"][0][0]["cites"] == [
        {"source": 41, "quote": "Lebanese immigrants in Puebla sold shawarma", "locator": "p. 112"},
        {"source": 9, "quote": 'a "quoted" phrase', "locator": None}]
    for broken in ("# T\n\nA sentence. [3]\n\n---\n[1] 41 | \"q\"\n", "# T\n\nNo cites line at all. [1]\n"):
        try:
            parse(broken)
            raise AssertionError("should not parse")
        except ValueError:
            pass


def test_reword_keeps_a_rewrite_only_when_it_stops_the_copying():
    from types import SimpleNamespace
    from cocina.article import Reworded, Rewording, copied, reword, source_grams

    copying = "Tacos al pastor are usually served with pineapple, onion and cilantro."
    assert copied(copying, source_grams(TEXTS)) == ["tacos al pastor are usually served with pineapple onion and cilantro"]
    sections = [{"heading": None, "paragraphs": [[
        sentence(copying, (207, "served with pineapple, onion and cilantro")),
        sentence("Their Mexican-born children swapped the lamb for pork and marinated it.",
                 (41, "swapped the lamb for pork and marinated it with chiles")),
    ]]}]

    class Stream:   # the model's two rewrites: one in its own words, one still copying
        def __init__(self, **kw):
            assert "1. Tacos al pastor" in kw["messages"][0]["content"]
        def __enter__(self):
            out = Rewording(sentences=[Reworded(n=1, text="Pineapple, onion and cilantro usually come with it."),
                                       Reworded(n=2, text="Their Mexican-born children swapped the lamb for pork and marinated it, too.")])
            usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            return SimpleNamespace(get_final_message=lambda: SimpleNamespace(parsed_output=out, usage=usage))
        def __exit__(self, *a):
            pass

    ai = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=Stream)))
    done, cost = reword(ai, sections, TEXTS)
    first, second = sections[0]["paragraphs"][0]
    assert (done, first["text"]) == (1, "Pineapple, onion and cilantro usually come with it.")
    assert second["text"].startswith("Their Mexican-born children swapped") and not second["text"].endswith("too.")
    assert cost > 0
    assert reword(None, [{"heading": None, "paragraphs": [[first]]}], TEXTS) == (0, 0.0)   # nothing copies: no call
