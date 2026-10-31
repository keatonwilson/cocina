"""Articles: long-form history and technique, every sentence citing a place in a source.

    python -m cocina.article sources ../briefs/birria.toml   # store the brief's sources, print their ids
    python -m cocina.article draft ../briefs/birria.toml     # sources, search, one drafting call → a proposal
    python -m cocina.article edit birria                     # the draft in $EDITOR, checked on save
    python -m cocina.article publish birria                  # the checks, then accept_proposal as you
    python -m cocina.article drop birria --why "…"           # reject the draft

A brief is a TOML file in the git-ignored briefs/ at the repo root (book excerpts
stay out of the public repo):

    slug = "birria"
    title = "How birria came to be"
    angle = "Where birria comes from, how it was cooked then and now, and how it travelled."
    words = 1200                  # optional, about how long
    search = true                 # the drafter may add up to 4 pages it finds itself

    [[source]]
    id = 41                       # already in the knowledgebase

    [[source]]
    url = "https://…"             # fetched like research's pages (robots, noai)
    authors = ["…"]               # optional; the publisher defaults to the site

    [[source]]
    work_type = "book"            # or interview, article, video, archive: anything without a URL
    title = "…"
    authors = ["…"]
    publisher = "…"
    published_on = 2012-04-01     # optional
    isbn = "…"                    # optional
    citation = "…"                # optional: the full citation, as it should be shown
    excerpts = "birria-book.txt"  # next to the brief: your excerpts, each after its [p. 112] or [12:30] marker

Every sentence cites at least one (source, quote, locator). Checked in code: each
quote is in the text we hold of its source, and no sentence copies 8 words in a row
from any of the article's sources; a book's or interview's locator is the marker
before the quote. The database checks the rest at publish (`article_ok`), and only a
person publishes: accept_proposal refuses auto-review and the drafter.
"""

import argparse
import getpass
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import anthropic
from pydantic import BaseModel

from cocina.db import client as db
from cocina.fetch import Skipped, fetch_page
from cocina.research import FALLBACK, MODEL, PAGE_CHARS, _cost, _flat, find_urls, quote_found, save_source
from cocina.review import credit
from cocina.write import COPY_WORDS, _ngrams

REPO = Path(__file__).resolve().parents[2]
MAX_FOUND = 4            # pages the drafter may add to the owner's
# ponytail: whole sources in one prompt (~100k tokens); passages + pgvector when a brief outgrows it
MAX_CHARS = 400_000
# A page or a timestamp, as _flat() leaves it: [p. 112], [p. xii], [p. 12-13], [12:30], [1:02:03]
MARKER = re.compile(r"\[(p\. ?[0-9ivxlcdm]+(?:-[0-9ivxlcdm]+)?|\d{1,2}:\d{2}(?::\d{2})?)\]")

SEARCH_PROMPT = """Search the web for reliable sources for an article about Mexican food: "{title}". {angle}
Run two or three searches, in English and in Spanish. Prefer historians, cookbook authors, food
writers, encyclopedias, museums and Mexican institutions over recipe aggregators, social media and
restaurant pages. When you have searched, reply with the single word: done."""

DRAFT_SYSTEM = """You draft a long-form article about Mexican food history or technique for a \
knowledgebase in which every sentence is cited. A person edits and publishes it.

Rules:
- Use only the sources provided. Never add anything from memory: no date, place, person, number, \
origin story or comparison that the sources don't state.
- Every sentence cites at least one source in `cites`: the source's number and `quote`, an excerpt \
copied character for character from that source, one sentence or less and under 300 characters, \
that states what the sentence says. Never paraphrase or translate a quote. A sentence that only \
leads into others ("Sources differ on the date.") has nothing to cite: join it to the sentence that \
states the facts.
- Write in your own words: never copy eight or more words in a row from a source, even a common \
phrase or a list of ingredients. Reorder or shorten it.
- Don't name sources in the text ("Wikipedia says"): the citations credit them. Where sources \
disagree, say so, name who says what, and cite each side rather than choosing one.
- Sections with short plain headings, each of one to four paragraphs. About {words} words in all; \
shorter is fine when the sources hold less.
- Plain English for a curious reader. No praise, no filler, and no closing summary.
- `credits`: for every source marked FOUND that you cite, its title, the people credited as \
authors (an empty list if none), the publisher (site or organisation name) and the publication \
date if the page states one (YYYY-MM-DD)."""


