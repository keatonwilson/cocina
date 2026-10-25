"""Research one entity: search, fetch, extract sourced claims, write proposals.

    python -m cocina.research chavindeca

Nothing here publishes. Every claim lands as a proposal with the exact quote
that supports it; claims whose quote isn't in the page we fetched are dropped.
"""

import argparse
import sys
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit

import anthropic
from pydantic import BaseModel

from cocina.db import client as db, norm
from cocina.fetch import Skipped, fetch_page

MODEL = "claude-sonnet-5-5"
PRICE_IN, PRICE_OUT = 2.00 / 1e6, 10.00 / 1e6     # $ per token, Sonnet 5.5
PRICE_SEARCH = 10.00 / 1000                       # $ per web search
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
MAX_PAGES = 6
PAGE_CHARS = 30_000
SKIP_HOSTS = ("pinterest.", "youtube.", "facebook.", "instagram.", "tiktok.", "amazon.", "reddit.", "yelp.",
              "x.com", "twitter.", "lemon8", "threads.")

SEARCH_PROMPT = """Search the web for reliable information about the Mexican food "{name}" ({kind}){aka}.
Run two or three searches, in English and in Spanish. Prefer historians, cookbook authors, food
writers, encyclopedias and Mexican institutions over recipe aggregators, social media and
restaurant pages. When you have searched, reply with the single word: done."""

EXTRACT_SYSTEM = """You extract checkable facts about Mexican food from web pages, for a knowledgebase \
that credits every source.

Rules:
- Use only the pages provided. Never add facts from memory.
- Every claim carries `quote`: an excerpt copied character for character from that page, one \
sentence or less and under 300 characters, that states the fact. Never paraphrase or translate a quote.
- One fact per claim. List ingredients, toppings and accompaniments as separate claims.
- `value` is the shortest canonical form in plain English: originates_in "Michoacán", not \
"Michoacán, Mexico"; uses "cheese", not "melted cheese". Keep a detail only when it is what makes \
the food distinct ("two corn tortillas"). Keep Spanish food names as they are.
- When several pages state the same fact, use the identical `value` for each, so the claims merge.
- `object_name`: for predicates that point at another food, place or technique (uses, variant_of, \
served_with, originates_in, dried_form_of, used_for, part_of), that thing's plain name. Otherwise null.
- Skip recipe quantities and step-by-step methods, prices, and anything about one particular restaurant.
- For each page give its title, the people credited as authors (an empty list if none), the publisher \
(site or organisation name) and the publication date if the page states one (YYYY-MM-DD)."""


def _flat(s: str) -> str:
    """Comparison form for quotes: unicode-normalised, straight quotes, collapsed whitespace."""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-"}))
    return " ".join(s.split()).casefold()


def quote_found(quote: str, text: str) -> bool:
    """The anti-hallucination check: the quote must really be on the page."""
    q = _flat(quote)
    return 12 <= len(q) and len(quote) <= 400 and q in _flat(text)


def group_claims(claims: list[dict]) -> list[dict]:
    """Merge claims that state the same fact: one claim, one evidence row per page."""
    groups: dict[tuple[str, str], dict] = {}
    for c in claims:
        g = groups.setdefault((c["predicate"], norm(c["value"])), {**c, "evidence": []})
        if c["page"] not in {e["page"] for e in g["evidence"]}:
            g["evidence"].append({"page": c["page"], "quote": c["quote"]})
        g["object_name"] = g.get("object_name") or c.get("object_name")
    return list(groups.values())


def _cost(usage) -> float:
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
    tokens_in = usage.input_tokens + (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0)
    return tokens_in * PRICE_IN + usage.output_tokens * PRICE_OUT + searches * PRICE_SEARCH


