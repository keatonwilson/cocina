"""Review open proposals: auto-review publishes the uncontested ones, a human the rest.

    python -m cocina.review --auto [--entity chavindeca]     # after research
    python -m cocina.review [--entity chavindeca] [--kind dish]

--auto: Claude checks each entity's open proposals against its published facts and
each other. A proposal whose quotes don't state it, or that can't be true alongside
another fact, is flagged for a human; the rest go through accept_proposal, so the
citation rules still decide. Prose and articles (phase 2+) stay human-only.

In the terminal: a accept · r reject (asks why) · s skip · o open the sources · q quit
"""

import argparse
import getpass
import os
import webbrowser
from typing import Literal

import anthropic
from pydantic import BaseModel

from cocina.db import client as db, norm
from cocina.research import FALLBACK, MODEL, _cost

SELECT = (
    "id, author, rationale, flag, created_at, "
    "claim(id, predicate, value, status, entity:entity!claim_entity_id_fkey(id, slug, name, kind), "
    "object:entity!claim_object_id_fkey(name)), "
    "evidence(quote, locator, claim:claim(id, predicate, value, entity:entity!claim_entity_id_fkey(id, slug, name, kind)), "
    "source(title, url, publisher, published_on, source_creator(creator(name))))"
)


def credit(src: dict) -> str:
    authors = ", ".join(sc["creator"]["name"] for sc in src.get("source_creator") or [])
    bits = [authors, src["title"], src.get("publisher"), (src.get("published_on") or "")[:4]]
    return " · ".join(b for b in bits if b) or "(no credit)"


def show(p: dict, n: int, total: int) -> list[str]:
    claim = (p["claim"] or [None])[0]
    target = claim or (p["evidence"][0]["claim"] if p["evidence"] else None)
    if not target:
        print(f"\n[{n}/{total}] proposal {p['id']} has no claim or evidence")
        return []
    e = target["entity"]
    link = f"  → {claim['object']['name']}" if claim and claim.get("object") else ""
    kind = "new claim" if claim else "more evidence for a published claim"
    print(f"\n[{n}/{total}] {e['name']} ({e['kind']}) · {target['predicate']} = \"{target['value']}\"{link}")
    print(f"  {kind} · {p['author']} · {p['rationale'] or ''}")
    if p.get("flag"):
        print(f"  ⚠ {p['flag']}")
    for i, ev in enumerate(p["evidence"], 1):
        print(f"  {i}. \"{ev['quote']}\"" + (f" ({ev['locator']})" if ev.get("locator") else ""))
        print(f"     — {credit(ev['source'])}")
        print(f"       {ev['source'].get('url') or ''}")
    return [ev["source"]["url"] for ev in p["evidence"] if ev["source"].get("url")]


def _target(p: dict) -> dict | None:
    """The claim a proposal is about: its new claim, or the published one it adds evidence to."""
    return (p["claim"] or [None])[0] or (p["evidence"][0]["claim"] if p["evidence"] else None)


def about(p: dict) -> dict:
    t = _target(p)
    return t["entity"] if t else {}


def open_proposals(entity: str | None = None, kind: str | None = None) -> list[dict]:
    rows = db().table("proposal").select(SELECT).eq("status", "open").order("id").execute().data
    return [p for p in rows if (not entity or about(p).get("slug") == entity)
            and (not kind or about(p).get("kind") == kind)]


AUTO_SYSTEM = """You check proposed facts about a Mexican food before they publish without a human \
reading them. Each proposal (#id) states one fact and quotes the pages that support it; published \
facts (P id) are already in the knowledgebase; a human rejected the facts marked R id, for the \
reason given.

Flag a proposal only when:
- unsupported: none of its quotes states the fact. The quote is real, but says something else, \
only hints at it, or is about a different food.
- conflict: it can't be true alongside another fact listed, published or proposed: "uses corn \
tortilla" against "uses flour tortilla", or two different places of origin. Facts that can all be \
true at once (several ingredients, regional variants, a second name) are not conflicts. Flag every \
proposal on each side of a conflict. A proposal that restates a rejected fact conflicts with it.

Flag nothing else; everything you don't flag is published. `why` is one sentence naming the other \
facts (#id, P id or R id) involved."""


class Flag(BaseModel):
    proposal: int
    kind: Literal["unsupported", "conflict"]
    why: str


class Verdict(BaseModel):
    flags: list[Flag]


def split(proposals: list[dict], flags: list,
          rejected: dict[tuple[str, str], str]) -> tuple[list[int], dict[int, str]]:
    """(proposals to accept, {proposal: flag}). A new claim restating a rejected fact
    (rejected: (predicate, norm(value)) → flag) is flagged whatever the model says; an
    already-flagged proposal waits for a human either way; a flag naming an id we didn't
    send is ignored."""
    asked = {p["id"] for p in proposals if not p.get("flag")}
    new = {f.proposal: f"{f.kind}: {f.why}" for f in flags if f.proposal in asked}
    for p in proposals:
        c = (p.get("claim") or [None])[0]
        if p["id"] in asked and c and (c["predicate"], norm(c["value"])) in rejected:
            new[p["id"]] = rejected[(c["predicate"], norm(c["value"]))]
    return [p["id"] for p in proposals if p["id"] in asked and p["id"] not in new], new