class Cite(BaseModel):
    source: int
    quote: str


class Sentence(BaseModel):
    text: str
    cites: list[Cite]


class Section(BaseModel):
    heading: str
    paragraphs: list[list[Sentence]]


class Credit(BaseModel):
    source: int
    title: str
    authors: list[str]
    publisher: str
    published_on: str | None


class Draft(BaseModel):
    sections: list[Section]
    credits: list[Credit]


# ---------------------------------------------------------------------------
# Checks (no network): the same ones at draft, edit and publish
# ---------------------------------------------------------------------------

def sentences(sections: list[dict]):
    """Each sentence with its place: (section, paragraph, sentence)."""
    for i, s in enumerate(sections):
        for j, p in enumerate(s["paragraphs"]):
            for k, e in enumerate(p):
                yield (i, j, k), e


def locate(quote: str, text: str) -> str | None:
    """The page or timestamp a quote sits under: the last [p. 112] or [12:30] marker before it."""
    flat = _flat(text)
    at = flat.find(_flat(quote))
    marks = MARKER.findall(flat[:at]) if at >= 0 else []
    return marks[-1] if marks else None


def fill_locators(sections: list[dict], texts: dict[int, str | None]) -> None:
    """A source with markers (a book's excerpts, a transcript) gets its locator from them, not from anyone's say."""
    for _, e in sentences(sections):
        for c in e["cites"]:
            text = texts.get(c["source"])
            if text and MARKER.search(_flat(text)):
                c["locator"] = locate(c["quote"], text)


def problems(sections: list[dict], texts: dict[int, str | None], also: list[str] = ()) -> dict[tuple, list[str]]:
    """What stops these sections publishing, by sentence place (() for the whole article).
    `texts`: every source a cite may name → the text we hold of it (None: entered by hand, taken on
    the owner's word). `also`: other texts the article drew on, for the copying check."""
    grams = set().union(*(_ngrams(t) for t in [*texts.values(), *also] if t))
    out: dict[tuple, list[str]] = {}
    if not any(True for _ in sentences(sections)):
        out[()] = ["the article has no sentences"]
    for place, e in sentences(sections):
        why = []
        if not e["text"].strip():
            why.append("an empty sentence")
        if not e["cites"]:
            why.append("it cites nothing")
        for c in e["cites"]:
            sid, quote = c["source"], c["quote"]
            if sid not in texts:
                why.append(f"there's no source {sid} in the knowledgebase")
            elif not 1 <= len(quote) <= 400:
                why.append(f"the quote from source {sid} is {len(quote)} characters (1–400)")
            elif texts[sid] is not None and not quote_found(quote, texts[sid]):
                why.append(f'the quote isn\'t in source {sid}: "{quote[:70]}"')
        if _ngrams(e["text"]) & grams:
            why.append(f"it copies {COPY_WORDS} or more words in a row from a source")
        if why:
            out[place] = why
    return out


# ---------------------------------------------------------------------------
# The edit file: one sentence per line, cites under a --- line
# ---------------------------------------------------------------------------

HELP = """% One sentence per line, ending in its cite numbers: [1][2]. A blank line ends a paragraph; ## starts a section.
% Under the --- line, one cite per line: [n] <source id> [locator] | "the exact quote". Lines starting with % are ignored.
% A book's or interview's locator comes from the [p. 112] / [12:30] markers in its excerpts."""
SENTENCE = re.compile(r"^(.*?)\s*((?:\[[\d,\s]+\]\s*)*)$")
CITE = re.compile(r'^\[(\d+)\]\s+(\d+)\s*(.*?)\s*\|\s*"(.*)"\s*$')


