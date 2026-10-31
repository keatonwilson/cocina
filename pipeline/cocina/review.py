"""Review open proposals: auto-review publishes the uncontested ones, a human the rest.

    python -m cocina.review --auto [--entity chavindeca]     # after research
    python -m cocina.review [--entity chavindeca] [--kind dish]

--auto: Claude checks each entity's open proposals against its published facts and
each other. A proposal whose quotes clearly don't state it is rejected; one that can't be
true alongside another fact, or is a judgement call, is flagged for a human; the rest go
through accept_proposal, so the citation rules still decide. --recheck re-judges what is
already flagged (it can reject, never accept). A writeup (cocina.write) is checked sentence by sentence
against the claims it cites. Articles (phase 3) stay human-only.

In the terminal: a accept · r reject (asks why) · s skip · o open the sources · q quit.
The auditor's claim retires come first (a retire · r keep: it isn't proposed again), after
the article quotes its last run found gone from their pages.
"""

import argparse
import getpass
from collections import defaultdict
from urllib.parse import urlsplit
import os
import webbrowser
from typing import Literal

import anthropic
from pydantic import BaseModel

from functools import partial
from types import SimpleNamespace

from cocina.db import all_rows, client as db, fold, norm
from cocina.research import FALLBACK, MODEL, _cost

SELECT = (
    "id, author, rationale, flag, created_at, "
    "claim(id, predicate, value, status, entity:entity!claim_entity_id_fkey(id, slug, name, kind), "
    "object:entity!claim_object_id_fkey(name)), "
    "evidence(quote, locator, claim:claim(id, predicate, value, entity:entity!claim_entity_id_fkey(id, slug, name, kind)), "
    "source(title, url, publisher, published_on, source_creator(creator(name)))), "
    "writeup(id, sentences, entity:entity(id, slug, name, kind))"
)


def credit(src: dict) -> str:
    authors = ", ".join(sc["creator"]["name"] for sc in src.get("source_creator") or [])
    bits = [authors, src["title"], src.get("publisher"), (src.get("published_on") or "")[:4]]
    return " · ".join(b for b in bits if b) or "(no credit)"


def show(p: dict, n: int, total: int) -> list[str]:
    if p.get("retires"):
        return _show_retire(p, n, total)
    if p.get("writeup"):
        return _show_writeup(p, n, total)
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


def _cited(w: dict) -> dict[int, dict]:
    """The card's claims by id, for the claims a writeup cites (a missing id: no longer published)."""
    card = db().rpc("get_entity", {"p_slug": w["entity"]["slug"]}).execute().data or {"claims": []}
    return {c["id"]: c for c in card["claims"]}


def _show_writeup(p: dict, n: int, total: int) -> list[str]:
    w = p["writeup"][0]
    facts = _cited(w)
    print(f"\n[{n}/{total}] {w['entity']['name']} ({w['entity']['kind']}) · writeup")
    print(f"  {p['author']} · {p['rationale'] or ''}")
    if p.get("flag"):
        print(f"  ⚠ {p['flag']}")
    urls = []
    for i, sentence in enumerate(w["sentences"], 1):
        print(f"  {i}. {sentence['text']}")
        for cid in sentence["claims"]:
            c = facts.get(cid)
            print(f"       [{cid}] {c['predicate']}: {c['value']}" if c else f"       [{cid}] (no longer published)")
            urls += [src["url"] for src in (c or {}).get("sources", []) if src.get("url") and src["url"] not in urls]
    return urls


def audit_retires(entity: str | None = None, kind: str | None = None) -> list[dict]:
    """Open claim retires (the auditor's), each with its claims and their quotes."""
    rows = (db().table("proposal").select("id, author, rationale, retires, retires_evidence")
            .eq("status", "open").eq("op", "retire").not_.is_("retires", "null").order("id").execute().data)
    out = []
    for p in rows:
        p["claims"] = (db().table("claim").select(
            "id, predicate, value, entity:entity!claim_entity_id_fkey(slug, name, kind), "
            "evidence(id, quote, source(title, url, publisher, published_on, source_creator(creator(name))))")
            .in_("id", p["retires"]).execute().data)
        e = p["claims"][0]["entity"] if p["claims"] else {}
        if (not entity or e.get("slug") == entity) and (not kind or e.get("kind") == kind):
            out.append(p)
    return out


