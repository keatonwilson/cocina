"""Keep sources honest: re-fetch every page a published quote cites, re-check each quote, take down what's gone.

    python -m cocina.audit [--dry-run]

Weekly (.github/workflows/audit.yml); no model calls. Each credited web page that a
published claim or article quotes is fetched as research fetches it (robots, noai):
- the same text (content_hash), or new text that still holds every quote: verified. Its
  quotes' fetched_at (cards' verified_at) becomes now, and new text replaces the stored copy.
- new text missing a quote, or a page that now says noai: those quotes are gone. The stored
  copy stays, as the record of what they were checked against.
- robots.txt, or a 401/403, refusing us: unchecked, and nothing changes.
- any other failure: counted. The third in a row makes the page dead, and its quotes gone.

A gone quote is retired at once (author and reviewer `audit`) when its claim keeps another
credited quote. A claim left with none gets a retire proposal of its own for a person
(`python -m cocina.review`), unless a person kept it before. Article quotes are only listed:
a person edits the article. The Actions log is public, so stdout carries counts; URLs and
reasons go to the run_log row (agent `audit`). --dry-run fetches and counts, writes nothing.
"""

import argparse
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import cache

from cocina.article import sentences
from cocina.db import all_rows, client as db
from cocina.fetch import Skipped, fetch_page
from cocina.research import PAGE_CHARS, quote_found

# ponytail: counts checks, not weeks; a hand-run audit between weekly ones brings death sooner
DEAD_AFTER = 3   # failed checks in a row
PAUSE = 1.0      # seconds between fetches: ~300 pages is about 10 minutes
WHY = {"dropped": "the quote is no longer on the page", "noai": "the page now opts out of AI use",
       "dead": f"the page failed {DEAD_AFTER} checks running"}


def kind(why: str) -> str:
    """How the auditor reads a Skipped: noai (opted out), blocked (we may not look) or failed."""
    if "opts out of AI use" in why:
        return "noai"
    # ponytail: a 401/403 reads as a bot block, not a dead page; a page moved behind a login never dies
    if why.endswith("disallowed by robots.txt") or why in ("HTTP 401", "HTTP 403"):
        return "blocked"
    return "failed"


def check(src: dict, fetch=fetch_page) -> dict:
    """One page → {outcome, gone: its quotes no longer there, page: new text to store, error}.

    src: {url, content_hash, failures, quotes: [{quote, evidence + claim | article}]}"""
    try:
        page = fetch(src["url"])
    except Skipped as e:
        k = kind(str(e))
        if k == "failed":
            k = "dead" if src["failures"] + 1 >= DEAD_AFTER else "failing"
        return {"outcome": k, "gone": src["quotes"] if k in ("noai", "dead") else [], "page": None, "error": str(e)}
    if page["content_hash"] == src["content_hash"]:
        return {"outcome": "same", "gone": [], "page": None, "error": None}
    gone = [q for q in src["quotes"] if not quote_found(q["quote"], page["text"])]
    return {"outcome": "dropped" if gone else "changed", "gone": gone, "page": None if gone else page, "error": None}


def decide(gone: dict[int, list[dict]], backing: dict[int, int], skip: set[int]) -> tuple[dict, dict]:
    """Split gone quotes by claim → (retired now: the claim keeps another credited quote,
    for a person: it keeps none). skip: claims a person kept, or already waiting for one."""
    auto = {c: qs for c, qs in gone.items() if backing.get(c)}
    person = {c: qs for c, qs in gone.items() if not backing.get(c) and c not in skip}
    return auto, person


@cache
def credited(source_id: int) -> bool:
    return db().rpc("credited", {"p_source": source_id}).execute().data


def collect() -> list[dict]:
    """The credited web pages published quotes cite, each with its quotes."""
    quotes = defaultdict(list)
    for e in all_rows(lambda: db().table("evidence").select("id, claim_id, source_id, quote, claim(status)")
                      .eq("status", "published").order("id")):
        if e["claim"]["status"] == "published":   # a retired claim's quotes stay published, unserved
            quotes[e["source_id"]].append({"evidence": e["id"], "claim": e["claim_id"], "quote": e["quote"]})
    for a in all_rows(lambda: db().table("article").select("slug, sections").eq("status", "published").order("id")):
        for _, s in sentences(a["sections"]):
            for c in s["cites"]:
                quotes[c["source"]].append({"article": a["slug"], "quote": c["quote"]})
    ids = list(quotes)
    srcs = all_rows(lambda: db().table("source").select("id, url, content_hash, failures")
                    .in_("id", ids).eq("work_type", "web page").not_.is_("url", "null").order("url"))
    # An opted-out creator's quotes aren't served, and its pages aren't ours to re-read
    return [{**s, "quotes": quotes[s["id"]]} for s in srcs if credited(s["id"])]


def _why(qs: list[dict]) -> str:
    return "; ".join(f"{q['url']}: {q['why']}" for q in qs)