def render(title: str, sections: list[dict], probs: dict[tuple, list[str]], legend: dict[int, str]) -> str:
    lines = [HELP, *(f"% !! {why}" for why in probs.get((), [])), "", f"# {title}", ""]
    numbers: dict[tuple, int] = {}   # (source, quote, locator) → its [n]
    for i, s in enumerate(sections):
        if s.get("heading"):
            lines += [f"## {s['heading']}", ""]
        for j, p in enumerate(s["paragraphs"]):
            for k, e in enumerate(p):
                lines += [f"% !! {why}" for why in probs.get((i, j, k), [])]
                marks = "".join(f"[{numbers.setdefault((c['source'], c['quote'], c.get('locator') or ''), len(numbers) + 1)}]"
                                for c in e["cites"])
                lines.append(f"{e['text']} {marks}".rstrip())
            lines.append("")
    lines.append("---")
    lines += [f'[{n}] {sid}{" " + loc if loc else ""} | "{quote}"' for (sid, quote, loc), n in numbers.items()]
    lines += ["", "% Sources (not read back):", *(f"% {sid} = {name}" for sid, name in sorted(legend.items()))]
    return "\n".join(lines) + "\n"


def parse(text: str) -> tuple[str, list[dict]]:
    """The edit file → (title, sections). Raises ValueError, naming the line it can't read."""
    body, sep, tail = text.partition("\n---\n")
    if not sep:
        raise ValueError("there's no --- line above the cites")
    cites = {}
    for line in tail.splitlines():
        if not line.strip() or line.startswith("%"):
            continue
        m = CITE.match(line)
        if not m:
            raise ValueError(f"can't read this cite: {line}")
        cites[int(m[1])] = {"source": int(m[2]), "quote": m[4], "locator": m[3] or None}
    title, sections, para = None, [], None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("%"):
            continue
        if line.startswith("# ") and title is None:
            title = line[2:].strip()
        elif line.startswith("## "):
            sections.append({"heading": line[3:].strip(), "paragraphs": []})
            para = None
        elif not line:
            para = None
        else:
            if not sections:
                sections.append({"heading": None, "paragraphs": []})
            if para is None:
                para = []
                sections[-1]["paragraphs"].append(para)
            m = SENTENCE.match(line)
            nums = [int(n) for n in re.findall(r"\d+", m[2])]
            if missing := [n for n in nums if n not in cites]:
                raise ValueError(f"[{missing[0]}] has no cite under the --- line: {line}")
            para.append({"text": m[1], "cites": [dict(cites[n]) for n in nums]})
    if not title:
        raise ValueError("there's no # title line")
    return title, [s for s in sections if s["paragraphs"]]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def load_brief(path: str) -> dict:
    p = Path(path)
    brief = tomllib.loads(p.read_text())
    for key in ("slug", "title", "angle"):
        if not str(brief.get(key, "")).strip():
            sys.exit(f"{path}: the brief needs `{key}`")
    for s in brief.get("source", []):
        if s.get("excerpts"):
            s["excerpts"] = str(p.parent / s["excerpts"])
    return brief


def _date(d) -> str | None:
    return str(d) if d else None


def _web_source(s: dict) -> int:
    known = db().table("source").select("id").eq("url", s["url"]).execute().data
    if known and not (s.get("authors") or s.get("publisher") or s.get("title")):
        return known[0]["id"]   # already in the knowledgebase, with its text
    try:
        page = fetch_page(s["url"])
    except Skipped as e:
        sys.exit(f"{s['url']}: can't use it: {e}")
    if len(page["text"]) > PAGE_CHARS:
        print(f"{s['url']}: {len(page['text'])} characters cut to {PAGE_CHARS}", file=sys.stderr)
        page["text"] = page["text"][:PAGE_CHARS]
    meta = SimpleNamespace(title=s.get("title"), authors=s.get("authors", []), publisher=s.get("publisher", ""),
                           published_on=_date(s.get("published_on")))
    sid = save_source(page, meta)
    if sid is None:
        sys.exit(f"{s['url']}: a creator of this page opted out")
    return sid


