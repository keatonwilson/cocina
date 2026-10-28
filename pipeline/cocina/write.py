"""Write an entity's writeup: a few plain sentences made only from its published claims.

    python -m cocina.write chavindeca
    python -m cocina.write --all        # served entities with no live (or a stale) writeup

Every sentence cites the claims it states. A sentence that cites none, cites a
claim the card doesn't carry, or copies 8 or more consecutive words of a source
quote fails the entity and nothing is written. A writeup lands as a proposal;
`review --auto` checks it reads true to its claims before it publishes.
"""

import argparse
import sys

import anthropic
from pydantic import BaseModel

from cocina.db import client as db
from cocina.research import FALLBACK, MODEL, _cost, _flat

COPY_WORDS = 8   # the copying check: this many words in a row from a quote is too many

WRITE_SYSTEM = """You write the short description of a Mexican food for a knowledgebase in which \
every sentence is cited.

Rules:
- Use only the facts listed. Never add anything from memory: no texture, flavour, history, \
comparison or praise that the facts don't state.
- Write 2 to 4 plain sentences in English. The first stands alone: what the food is, in one line.
- Every sentence lists in `claims` the ids of the facts it states, and states nothing beyond them.
- Where facts disagree (two places of origin, say), say that sources differ rather than choosing one.
- Write in your own words; never copy a fact's wording at length.
- Leave out a fact that doesn't fit a short description rather than forcing it in."""


class Sentence(BaseModel):
    text: str
    claims: list[int]


class Writeup(BaseModel):
    sentences: list[Sentence]


def _ngrams(text: str, n: int = COPY_WORDS) -> set[tuple[str, ...]]:
    words = [w.strip(".,;:!?()\"'") for w in _flat(text).split()]
    words = [w for w in words if w]
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def problems(sentences: list[dict], card: dict) -> list[str]:
    """Why these sentences can't be proposed for this card (empty when they can)."""
    ids = {c["id"] for c in card["claims"]}
    quoted = set().union(*(_ngrams(s["quote"]) for c in card["claims"] for s in c["sources"]))
    out = []
    if not 1 <= len(sentences) <= 6:
        out.append(f"{len(sentences)} sentences (1–6 allowed)")
    for i, s in enumerate(sentences, 1):
        if not s["text"].strip() or not s["claims"]:
            out.append(f"sentence {i} cites nothing")
        if set(s["claims"]) - ids:
            out.append(f"sentence {i} cites claims the card doesn't carry: {sorted(set(s['claims']) - ids)}")
        if _ngrams(s["text"]) & quoted:
            out.append(f"sentence {i} copies {COPY_WORDS}+ words of a source quote")
    return out


def _facts(card: dict) -> str:
    lines = [f"Food: {card['name']} ({card['kind']})"]
    if card["aliases"]:
        lines.append(f"Other names: {', '.join(card['aliases'])}")
    lines += ["", "Facts:"]
    for c in card["claims"]:
        obj = f" ({c['object']['name']})" if c.get("object") else ""
        lines.append(f"[{c['id']}] {c['predicate']}: {c['value']}{obj}")
    return "\n".join(lines)


def write(ai: anthropic.Anthropic, slug: str) -> dict:
    card = db().rpc("get_entity", {"p_slug": slug}).execute().data
    if not card or not card["claims"]:
        return {"slug": slug, "skipped": "not served: no published, credited claims"}
    entity_id = db().table("entity").select("id").eq("slug", slug).single().execute().data["id"]
    if db().table("writeup").select("id").eq("entity_id", entity_id).eq("status", "proposed").execute().data:
        return {"slug": slug, "skipped": "a writeup is already waiting for review"}

    with ai.beta.messages.stream(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "medium"},
        system=WRITE_SYSTEM,
        messages=[{"role": "user", "content": _facts(card)}],
        output_format=Writeup,
        **FALLBACK,
    ) as stream:
        resp = stream.get_final_message()
    cost = round(_cost(resp.usage), 4)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        errors = [f"stop_reason={resp.stop_reason}"]
    else:
        sentences = [s.model_dump() for s in resp.parsed_output.sentences]
        errors = problems(sentences, card)
    if errors:
        db().table("run_log").insert({"agent": "write", "cost_usd": cost,
                                      "errors": [{"slug": slug, "error": e} for e in errors]}).execute()
        return {"slug": slug, "failed": errors, "cost_usd": cost}

    run = db().table("run_log").insert({"agent": "write", "cost_usd": cost, "proposals": 1}).execute().data[0]
    live = db().table("writeup").select("id").eq("entity_id", entity_id).eq("status", "published").execute().data
    proposal = db().table("proposal").insert({
        "op": "change" if live else "add", "author": "write", "run_id": run["id"],
        "rationale": f"{len(sentences)} sentences from {len({i for s in sentences for i in s['claims']})} claims",
    }).execute().data[0]
    db().table("writeup").insert({"entity_id": entity_id, "sentences": sentences,
                                  "proposal_id": proposal["id"]}).execute()
    return {"slug": slug, "sentences": len(sentences), "cost_usd": cost}


def needs_writeup() -> list[str]:
    """Served entities with no live writeup: never written, or stale (a cited claim lost its credit)."""
    return [c["slug"] for c in db().rpc("cards").execute().data if not c.get("writeup")]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--all", action="store_true", help="every served entity without a live writeup")
    a = ap.parse_args()
    ai = anthropic.Anthropic(timeout=600, max_retries=1)
    for s in a.slugs or (needs_writeup() if a.all else ap.error("name slugs or --all")):
        try:
            print(write(ai, s))
        except Exception as e:  # keep going through a list
            print(f"{s}: failed: {e!r}", file=sys.stderr)
