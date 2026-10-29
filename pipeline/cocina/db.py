"""Supabase client for the pipeline. Service role: it writes proposals."""

import os
import unicodedata
from functools import lru_cache

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@lru_cache
def client():
    return create_client(env("COCINA_SUPABASE_URL"), env("COCINA_SUPABASE_SERVICE_KEY"))


def norm(text: str | None) -> str:
    """Python twin of SQL norm(): lowercase, accents stripped, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


def all_rows(query, page: int = 1000) -> list[dict]:
    """Every row of a query, paging past PostgREST's max_rows (1000).
    `query` builds a fresh, ordered request each call: lambda: client().table(…).select(…).order("id")."""
    rows: list[dict] = []
    while True:
        batch = query().range(len(rows), len(rows) + page - 1).execute().data
        rows += batch
        if len(batch) < page:
            return rows


def fold(text: str | None) -> str:
    """norm() with a plural folded away per word, so "Tacos árabes" and "Taco Arabe" compare equal.
    ponytail: drops a trailing s only (frijoles → frijole); enough to compare names"""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in norm(text).split())