def _hand_source(s: dict) -> int:
    """A book, interview or other work without a URL, entered from the brief; the brief is its record."""
    if s.get("work_type") not in ("book", "interview", "article", "video", "archive") or not s.get("title"):
        sys.exit(f"a source needs an id, a url, or a work_type (book, interview…) and a title: {s}")
    row = {"title": s["title"], "work_type": s["work_type"], "publisher": s.get("publisher"),
           "published_on": _date(s.get("published_on")), "isbn": s.get("isbn"), "citation_text": s.get("citation")}
    same = (db().table("source").select("id").is_("url", "null").eq("title", s["title"])
            .eq("work_type", s["work_type"]).execute().data)
    sid = (db().table("source").update(row).eq("id", same[0]["id"]).execute().data[0]["id"] if same
           else db().table("source").insert(row).execute().data[0]["id"])
    for name in s.get("authors", []):
        creator = db().rpc("creator_id", {"p_name": name}).execute().data
        db().table("source_creator").upsert({"source_id": sid, "creator_id": creator}).execute()
    if s.get("excerpts"):
        db().table("source_text").upsert({"source_id": sid, "text": Path(s["excerpts"]).read_text()}).execute()
    return sid


def store_sources(brief: dict) -> list[int]:
    """The brief's sources, in the knowledgebase. Exits on one that can't be credited."""
    ids = []
    for s in brief.get("source", []):
        sid = s["id"] if "id" in s else _web_source(s) if "url" in s else _hand_source(s)
        if not db().rpc("credited", {"p_source": sid}).execute().data:
            sys.exit(f"source {sid} ({s.get('title') or s.get('url') or 'id'}) can't be credited: it needs "
                     "authors or a publisher, and none of its creators may have opted out")
        ids.append(sid)
    return list(dict.fromkeys(ids))


def load_texts(ids) -> dict[int, str | None]:
    """Every existing source among `ids` → the text we hold of it (None when there's none)."""
    ids = sorted(set(ids))
    known = [r["id"] for r in db().table("source").select("id").in_("id", ids).execute().data] if ids else []
    rows = db().table("source_text").select("source_id, text").in_("source_id", known).execute().data if known else []
    return {i: None for i in known} | {r["source_id"]: r["text"] for r in rows}


def legend(ids, found=()) -> dict[int, str]:
    """Source id → its credit line, * for a page the drafter found."""
    rows = (db().table("source").select("id, title, url, publisher, published_on, source_creator(creator(name))")
            .in_("id", sorted(set(ids))).execute().data) if ids else []
    return {r["id"]: credit(r) + (f" · {r['url']}" if r["url"] else "") + (" *found by the drafter" if r["id"] in found else "")
            for r in rows}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def reviewer() -> str:
    return os.getenv("COCINA_REVIEWER") or getpass.getuser()


def _waiting(slug: str) -> dict | None:
    rows = db().table("article").select("*").eq("slug", slug).eq("status", "proposed").order("id", desc=True).execute().data
    return rows[0] if rows else None


def _live(slug: str) -> dict | None:
    rows = db().table("article").select("*").eq("slug", slug).eq("status", "published").execute().data
    return rows[0] if rows else None


def _cited(sections: list[dict]) -> set[int]:
    return {c["source"] for _, e in sentences(sections) for c in e["cites"]}


def _article_texts(a: dict) -> dict[int, str | None]:
    return load_texts([*a["sources"], *a["found"], *_cited(a["sections"])])