def _show_retire(p: dict, n: int, total: int) -> list[str]:
    gone, urls = set(p["retires_evidence"] or []), []
    for c in p["claims"]:
        e = c["entity"]
        print(f"\n[{n}/{total}] {e['name']} ({e['kind']}) · {c['predicate']} = \"{c['value']}\" · take it down?")
        print(f"  {p['author']} · {p['rationale'] or ''}")
        for ev in c["evidence"]:
            if ev["id"] in gone:
                print(f"  gone: \"{ev['quote']}\"")
                print(f"     — {credit(ev['source'])}")
                print(f"       {ev['source'].get('url') or ''}")
                urls += [ev["source"]["url"]] if ev["source"].get("url") else []
    return urls


def article_notes() -> None:
    """The article quotes the last audit found gone from their pages: a person fixes them in `article edit`."""
    last = (db().table("run_log").select("started_at, errors").eq("agent", "audit").not_.is_("finished_at", "null")
            .order("started_at", desc=True).limit(1).execute().data)
    notes = defaultdict(list)
    for e in last[0]["errors"] if last else []:
        if "article" in e:
            notes[e["article"]].append(e)
    for slug, es in notes.items():
        print(f"article {slug}: {len(es)} quote(s) gone (audit of {last[0]['started_at'][:10]}); "
              f"fix with python -m cocina.article edit {slug}")
        for e in es:
            print(f"  \"{e['quote'][:100]}\" · source {e['source']}: {e['why']}")


def _target(p: dict) -> dict | None:
    """The claim a proposal is about: its new claim, or the published one it adds evidence to."""
    return (p["claim"] or [None])[0] or (p["evidence"][0]["claim"] if p["evidence"] else None)


def about(p: dict) -> dict:
    if p.get("writeup"):
        return p["writeup"][0]["entity"]
    t = _target(p)
    return t["entity"] if t else {}