def audit(dry_run: bool) -> None:
    srcs = collect()
    run = None if dry_run else db().table("run_log").insert({"agent": "audit"}).execute().data[0]
    now = datetime.now(timezone.utc).isoformat()
    pages, quotes_gone = Counter(), Counter()
    gone = defaultdict(list)   # claim → its gone quotes
    log = []                   # run_log errors: pages with a problem, article quotes to fix, refusals
    for i, src in enumerate(srcs):
        if i:
            time.sleep(PAUSE)
        try:
            r = check(src)
        except Exception as e:  # a bug, not the page: count nothing against it
            pages["error"] += 1
            log.append({"url": src["url"], "outcome": "error", "error": repr(e)})
            continue
        pages[r["outcome"]] += 1
        if r["outcome"] not in ("same", "changed"):
            log.append({"url": src["url"], "outcome": r["outcome"], "error": r["error"]})
        for q in r["gone"]:
            quotes_gone[r["outcome"]] += 1
            if "evidence" in q:
                gone[q["claim"]].append({**q, "url": src["url"], "why": WHY[r["outcome"]]})
            else:
                log.append({"article": q["article"], "source": src["id"], "url": src["url"],
                            "why": WHY[r["outcome"]], "quote": q["quote"]})
        if dry_run:
            continue
        found = [q["evidence"] for q in src["quotes"]
                 if "evidence" in q and q not in r["gone"] and r["outcome"] in ("same", "changed", "dropped")]
        failed = r["outcome"] in ("failing", "dead")
        update = {"checked_at": now, "check_error": r["error"], "failures": src["failures"] + 1 if failed else 0}
        try:
            if r["page"]:   # new text that still holds every quote
                update |= {"content_hash": r["page"]["content_hash"], "fetched_at": r["page"]["fetched_at"]}
                db().table("source_text").upsert(
                    {"source_id": src["id"], "text": r["page"]["text"][:PAGE_CHARS]}).execute()
            db().table("source").update(update).eq("id", src["id"]).execute()
            if found:
                db().table("evidence").update({"fetched_at": now}).in_("id", found).execute()
        except Exception as e:  # one page's failure doesn't stop the rest
            log.append({"url": src["url"], "refused": repr(e)})

    backing: dict[int, int] = {}
    if gone:
        out = {q["evidence"] for qs in gone.values() for q in qs}
        for e in all_rows(lambda: db().table("evidence").select("id, claim_id, source_id")
                          .in_("claim_id", list(gone)).eq("status", "published").order("id")):
            if e["id"] not in out and credited(e["source_id"]):
                backing[e["claim_id"]] = backing.get(e["claim_id"], 0) + 1
    waiting = all_rows(lambda: db().table("proposal").select("retires").eq("author", "audit")
                       .in_("status", ["open", "rejected"]).not_.is_("retires", "null").order("id"))
    skip = {c for p in waiting for c in p["retires"]}
    auto, person = decide(gone, backing, skip)

    retired = accepted = proposed = 0
    for cid, qs in ([] if dry_run else auto.items()):
        p = db().table("proposal").insert({"op": "retire", "author": "audit", "rationale": _why(qs),
                                           "retires_evidence": [q["evidence"] for q in qs]}).execute().data[0]
        try:
            db().rpc("accept_proposal", {"p_id": p["id"], "p_reviewer": "audit"}).execute()
            retired += len(qs)
            accepted += 1
        except Exception as e:  # a citation rule refused it: close it, so it isn't left open
            db().rpc("reject_proposal", {"p_id": p["id"], "p_reviewer": "audit", "p_reason": repr(e)}).execute()
            log.append({"claim": cid, "refused": repr(e)})
    for cid, qs in ([] if dry_run else person.items()):
        db().table("proposal").insert({"op": "retire", "author": "audit", "rationale": _why(qs), "retires": [cid],
                                       "retires_evidence": [q["evidence"] for q in qs]}).execute()
        proposed += 1

    articles = sum(1 for e in log if "article" in e)
    print(f"{len(srcs)} pages: {pages['same']} unchanged, {pages['changed']} changed with every quote, "
          f"{pages['dropped']} missing a quote, {pages['noai']} now noai, {pages['dead']} dead, "
          f"{pages['failing']} failing, {pages['blocked']} refusing us" + (f", {pages['error']} errors" if pages["error"] else ""))
    print(f"{sum(quotes_gone.values())} quotes gone ({quotes_gone['dropped']} dropped, {quotes_gone['dead']} dead, "
          f"{quotes_gone['noai']} noai): {retired if not dry_run else sum(map(len, auto.values()))} retired, "
          f"{proposed if not dry_run else len(person)} claims for a person, "
          f"{len(set(gone) - set(auto) - set(person))} kept or waiting; {articles} article quotes to fix")
    if dry_run:
        for e in log:
            print(" ", e)
        return
    db().table("run_log").update({"finished_at": datetime.now(timezone.utc).isoformat(),
                                  "proposals": accepted + proposed, "errors": log}).eq("id", run["id"]).execute()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="fetch and count; write nothing")
    audit(ap.parse_args().dry_run)