def draft(ai: anthropic.Anthropic, brief_path: str) -> dict:
    brief = load_brief(brief_path)
    slug = brief["slug"]
    if _waiting(slug):
        sys.exit(f"{slug}: a draft is already waiting: edit, publish or drop it first")
    picked = store_sources(brief)
    texts = load_texts(picked)
    errors, cost, pages = [], 0.0, []

    if brief.get("search", True):
        known = {r["url"] for r in db().table("source").select("url").in_("id", picked).execute().data if r["url"]}
        urls, c = find_urls(ai, SEARCH_PROMPT.format(title=brief["title"], angle=brief["angle"]))
        cost += c
        for url in urls:
            if len(pages) == MAX_FOUND:
                break
            if url in known:
                continue
            try:
                page = fetch_page(url)
            except Skipped as e:
                errors.append({"url": url, "skipped": str(e)})
                continue
            if page["url"] not in known:   # a redirect can land on a page the owner picked
                page["text"] = page["text"][:PAGE_CHARS]
                pages.append(page)
                known.add(page["url"])

    # The prompt: the owner's sources that have text, then the pages found, numbered
    numbered = [(sid, None, texts[sid]) for sid in picked if texts[sid]] + [(None, p, p["text"]) for p in pages]
    if not numbered:
        sys.exit(f"{slug}: no source with text to draft from")
    if (total := sum(len(t) for *_, t in numbered)) > MAX_CHARS:
        sys.exit(f"{slug}: the sources hold {total} characters; the drafter reads at most {MAX_CHARS} in one prompt")
    names = legend(picked)
    blocks = [f'<source n="{n}">\n'
              + (names[sid] if sid else f"FOUND: a page you found; give its credit. {page['title']} · {page['url']}")
              + f"\n\n{text}\n</source>" for n, (sid, page, text) in enumerate(numbered, 1)]
    prompt = "\n\n".join(blocks) + f"\n\nWrite the article \"{brief['title']}\". {brief['angle']}"

    with ai.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": anthropic.transform_schema(Draft)}},
        system=DRAFT_SYSTEM.format(words=brief.get("words", 1200)),
        messages=[{"role": "user", "content": prompt}],
        **FALLBACK,
    ) as stream:
        resp = stream.get_final_message()
    cost = round(cost + _cost(resp.usage), 4)
    if resp.stop_reason != "end_turn":
        db().table("run_log").insert({"agent": "draft", "cost_usd": cost,
                                      "errors": errors + [{"slug": slug, "stop_reason": resp.stop_reason}]}).execute()
        sys.exit(f"{slug}: the drafter stopped ({resp.stop_reason}); nothing was saved (${cost})")
    out = Draft.model_validate_json(next(b.text for b in resp.content if b.type == "text"))

    # Number → source id; a found page becomes a source only when it's cited
    cited = {c.source for s in out.sections for p in s.paragraphs for e in p for c in e.cites}
    credits = {c.source: c for c in out.credits}
    ids, found = {}, []
    for n, (sid, page, text) in enumerate(numbered, 1):
        if sid:
            ids[n] = sid
        elif n in cited:
            meta = credits.get(n) or SimpleNamespace(title=page["title"], authors=[], publisher="", published_on=None)
            if new := save_source(page, meta):   # None: a creator opted out
                ids[n] = new
                found.append(new)
                texts[new] = text
            else:
                errors.append({"url": page["url"], "skipped": "a creator opted out"})
    sections = [{"heading": s.heading, "paragraphs": [
        [{"text": e.text, "cites": [{"source": ids[c.source], "quote": c.quote, "locator": None}
                                    for c in e.cites if c.source in ids]} for e in p] for p in s.paragraphs]}
        for s in out.sections]
    fill_locators(sections, texts)
    probs = problems(sections, texts, also=[p["text"] for p in pages])

    count = sum(1 for _ in sentences(sections))
    run = db().table("run_log").insert({
        "agent": "draft", "cost_usd": cost, "proposals": 1,
        "errors": errors + [{"slug": slug, "sentence": ".".join(str(i + 1) for i in place), "problem": why}
                            for place, whys in probs.items() for why in whys]}).execute().data[0]
    proposal = db().table("proposal").insert({
        "op": "change" if _live(slug) else "add", "author": "draft", "run_id": run["id"],
        "rationale": f"{count} sentences citing {len(_cited(sections))} sources ({len(found)} found by the drafter); "
                     f"{len(probs)} with problems"}).execute().data[0]
    db().table("article").insert({"slug": slug, "title": brief["title"], "sections": sections, "sources": picked,
                                  "found": found, "proposal_id": proposal["id"]}).execute()
    return {"slug": slug, "sentences": count, "found": len(found), "problems": len(probs), "cost_usd": cost}