def open_proposals(entity: str | None = None, kind: str | None = None) -> list[dict]:
    rows = all_rows(lambda: db().table("proposal").select(SELECT).eq("status", "open").order("id"))
    # Claims, evidence and writeups only: an article is published with `cocina.article publish`
    return [p for p in rows if about(p) and (not entity or about(p).get("slug") == entity)
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
true at once (several ingredients, regional variants, a second name, a place inside another place \
such as Mexico City and Mexico) are not conflicts. Flag every proposal on each side of a conflict. A \
proposal that restates a rejected fact conflicts with it.

For an unsupported proposal, set `borderline` true only when a careful editor could reasonably \
read the quote as stating the fact. A quote that says the reverse (X is a variant of Y, for "Y is \
a variant of X"), states only part of the fact, is about another food, or is one cook's choice \
(a substitution) rather than a fact about the dish is not borderline: those are rejected without \
a person. Every borderline flag, and every conflict, waits for a person. Never output a flag for \
a proposal you find acceptable.

Flag nothing else; everything you don't flag is published. `why` is one sentence naming the other \
facts (#id, P id or R id) involved."""


class Flag(BaseModel):
    proposal: int
    kind: Literal["unsupported", "conflict"]
    why: str
    borderline: bool


class Verdict(BaseModel):
    flags: list[Flag]


AUTO_WRITEUP_SYSTEM = """You check a short description of a Mexican food before it publishes without \
a human reading it. Each sentence (S1, S2…) is followed by the facts it cites, by id.

List a problem for a sentence only when it states something its cited facts don't: a detail, \
place, date, ingredient, quality or comparison that isn't in them, or something that contradicts \
them. Rewording, joining facts and leaving facts out are fine, and so is "sources differ" when \
cited facts from different sources give different answers to the same question. List nothing \
else; a description with no problems is published."""


class Problem(BaseModel):
    sentence: int
    why: str


class WriteupCheck(BaseModel):
    problems: list[Problem]


def split(proposals: list[dict], flags: list, rejected: dict[tuple[str, str], str],
          recheck: bool = False) -> tuple[list[int], dict[int, str], dict[int, str]]:
    """(accept, {proposal: flag for a person}, {proposal: reason to reject it}).

    Rejected without a person: an unsupported proposal the model calls not borderline, a code flag
    marked clear (a "kind of taco" name), and a new claim restating a fact a person rejected
    or retired (rejected: (predicate, norm(value)) → reason), whatever the model says. Every
    other flag waits for a person, conflicts always. A flag naming an id we didn't send is
    ignored. recheck: judge already-flagged proposals too, but never accept one; unless it
    can be rejected, a person still decides it."""
    was_flagged = {p["id"] for p in proposals if p.get("flag")}
    asked = {p["id"] for p in proposals} - (set() if recheck else was_flagged)
    flag: dict[int, str] = {}
    reject: dict[int, str] = {}
    for f in flags:   # later entries win: code flags come after the model's
        if f.proposal not in asked:
            continue
        clear = getattr(f, "clear", False) or (f.kind == "unsupported" and not getattr(f, "borderline", True))
        flag.pop(f.proposal, None)
        reject.pop(f.proposal, None)
        (reject if clear else flag)[f.proposal] = f"{f.kind}: {f.why}"
    for p in proposals:
        c = (p.get("claim") or [None])[0]
        if p["id"] in asked and c and (c["predicate"], norm(c["value"])) in rejected:
            flag.pop(p["id"], None)
            reject[p["id"]] = rejected[(c["predicate"], norm(c["value"]))]
    accept = [p["id"] for p in proposals
              if p["id"] in asked - was_flagged and p["id"] not in flag and p["id"] not in reject]
    return accept, flag, reject


def name_clash(value: str, own: str, others: dict[str, set[str]], family: bool = False) -> str | None:
    """Why an also_known_as value can't be another name for this entity, or None.
    `others`: folded names and aliases of every other entity → their slugs. `family`: the
    entity is a generic menu category (Taco, Burro), whose "kind of" names are the trap;
    "tacos al pastor" is a fine name for Al Pastor, not for Taco."""
    v = fold(value)
    if v in others:   # "tacos árabes" for Al Pastor, when Taco Arabe exists
        return f"also the name of {', '.join(sorted(others[v]))}"
    mine = fold(own)
    # "tacos al pastor" or "fish tacos" for Taco: a kind of taco, not another name for one ("Tacos" is fine)
    if family and v != mine and f" {mine} " in f" {v} ":
        inside = sorted({slug for n, slugs in others.items()
                         if len(n) >= 4 and n not in mine and f" {n} " in f" {v} " for slug in slugs})
        return f"names a kind of {own}" + (f" made with {', '.join(inside)}" if inside else "") + ", not another name for it"
    return None


def _all_names() -> dict[str, set[str]]:
    """Folded names and live aliases of every live entity → slugs."""
    ents = all_rows(lambda: db().table("entity").select("id, slug, name").in_("status", ["proposed", "published"]).order("id"))
    slug = {e["id"]: e["slug"] for e in ents}
    names: dict[str, set[str]] = {}
    for e in ents:
        names.setdefault(fold(e["name"]), set()).add(e["slug"])
    for a in all_rows(lambda: db().table("alias").select("entity_id, text").in_("status", ["proposed", "published"]).order("id")):
        if a["entity_id"] in slug:
            names.setdefault(fold(a["text"]), set()).add(slug[a["entity_id"]])
    return names


def _facts(ent: dict, decided: list[dict], proposals: list[dict]) -> str:
    lines = [f"Food: {ent['name']} ({ent['kind']})", "", "Published and rejected facts:"]
    lines += [f"P{c['id']} {c['predicate']} = \"{c['value']}\"" if c["status"] == "published" else
              f"R{c['id']} {c['predicate']} = \"{c['value']}\" ({_why_down(c)})"
              for c in decided] or ["(none)"]
    lines += ["", "Proposed facts:"]
    for p in proposals:
        t = _target(p)
        more = "" if p["claim"] else f" (another source for P{t['id']})"
        lines.append(f"#{p['id']}{more} {t['predicate']} = \"{t['value']}\"")
        lines += [f"   quote: \"{ev['quote']}\"" for ev in p["evidence"]]
    return "\n".join(lines)


def taken_down(decided: list[dict]) -> list[dict]:
    """The claims a person took down: retired, or rejected by someone other than auto-review.
    Auto-review's own rejections were about one quote, not the fact, so a later proposal of
    the same fact (with a better quote) is judged afresh rather than blocked."""
    return [c for c in decided if c["status"] == "retired"
            or (c["status"] == "rejected" and (c["proposal"] or {}).get("decided_by") != "auto-review")]


def _why_down(c: dict) -> str:
    """A rejected or retired claim, as auto-review describes it."""
    return "retired by a reviewer" if c["status"] == "retired" else f"rejected: {(c['proposal'] or {}).get('reason')}"


def auto_review(entity: str | None = None, recheck: bool = False) -> None:
    """recheck: also re-judge claim proposals already flagged, rejecting the clear-cut ones
    (it never accepts a flagged proposal)."""
    ai = anthropic.Anthropic(timeout=600, max_retries=1)
    by_entity: dict[int, list[dict]] = {}
    writeups = []
    for p in open_proposals(entity):
        if p.get("writeup"):
            writeups.append(p)
        elif _target(p):
            by_entity.setdefault(about(p)["id"], []).append(p)
    # Claims first: a writeup only cites what is already published
    # An entity whose open proposals are all flagged has nothing new to judge (they wait for a human)
    names = _all_names() if by_entity else {}
    jobs = [(partial(_auto_claims, names=names, recheck=recheck), about(ps[0]), ps)
            for ps in by_entity.values() if recheck or any(not p.get("flag") for p in ps)]
    jobs += [(_auto_writeup, about(p), [p]) for p in writeups if not p.get("flag")]
    for job, ent, proposals in jobs:
        try:
            job(ai, ent, proposals)
        except Exception as e:  # one failure doesn't stop the rest
            # ponytail: a DB error after the model call loses that call's cost from run_log
            print(f"{ent['slug']}: auto-review failed: {e!r}; "
                  "anything accepted before the error stays published, the rest stays open")
            db().table("run_log").insert({"agent": "auto-review", "errors": [{"error": repr(e)}]}).execute()


def _check(ai: anthropic.Anthropic, system: str, content: str, schema) -> tuple[object, float]:
    """One structured model call: (parsed output or None, cost)."""
    with ai.beta.messages.stream(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "medium"},
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=schema,
        **FALLBACK,
    ) as stream:
        resp = stream.get_final_message()
    ok = resp.stop_reason != "refusal" and resp.parsed_output is not None
    return (resp.parsed_output if ok else None), round(_cost(resp.usage), 4)


