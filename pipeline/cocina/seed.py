"""Seed proposed entities from cartoTaco's spec tables and Salsa Atlas.

    python -m cocina.seed

Reads cartoTaco with its anon key (public-read tables, so read-only by
construction). Each spec becomes a proposed entity with its name as an alias
and the old description kept in `note`, a hint for research, never evidence.
Entities stay proposed until research finds a sourced claim and a human
accepts it. Safe to re-run: existing slugs are skipped.
"""

import re

from postgrest.exceptions import APIError
from supabase import create_client

from cocina.db import client as db, env, norm

# Menu categories cartoTaco tracks (menu.<x>_yes) that have no description anywhere
MENU_CATEGORIES = [
    ("Taco", []), ("Burro", ["burrito"]), ("Torta", []), ("Sonoran hot dog", ["dogo", "hot dog"]),
    ("Gordita", []), ("Huarache", []), ("Cemita", []), ("Flauta", ["taco dorado"]), ("Chalupa", []),
    ("Molote", []), ("Tostada", []), ("Enchilada", []), ("Tamal", ["tamale"]), ("Sope", []),
    ("Caldo", []), ("Quesadilla", []),
]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(name)).strip("-")


def _note(table: str, row: dict, *fields: str) -> str:
    parts = [f"{f}: {row[f]}" for f in fields if row.get(f)]
    return f"Imported from cartoTaco {table} #{row.get('id') or row.get('slug')}. " + " | ".join(parts)


def seed() -> None:
    ct = create_client(env("CARTOTACO_SUPABASE_URL"), env("CARTOTACO_ANON_KEY"))
    try:
        styles = ct.table("salsa_styles").select("*").eq("status", "published").execute().data
    except APIError as e:
        if e.code != "PGRST205":
            raise
        print("No salsa_styles in this cartoTaco database (the atlas hasn't shipped there): salsas come from salsa_spec only")
        styles = []
    styled_specs = {s["spec_id"] for s in styles if s.get("spec_id")}

    rows: list[tuple[str, str, str, str, list[str]]] = []          # slug, kind, name, note, aliases
    for s in styles:
        rows.append((s["slug"], "salsa", s["name"],
                     _note("salsa_styles", s, "origin", "history", "ingredients"), []))
    for table, kind in (("item_spec", "dish"), ("protein_spec", "protein_prep"), ("salsa_spec", "salsa")):
        for r in ct.table(table).select("*").execute().data:
            if table == "salsa_spec" and r["id"] in styled_specs:
                continue                                             # the atlas entry covers it
            rows.append((slugify(r["name"]), kind, r["name"].strip(),
                         _note(table, r, "origin", "short_descrip", "long_descrip"), []))
    for name, aliases in MENU_CATEGORIES:
        rows.append((slugify(name), "menu_category", name, "Menu category tracked by cartoTaco.", aliases))

    have = {e["slug"] for e in db().table("entity").select("slug").execute().data}
    added = 0
    for slug, kind, name, note, aliases in rows:
        if slug in have:
            continue
        have.add(slug)
        entity = db().table("entity").insert({"slug": slug, "kind": kind, "name": name, "note": note}).execute().data[0]
        names = {norm(n): n for n in [name, *aliases]}               # dedupe by normalised form
        db().table("alias").insert([{"entity_id": entity["id"], "text": n} for n in names.values()]).execute()
        added += 1
    print(f"{added} entities added, {len(rows) - added} already present")


if __name__ == "__main__":
    seed()