def edit(slug: str) -> None:
    a = _waiting(slug) or _new_version(slug)
    path = REPO / "briefs" / f"{slug}.md"
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        print(f"{path} holds an edit that wasn't saved; reopening it")
    else:
        texts = _article_texts(a)
        path.write_text(render(a["title"], a["sections"], problems(a["sections"], texts),
                               legend(texts, a["found"])))
    editor = shlex.split(os.getenv("VISUAL") or os.getenv("EDITOR") or "vi")
    while True:
        subprocess.run([*editor, str(path)], check=False)
        try:
            title, sections = parse(path.read_text())
        except ValueError as e:
            if input(f"{e}\nEdit again, or leave it for next time? [E/l] ").strip().lower() == "l":
                return
            continue
        texts = load_texts([*a["sources"], *a["found"], *_cited(sections)])
        fill_locators(sections, texts)
        probs = problems(sections, texts)
        db().table("article").update({"title": title, "sections": sections}).eq("id", a["id"]).execute()
        a |= {"title": title, "sections": sections}
        path.unlink()
        if not probs:
            print(f"Saved, with no problems: `python -m cocina.article publish {slug}` when it's ready.")
            return
        for place, whys in probs.items():
            print(f"  {'.'.join(str(i + 1) for i in place) or 'article'}: {'; '.join(whys)}")
        if input(f"Saved, with {len(probs)} sentence(s) to fix before it can publish. Edit again? [Y/n] ").strip().lower() == "n":
            return
        path.write_text(render(title, sections, probs, legend(texts, a["found"])))


def _new_version(slug: str) -> dict:
    """A published article, copied into a new proposed version to edit."""
    live = _live(slug)
    if not live:
        sys.exit(f"{slug}: no draft and no published article")
    proposal = db().table("proposal").insert({"op": "change", "author": reviewer(),
                                              "rationale": "a new version, edited by hand"}).execute().data[0]
    keep = {k: live[k] for k in ("slug", "title", "sections", "sources", "found")}
    return db().table("article").insert(keep | {"proposal_id": proposal["id"]}).execute().data[0]


def publish(slug: str) -> None:
    a = _waiting(slug)
    if not a:
        sys.exit(f"{slug}: no draft waiting")
    texts = _article_texts(a)
    if probs := problems(a["sections"], texts):
        for place, whys in probs.items():
            print(f"  {'.'.join(str(i + 1) for i in place) or 'article'}: {'; '.join(whys)}")
        sys.exit(f"{slug}: {len(probs)} sentence(s) to fix first: python -m cocina.article edit {slug}")
    cites = [c for _, e in sentences(a["sections"]) for c in e["cites"]]
    names = legend(_cited(a["sections"]), a["found"])
    print(f"{a['title']}\n  {sum(1 for _ in sentences(a['sections']))} sentences, {len(cites)} cites, "
          f"{len(names)} sources; {sum(c['source'] in a['found'] for c in cites)} cites rest on pages the drafter found")
    for sid, name in sorted(names.items()):
        print(f"  {sid} = {name}")
    if input("Publish it? [y/N] ").strip().lower() != "y":
        return
    db().rpc("accept_proposal", {"p_id": a["proposal_id"], "p_reviewer": reviewer()}).execute()
    print(f"Published {slug}.")


def drop(slug: str, why: str) -> None:
    a = _waiting(slug)
    if not a:
        sys.exit(f"{slug}: no draft waiting")
    db().rpc("reject_proposal", {"p_id": a["proposal_id"], "p_reviewer": reviewer(), "p_reason": why}).execute()
    print(f"Dropped the {slug} draft.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("sources", "draft"):
        sub.add_parser(cmd).add_argument("brief")
    for cmd in ("edit", "publish", "drop"):
        p = sub.add_parser(cmd)
        p.add_argument("slug")
        if cmd == "drop":
            p.add_argument("--why", required=True)
    a = ap.parse_args()
    if a.cmd == "sources":
        for sid, name in legend(store_sources(load_brief(a.brief))).items():
            print(f"{sid} = {name}")
    elif a.cmd == "draft":
        print(draft(anthropic.Anthropic(timeout=900, max_retries=1), a.brief))
    elif a.cmd == "edit":
        edit(a.slug)
    elif a.cmd == "publish":
        publish(a.slug)
    else:
        drop(a.slug, a.why)