def _publish(label: str, accept: list[int], flags: dict[int, str], cost: float,
             reject: dict[int, str] | None = None) -> None:
    """Reject what is clearly wrong, store the flags, accept the rest through the gate, log the run."""
    reject = reject or {}
    for pid, why in reject.items():   # the reason goes back to the agent; auto-review's own rejections don't block the fact (a better quote may return)
        db().rpc("reject_proposal", {"p_id": pid, "p_reviewer": "auto-review", "p_reason": why}).execute()
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
    print(f"{label}: {len(accept) - len(refused)} accepted, {len(reject)} rejected, {len(flags)} flagged for you, "
          f"{len(refused)} refused · ${cost:.3f}")
    for pid, why in flags.items():
        print(f"  #{pid} {why}")
    for r in refused:
        print(f"  refused {r}")


def _failed(label: str, cost: float) -> None:
    print(f"{label}: auto-review failed (refused or unparsed); nothing accepted")
    db().table("run_log").insert({"agent": "auto-review", "cost_usd": cost,
                                  "errors": [{"error": f"{label}: refused or unparsed"}]}).execute()


def _auto_claims(ai: anthropic.Anthropic, ent: dict, proposals: list[dict], names: dict[str, set[str]],
                 recheck: bool = False) -> None:
    decided = (db().table("claim").select("id, predicate, value, status, proposal(reason, decided_by)")
               .eq("entity_id", ent["id"]).in_("status", ["published", "rejected", "retired"]).execute().data)
    # A fact a person took down (rejected or retired) never comes back on its own
    down = taken_down(decided)
    rejected = {(c["predicate"], norm(c["value"])): f"conflict: restates R{c['id']}, {_why_down(c)}" for c in down}
    decided = [c for c in decided if c["status"] == "published" or c in down]   # what the model sees as R
    others = {n: slugs - {ent["slug"]} for n, slugs in names.items() if slugs - {ent["slug"]}}
    # A "kind of taco" name is clearly not another name; a name another entity uses may be shared: a person decides
    clashes = [SimpleNamespace(proposal=p["id"], kind="conflict", why=why, clear=why.startswith("names a kind of"))
               for p in proposals
               if (c := (p.get("claim") or [None])[0]) and c["predicate"] == "also_known_as"
               and (why := name_clash(c["value"], ent["name"], others, family=ent["kind"] == "menu_category"))]
    out, cost = _check(ai, AUTO_SYSTEM, _facts(ent, decided, proposals), Verdict)
    if out is None:
        return _failed(ent["slug"], cost)
    # A name that belongs to another entity is flagged whatever the model says (later flags win)
    accept, flags, reject = split(proposals, [*out.flags, *clashes], rejected, recheck)
    _publish(ent["slug"], accept, flags, cost, reject)


