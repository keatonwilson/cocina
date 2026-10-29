"""Take published claims down, on your decision: the one way a published fact leaves.

    python -m cocina.retire 1475 1471 --why "kinds of taco, not other names for it"
    python -m cocina.retire --entity gringas --into gringa --why "duplicate of gringa"

Shows each claim with its quotes and asks before changing anything. --entity takes
all of an entity's published claims, which retires the entity too (with its names
and writeup); --into then gives its name to the survivor as a search hint, so a
lookup by that name (cartoTaco's spec names) still lands. The retire is a proposal
accepted in your name, so it moves kb_version and stays in the history; nothing is
deleted. A writeup citing a retired claim stops being served until `write --all`.
"""

import argparse
import getpass
import os
import sys

from cocina.db import client as db, norm
from cocina.review import credit

CLAIMS = ("id, predicate, value, status, entity_id, "
          "evidence(quote, status, source(title, url, publisher, published_on, source_creator(creator(name))))")


def _entity(slug: str) -> dict:
    rows = db().table("entity").select("id, slug, name, status").eq("slug", slug).execute().data
    if not rows:
        sys.exit(f"no entity {slug!r}")
    return rows[0]


def retire(ids: list[int], entity: str | None, into: str | None, why: str, who: str) -> None:
    twin = _entity(entity) if entity else None
    q = db().table("claim").select(CLAIMS)
    claims = (q.eq("entity_id", twin["id"]).eq("status", "published") if twin else q.in_("id", ids)).order("id").execute().data
    live = [c for c in claims if c["status"] == "published"]
    for c in claims:
        if c["status"] != "published":
            print(f"  skipping [{c['id']}]: it is {c['status']}, not published")
    if missing := set(ids) - {c["id"] for c in claims}:
        print(f"  no claims {sorted(missing)}")
    if not live:
        sys.exit("nothing to retire")

    names = {e["id"]: e["name"] for e in db().table("entity").select("id, name")
             .in_("id", list({c["entity_id"] for c in live})).execute().data}
    for c in live:
        print(f"\n[{c['id']}] {names[c['entity_id']]} · {c['predicate']} = \"{c['value']}\"")
        for ev in c["evidence"]:
            if ev["status"] == "published":
                print(f"   \"{ev['quote']}\"\n     — {credit(ev['source'])}")

    survivor = _entity(into) if into else None
    hint = survivor and twin and norm(twin["name"]) not in (
        {norm(survivor["name"])} | {norm(a["text"]) for a in db().table("alias").select("text")
                                     .eq("entity_id", survivor["id"]).in_("status", ["proposed", "published"])
                                     .execute().data})
    what = f"{len(live)} claim(s)" + (f", retiring {twin['slug']}" if twin else "") + \
           (f", and give {twin['name']!r} to {survivor['slug']}" if hint else "")
    if input(f"\nRetire {what}? Reason: {why}\n[y/N] > ").strip().lower() != "y":
        sys.exit("nothing changed")

    p = db().table("proposal").insert({"op": "retire", "author": who, "rationale": why,
                                       "retires": [c["id"] for c in live]}).execute().data[0]
    if hint:
        db().table("alias").insert({"entity_id": survivor["id"], "text": twin["name"], "proposal_id": p["id"]}).execute()
    try:
        db().rpc("accept_proposal", {"p_id": p["id"], "p_reviewer": who}).execute()
    except Exception as e:  # a rule refused it: leave nothing open
        db().rpc("reject_proposal", {"p_id": p["id"], "p_reviewer": who,
                                     "p_reason": f"refused: {getattr(e, 'message', e)}"}).execute()
        sys.exit(f"refused: {getattr(e, 'message', e)}")
    print(f"retired: proposal #{p['id']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("claims", nargs="*", type=int, help="claim ids")
    ap.add_argument("--entity", help="retire all of this entity's published claims (and so the entity)")
    ap.add_argument("--into", help="with --entity: the survivor that takes its name as a search hint")
    ap.add_argument("--why", required=True, help="the reason, kept on the proposal")
    a = ap.parse_args()
    if bool(a.claims) == bool(a.entity) or (a.into and not a.entity):
        ap.error("name claim ids or --entity (and --into only with --entity)")
    retire(a.claims, a.entity, a.into, a.why, os.getenv("COCINA_REVIEWER") or getpass.getuser())