def find_urls(ai: anthropic.Anthropic, entity: dict, aliases: list[str]) -> tuple[list[str], float]:
    """Let Claude choose the searches; take the URLs from the search results themselves."""
    aka = f", also called {', '.join(aliases)}" if aliases else ""
    resp = ai.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        # ponytail: basic web_search, not _20260209; cartoTacoMenuExtract saw the newer
        # variant's filtering sandbox fail (src/description_gen.py:_web_search_tools)
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": SEARCH_PROMPT.format(name=entity["name"], kind=entity["kind"], aka=aka)}],
        **FALLBACK,
    )
    urls: list[str] = []
    for block in resp.content:
        if block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                host = urlsplit(r.url).netloc.lower()
                if r.url not in urls and not any(h in host for h in SKIP_HOSTS):
                    urls.append(r.url)
    return urls, _cost(resp.usage)


def extract(ai: anthropic.Anthropic, entity: dict, aliases: list[str], predicates: list[str],
            pages: list[dict]) -> tuple[object, float]:
    Pred = Literal[tuple(predicates)]  # noqa: N806 - the DB's predicate list becomes an enum

    class Page(BaseModel):
        index: int
        title: str
        authors: list[str]
        publisher: str
        published_on: str | None

    class Claim(BaseModel):
        page: int
        predicate: Pred
        value: str
        object_name: str | None
        quote: str

    class Extraction(BaseModel):
        pages: list[Page]
        claims: list[Claim]

    docs = "\n\n".join(
        f'<page index="{i}" url="{p["url"]}">\n{p["text"]}\n</page>' for i, p in enumerate(pages, 1)
    )
    hint = (f"\n\nAn earlier, unsourced description (it may contain errors; use it only to know what "
            f"to look for, never as evidence):\n{entity['note']}") if entity.get("note") else ""
    # Streamed: long pages in, a long JSON out. Medium effort: extraction needs care, not deep reasoning
    with ai.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        output_config={"effort": "medium"},
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": (
            f"Entity: {entity['name']} ({entity['kind']})"
            + (f"\nOther names: {', '.join(aliases)}" if aliases else "")
            + f"\nAllowed predicates: {', '.join(predicates)}{hint}\n\n{docs}"
        )}],
        output_format=Extraction,
        **FALLBACK,
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"extraction failed (stop_reason={resp.stop_reason})")
    return resp.parsed_output, _cost(resp.usage)


def save_source(page: dict, meta) -> int:
    published_on = meta.published_on if meta.published_on and len(meta.published_on) == 10 else None
    row = db().table("source").upsert({
        "url": page["url"],
        "title": meta.title or page["title"] or page["url"],
        "publisher": meta.publisher or urlsplit(page["url"]).netloc.removeprefix("www."),
        "published_on": published_on,
        "content_hash": page["content_hash"],
        "fetched_at": page["fetched_at"],
    }, on_conflict="url").execute().data[0]
    db().table("source_text").upsert({"source_id": row["id"], "text": page["text"]}).execute()
    for name in meta.authors:
        creator = db().rpc("creator_id", {"p_name": name}).execute().data
        db().table("source_creator").upsert({"source_id": row["id"], "creator_id": creator}).execute()
    return row["id"]


def link(name: str | None) -> int | None:
    """Object entity id, only on exactly one close published match (as resolve_spec_id does)."""
    if not name:
        return None
    hits = [h for h in db().rpc("search_entities", {"q": name}).execute().data if h["distance"] <= 2]
    if len(hits) != 1:
        return None
    return db().table("entity").select("id").eq("slug", hits[0]["slug"]).single().execute().data["id"]