def writeup_text(w: dict, facts: dict[int, dict]) -> str:
    lines = [f"Food: {w['entity']['name']} ({w['entity']['kind']})", ""]
    for i, sentence in enumerate(w["sentences"], 1):
        lines.append(f"S{i}: {sentence['text']}")
        lines += [f"   [{cid}] {facts[cid]['predicate']}: {facts[cid]['value']} (from {_hosts(facts[cid])})"
                  for cid in sentence["claims"]]
    return "\n".join(lines)


def _hosts(claim: dict) -> str:
    """Where a card claim's sources live, so a judge can see whether two facts share one."""
    hosts = {urlsplit(s["url"]).netloc.removeprefix("www.") if s.get("url") else s.get("publisher") or "?"
             for s in claim["sources"]}
    return ", ".join(sorted(hosts))


def _auto_writeup(ai: anthropic.Anthropic, ent: dict, proposals: list[dict]) -> None:
    p = proposals[0]
    w = p["writeup"][0]
    facts = _cited(w)
    label = f"{ent['slug']} (writeup)"
    gone = sorted({cid for s in w["sentences"] for cid in s["claims"]} - set(facts))
    if gone:  # the gate would refuse it: say why instead
        return _publish(label, [], {p["id"]: f"stale: cites claims that are no longer published: {gone}"}, 0)
    out, cost = _check(ai, AUTO_WRITEUP_SYSTEM, writeup_text(w, facts), WriteupCheck)
    if out is None:
        return _failed(label, cost)
    if out.problems:
        why = "unsupported: " + "; ".join(f"S{x.sentence}: {x.why}" for x in out.problems)
        return _publish(label, [], {p["id"]: why}, cost)
    _publish(label, [p["id"]], {}, cost)


def review(entity: str | None, kind: str | None) -> None:
    who = os.getenv("COCINA_REVIEWER") or getpass.getuser()
    if not entity and not kind:
        article_notes()
    rows = audit_retires(entity, kind) + open_proposals(entity, kind)
    done = {"accepted": 0, "rejected": 0, "skipped": 0}
    for n, p in enumerate(rows, 1):
        urls = show(p, n, len(rows))
        while True:
            key = input("  a retire · r keep · s skip · o open · q quit > " if p.get("retires") else
                        "  a accept · r reject · s skip · o open · q quit > ").strip().lower()
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
                reason = input("  why? ").strip() or ("kept by a person" if p.get("retires") else "no reason given")
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
    ap.add_argument("--recheck", action="store_true", help="with --auto: re-judge flagged claims, rejecting clear-cut ones")
    a = ap.parse_args()
    auto_review(a.entity, a.recheck) if a.auto else review(a.entity, a.kind)