def _facts(ent: dict, decided: list[dict], proposals: list[dict]) -> str:
    lines = [f"Food: {ent['name']} ({ent['kind']})", "", "Published and rejected facts:"]
    lines += [f"P{c['id']} {c['predicate']} = \"{c['value']}\"" if c["status"] == "published" else
              f"R{c['id']} {c['predicate']} = \"{c['value']}\" (rejected: {(c['proposal'] or {}).get('reason')})"
              for c in decided] or ["(none)"]
    lines += ["", "Proposed facts:"]
    for p in proposals:
        t = _target(p)
        more = "" if p["claim"] else f" (another source for P{t['id']})"
        lines.append(f"#{p['id']}{more} {t['predicate']} = \"{t['value']}\"")
        lines += [f"   quote: \"{ev['quote']}\"" for ev in p["evidence"]]
    return "\n".join(lines)


def auto_review(entity: str | None = None) -> None:
    ai = anthropic.Anthropic(timeout=600, max_retries=1)
    by_entity: dict[int, list[dict]] = {}
    for p in open_proposals(entity):
        if _target(p):
            by_entity.setdefault(about(p)["id"], []).append(p)
    for proposals in by_entity.values():
        try:
            _auto_entity(ai, about(proposals[0]), proposals)
        except Exception as e:  # one entity's failure doesn't stop the rest
            # ponytail: a DB error after the model call loses that call's cost from run_log
            print(f"{about(proposals[0])['slug']}: auto-review failed: {e!r}; "
                  "anything accepted before the error stays published, the rest stays open")
            db().table("run_log").insert({"agent": "auto-review", "errors": [{"error": repr(e)}]}).execute()


def _auto_entity(ai: anthropic.Anthropic, ent: dict, proposals: list[dict]) -> None:
    decided = (db().table("claim").select("id, predicate, value, status, proposal(reason)")
               .eq("entity_id", ent["id"]).in_("status", ["published", "rejected"]).execute().data)
    rejected = {(c["predicate"], norm(c["value"])):
                f"conflict: restates R{c['id']}, rejected: {(c['proposal'] or {}).get('reason')}"
                for c in decided if c["status"] == "rejected"}
    with ai.beta.messages.stream(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "medium"},
        system=AUTO_SYSTEM,
        messages=[{"role": "user", "content": _facts(ent, decided, proposals)}],
        output_format=Verdict,
        **FALLBACK,
    ) as stream:
        resp = stream.get_final_message()
    cost = round(_cost(resp.usage), 4)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        print(f"{ent['slug']}: auto-review failed (stop_reason={resp.stop_reason}); nothing accepted")
        db().table("run_log").insert({"agent": "auto-review", "cost_usd": cost,
                                      "errors": [{"error": f"stop_reason={resp.stop_reason}"}]}).execute()
        return
    accept, flags = split(proposals, resp.parsed_output.flags, rejected)
    for pid, why in flags.items():
        db().table("proposal").update({"flag": why}).eq("id", pid).execute()
    refused = []
    for pid in accept:
        try:
            db().rpc("accept_proposal", {"p_id": pid, "p_reviewer": "auto-review"}).execute()
        except Exception as e:  # a citation rule refused it: it stays open for the next run
            refused.append(f"#{pid}: {getattr(e, 'message', e)}")
    db().table("run_log").insert({"agent": "auto-review", "cost_usd": cost,
                                  "proposals": len(accept) - len(refused),
                                  "errors": [{"refused": r} for r in refused]}).execute()
    print(f"{ent['slug']}: {len(accept) - len(refused)} accepted, {len(flags)} flagged for you, "
          f"{len(refused)} refused · ${cost:.3f}")
    for pid, why in flags.items():
        print(f"  #{pid} {why}")
    for r in refused:
        print(f"  refused {r}")


def review(entity: str | None, kind: str | None) -> None:
    who = os.getenv("COCINA_REVIEWER") or getpass.getuser()
    rows = open_proposals(entity, kind)
    done = {"accepted": 0, "rejected": 0, "skipped": 0}
    for n, p in enumerate(rows, 1):
        urls = show(p, n, len(rows))
        while True:
            key = input("  a accept · r reject · s skip · o open · q quit > ").strip().lower()
            if key == "o":
                for u in urls:
                    webbrowser.open(u)
                continue
            if key == "a":
                try:
                    db().rpc("accept_proposal", {"p_id": p["id"], "p_reviewer": who}).execute()
                    done["accepted"] += 1
                except Exception as e:  # a citation rule refused it: say which and move on
                    print(f"  refused: {getattr(e, 'message', e)}")
            elif key == "r":
                reason = input("  why? ").strip() or "no reason given"
                db().rpc("reject_proposal", {"p_id": p["id"], "p_reviewer": who, "p_reason": reason}).execute()
                done["rejected"] += 1
            elif key == "s":
                done["skipped"] += 1
            elif key == "q":
                print(done)
                return
            else:
                continue
            break
    print(done)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entity")
    ap.add_argument("--kind")
    ap.add_argument("--auto", action="store_true", help="publish uncontested proposals, flag the rest")
    a = ap.parse_args()
    auto_review(a.entity) if a.auto else review(a.entity, a.kind)
