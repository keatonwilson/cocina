"""Review open proposals in the terminal: the one gate to published data.

    python -m cocina.review [--entity chavindeca] [--kind dish]

a accept · r reject (asks why) · s skip · o open the sources · q quit
"""

import argparse
import getpass
import os
import webbrowser

from cocina.db import client as db

SELECT = (
    "id, author, rationale, created_at, "
    "claim(id, predicate, value, status, entity:entity!claim_entity_id_fkey(slug, name, kind), "
    "object:entity!claim_object_id_fkey(name)), "
    "evidence(quote, locator, claim:claim(predicate, value, entity:entity!claim_entity_id_fkey(slug, name, kind)), "
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
    for i, ev in enumerate(p["evidence"], 1):
        print(f"  {i}. \"{ev['quote']}\"" + (f" ({ev['locator']})" if ev.get("locator") else ""))
        print(f"     — {credit(ev['source'])}")
        print(f"       {ev['source'].get('url') or ''}")
    return [ev["source"]["url"] for ev in p["evidence"] if ev["source"].get("url")]


def review(entity: str | None, kind: str | None) -> None:
    who = os.getenv("COCINA_REVIEWER") or getpass.getuser()
    rows = db().table("proposal").select(SELECT).eq("status", "open").order("id").execute().data

    def about(p):
        c = (p["claim"] or [None])[0] or (p["evidence"][0]["claim"] if p["evidence"] else None)
        return c["entity"] if c else {}

    rows = [p for p in rows if (not entity or about(p).get("slug") == entity)
            and (not kind or about(p).get("kind") == kind)]
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
    a = ap.parse_args()
    review(a.entity, a.kind)