def research(slug: str) -> dict:
    ai = anthropic.Anthropic(timeout=600, max_retries=1)
    entity = db().table("entity").select("*").eq("slug", slug).single().execute().data
    aliases = [a["text"] for a in db().table("alias").select("text").eq("entity_id", entity["id"])
               .in_("status", ["proposed", "published"]).execute().data
               if norm(a["text"]) != norm(entity["name"])]
    predicates = [p["name"] for p in db().table("predicate").select("name").eq("kind", entity["kind"]).execute().data]
    run = db().table("run_log").insert({"agent": "research"}).execute().data[0]
    errors, cost, proposals = [], 0.0, 0

    try:
        urls, c = find_urls(ai, entity, aliases)
        cost += c
        pages = []
        for url in urls:
            if len(pages) == MAX_PAGES:
                break
            try:
                page = fetch_page(url)
            except Skipped as e:
                errors.append({"url": url, "skipped": str(e)})
                continue
            if len(page["text"]) > PAGE_CHARS:
                errors.append({"url": url, "capped": f"{len(page['text'])} chars cut to {PAGE_CHARS}"})
                page["text"] = page["text"][:PAGE_CHARS]
            pages.append(page)
        if not pages:
            raise RuntimeError("no usable pages")

        out, c = extract(ai, entity, aliases, predicates, pages)
        cost += c
        meta = {m.index: m for m in out.pages}

        kept = []
        for cl in out.claims:
            page = pages[cl.page - 1] if 1 <= cl.page <= len(pages) else None
            if page and quote_found(cl.quote, page["text"]):
                kept.append(cl.model_dump())
            else:
                errors.append({"dropped": cl.model_dump(), "why": "quote not found on the page"})

        source_ids: dict[int, int] = {}
        live = db().table("claim").select("id, predicate, value, status, proposal_id") \
            .eq("entity_id", entity["id"]).in_("status", ["proposed", "published"]).execute().data
        existing = {(c["predicate"], norm(c["value"])): c for c in live}
        # Every alias, rejected ones too: the unique key on (entity, norm(text)) covers them all
        names = {norm(entity["name"])} | {norm(a["text"]) for a in db().table("alias").select("text")
                                          .eq("entity_id", entity["id"]).execute().data}

        for g in group_claims(kept):
            for e in g["evidence"]:
                if e["page"] not in source_ids:
                    source_ids[e["page"]] = save_source(pages[e["page"] - 1], meta.get(e["page"]) or _bare(pages[e["page"] - 1]))
            hosts = sorted({urlsplit(pages[e["page"] - 1]["url"]).netloc.removeprefix("www.") for e in g["evidence"]})
            old = existing.get((g["predicate"], norm(g["value"])))
            if old and old["status"] == "proposed":
                claim_id, proposal_id = old["id"], old["proposal_id"]        # pending: just add evidence
            else:
                proposal_id = db().table("proposal").insert({
                    "author": "research", "run_id": run["id"],
                    "rationale": (f"{len(hosts)} more source(s) agree" if old else f"{len(hosts)} source(s)")
                                 + ": " + ", ".join(hosts),
                }).execute().data[0]["id"]
                proposals += 1
                if old:
                    claim_id = old["id"]
                else:
                    claim_id = db().table("claim").insert({
                        "entity_id": entity["id"], "predicate": g["predicate"], "value": g["value"],
                        "object_id": link(g["object_name"]), "proposal_id": proposal_id,
                    }).execute().data[0]["id"]
                    if g["predicate"] == "also_known_as" and norm(g["value"]) not in names:
                        names.add(norm(g["value"]))
                        db().table("alias").insert({
                            "entity_id": entity["id"], "text": g["value"], "claim_id": claim_id,
                            "proposal_id": proposal_id,
                        }).execute()
            db().table("evidence").upsert([{
                "claim_id": claim_id, "source_id": source_ids[e["page"]], "quote": e["quote"],
                "fetched_at": pages[e["page"] - 1]["fetched_at"], "proposal_id": proposal_id,
            } for e in g["evidence"]], on_conflict="claim_id,source_id", ignore_duplicates=True).execute()
    except Exception as e:
        errors.append({"error": repr(e)})
        raise
    finally:
        db().table("run_log").update({
            "finished_at": datetime.now(timezone.utc).isoformat(), "cost_usd": round(cost, 4),
            "proposals": proposals, "errors": errors,
        }).eq("id", run["id"]).execute()

    return {"slug": slug, "proposals": proposals, "cost_usd": round(cost, 4),
            "dropped": sum(1 for e in errors if "dropped" in e), "skipped": sum(1 for e in errors if "skipped" in e)}


def _bare(page: dict) -> SimpleNamespace:
    """Metadata for a page the model didn't describe: title from HTML, publisher from host."""
    return SimpleNamespace(title=page["title"], authors=[], publisher="", published_on=None)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="+")
    for s in ap.parse_args().slugs:
        try:
            print(research(s))
        except Exception as e:  # keep going through a list; the run_log has the details
            print(f"{s}: failed: {e!r}", file=sys.stderr)
