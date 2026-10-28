"""Other names from Wikidata: its CC0 English and Spanish labels and aliases, proposed
as also_known_as claims.

    python -m cocina.wikidata birria
    python -m cocina.wikidata --all      # every proposed or published entity

The one exception to the robots rule: wikidata.org's robots.txt disallows crawlers
on /w/, where its documented Action API lives. The owner decided (2026-10-28) to use
that API as Wikimedia intends programs to: our descriptive user agent, one request
at a time, `maxlag` respected. Only API below is exempt; every other fetch still
asks fetch.allowed().

A food matches only when exactly one Wikidata item has the entity's name as an
English or Spanish label or alias and is a kind of food (an instance or subclass of
something under Q2095). That item's labels become a page whose lines the claims
quote, so the quote check, credit (publisher Wikidata, CC0) and review --auto apply
as they do to any source.
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

from cocina.db import client as db, norm
from cocina.fetch import _get
from cocina.research import propose, quote_found

API = "https://www.wikidata.org/w/api.php"   # the robots exception (see the docstring)
FOOD = "Q2095"
LANGS = ("en", "es")
CLASS_DEPTH = 8          # subclass hops to look for food before giving up
BUSY = {"maxlag", "cirrussearch-too-busy-error", "ratelimited"}   # transient: wait and retry


def _wait(retry_after: str | None) -> int:
    """Seconds to wait for a Retry-After: a number of seconds (an HTTP date or junk → 5), at most a minute."""
    return min(int(retry_after), 60) if retry_after and retry_after.strip().isdecimal() else 5


def _api(**params) -> dict:
    url = API + "?" + urlencode({**params, "format": "json", "maxlag": 5})
    for _ in range(4):
        status, headers, body, _final = _get(url)
        data = json.loads(body or b"{}")
        # Busy, at the edge (429/503, empty body) or in the API: wait as asked
        if status in (429, 503) or data.get("error", {}).get("code") in BUSY:
            time.sleep(_wait(headers.get("Retry-After")))
            continue
        if status >= 400 or "error" in data:
            raise RuntimeError(f"Wikidata API {status}: {data.get('error')}")
        time.sleep(0.5)   # one request at a time, gently
        return data
    raise RuntimeError("Wikidata kept reporting lag")


def _entities(ids, props: str) -> dict:
    ids, out = list(ids), {}
    for i in range(0, len(ids), 50):            # the API's limit per request
        out |= _api(action="wbgetentities", ids="|".join(ids[i:i + 50]), props=props,
                    languages="|".join(LANGS))["entities"]
    return out


def _classes(item: dict, props=("P31", "P279")) -> set[str]:
    return {c["mainsnak"]["datavalue"]["value"]["id"]
            for p in props for c in item.get("claims", {}).get(p, [])
            if c.get("mainsnak", {}).get("datavalue")}


_parents: dict[str, set[str]] = {}    # class → its superclasses, shared across entities


def is_food(item: dict) -> bool:
    """Whether an item is an instance or subclass of something under food (Q2095)."""
    frontier, seen = _classes(item), set()
    for _ in range(CLASS_DEPTH):
        if FOOD in frontier:
            return True
        frontier -= seen
        if not frontier:
            return False
        seen |= frontier
        missing = [q for q in frontier if q not in _parents]
        if missing:
            for qid, e in _entities(missing, "claims").items():
                _parents[qid] = _classes(e, ("P279",))
        frontier = set().union(*(_parents.get(q, set()) for q in frontier))
    return FOOD in frontier


def names_of(item: dict) -> list[tuple[str, str]]:
    """(line, name) for each English and Spanish label and alias; the line is what a claim quotes."""
    out = [(f"Label ({lang}): {item['labels'][lang]['value']}", item["labels"][lang]["value"])
           for lang in LANGS if lang in item.get("labels", {})]
    out += [(f"Also known as ({lang}): {a['value']}", a["value"])
            for lang in LANGS for a in item.get("aliases", {}).get(lang, [])]
    return out


def pick(name: str, items: dict, food=is_food) -> str | None:
    """The one food item whose label or alias is exactly this name; None for none or several."""
    hits = [qid for qid, item in items.items()
            if norm(name) in {norm(n) for _, n in names_of(item)} and food(item)]
    return hits[0] if len(hits) == 1 else None


def page_and_claims(qid: str, item: dict, known: set[str]) -> tuple[dict, list[dict]]:
    """The item as a page of name lines, and an also_known_as claim for each name not yet known."""
    names = names_of(item)
    text = "\n".join(line for line, _ in names)
    page = {"url": f"https://www.wikidata.org/wiki/{qid}",
            "title": next((n for line, n in names if line.startswith("Label")), qid),
            "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat()}
    claims, seen = [], set(known)
    for line, name in names:
        if norm(name) in seen or not 2 <= len(name) <= 80 or not quote_found(line, text):
            continue
        seen.add(norm(name))
        claims.append({"page": 1, "predicate": "also_known_as", "value": name, "object_name": None, "quote": line})
    return page, claims


def wikidata(slug: str) -> dict:
    entity = db().table("entity").select("*").eq("slug", slug).single().execute().data
    found = {}
    for lang in LANGS:
        for hit in _api(action="wbsearchentities", search=entity["name"], language=lang, uselang=lang,
                        type="item", limit=10).get("search", []):
            found[hit["id"]] = hit
    if not found:
        return {"slug": slug, "skipped": "no Wikidata item by that name"}
    items = _entities(found, "labels|aliases|claims")
    qid = pick(entity["name"], items)
    if not qid:
        return {"slug": slug, "skipped": "not exactly one food item by that name"}

    known = {norm(entity["name"])} | {norm(a["text"]) for a in db().table("alias").select("text")
                                       .eq("entity_id", entity["id"]).in_("status", ["proposed", "published"])
                                       .execute().data}
    page, claims = page_and_claims(qid, items[qid], known)
    if not claims:
        return {"slug": slug, "qid": qid, "proposals": 0}

    links = {p["name"]: p["links"] for p in
             db().table("predicate").select("name, links").eq("kind", entity["kind"]).execute().data}
    if "also_known_as" not in links:
        return {"slug": slug, "qid": qid, "skipped": f"{entity['kind']} has no also_known_as"}
    meta = {1: SimpleNamespace(title=page["title"], authors=[], publisher="Wikidata", published_on=None,
                               license="CC0-1.0", work_type="dataset")}
    run = db().table("run_log").insert({"agent": "wikidata"}).execute().data[0]
    errors, made = [], 0
    try:
        made = propose(entity, [page], meta, claims, run, links, errors)
    except Exception as e:  # as research does: the run_log says what broke
        errors.append({"error": repr(e)})
        raise
    finally:
        db().table("run_log").update({"finished_at": datetime.now(timezone.utc).isoformat(),
                                      "proposals": made, "errors": errors}).eq("id", run["id"]).execute()
    return {"slug": slug, "qid": qid, "proposals": made}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--all", action="store_true", help="every proposed or published entity")
    a = ap.parse_args()
    slugs = a.slugs or ([e["slug"] for e in db().table("entity").select("slug")
                         .in_("status", ["proposed", "published"]).order("slug").execute().data]
                        if a.all else ap.error("name slugs or --all"))
    for s in slugs:
        try:
            print(wikidata(s))
        except Exception as e:  # keep going through a list
            print(f"{s}: failed: {e!r}", file=sys.stderr)
