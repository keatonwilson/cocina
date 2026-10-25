# CLAUDE.md

cocina is a curated knowledgebase of Mexican food concepts (dishes, salsas, chiles, techniques). Every fact is a small **claim** backed by **evidence**: a short quote from a credited **source**. Scripts and agents only *propose*; a human publishes. Consumers (cartoTaco's concierge first) read published data only. Design doc: "Culinary Knowledgebase: Design" (claude.ai/artifact/JKErHUCn6rYC4jYppQbJtv); phase plan in `docs/PLAN.md`.

## Layout

- `supabase/migrations/` – the schema, citation rules and read functions (`001_core.sql`)
- `supabase/tests/` – pgTAP tests of the citation rules (`supabase test db`)
- `pipeline/` – Python 3.12: `seed`, `research`, `review`, `snapshot` (run as `python -m cocina.<name>` from `pipeline/`)
- `app/` – SvelteKit on Vercel: the read-only MCP server at `/mcp` (the review inbox comes here in phase 2)
- `.env` at the repo root is shared by the pipeline (symlinked as `pipeline/.env`) and the app (`kit.env.dir`); see `.env.example`

## Commands

- `supabase start` – local Postgres + REST (migrations applied). `supabase db reset` re-applies them
- `supabase test db` – citation-rule tests
- `cd pipeline && .venv/bin/pytest -q` – quote check, grouping, page text (no network)
- `cd app && pnpm check` – type check; `pnpm dev` serves `/mcp`
- `python -m cocina.seed` – proposed entities from cartoTaco's spec tables and Salsa Atlas (read-only, anon key; re-runnable)
- `python -m cocina.research <slug>...` – search → fetch → extract → quote check → proposals (~$0.15–0.25 and ~30 s per entity)
- `python -m cocina.review [--entity slug] [--kind dish]` – accept/reject in the terminal
- `python -m cocina.snapshot [--release]` – `snapshots/kb-YYYY.MM.DD.jsonl`, optionally a GitHub release

## Data model (and why)

- `entity` (slug, kind, name, `note`) → `claim` (predicate, `value text`, optional `object_id`) → `evidence` (quote ≤ 400 chars, locator) → `source` (+ `source_creator` → `creator`). `source_text` holds the fetched text quotes are checked against; it is never served.
- **Edges are claims** with an `object_id` (`variant_of`, `uses`, `served_with`, `originates_in`…), so links need evidence like any fact. Which predicates may link, per kind, is in `predicate`.
- **One fact per claim**, `value` as short text; agreeing sources become several evidence rows on one claim (unique on entity + predicate + `norm(value)`).
- **Drafts live in the real tables**: `entity`, `claim`, `alias` and `evidence` carry `status` (`proposed | published | rejected | retired`) and `proposal_id`. `proposal` is a thin header (author, rationale, decision, reason).
- `confidence` (distinct sites/works agreeing) and `verified_at` are derived in `card()`, never stored.
- `alias.claim_id` is set for a sourced name (`also_known_as`), null for a search hint (seed names, misspellings).
- Seed text from cartoTaco goes into `entity.note`: a research hint ("may contain errors"), never evidence.

## Rules the database enforces

Deferred constraint triggers check at commit, so a whole proposal is judged at once:
- a published claim needs published evidence from a source with an author or a publisher, and no `do_not_use` creator
- a published entity needs a published claim; a published claim's `object_id` must be published
- an alias with a `claim_id` is published only with its claim

`accept_proposal(id, reviewer)` is the one gate: it publishes the proposal's rows (plus a seeded entity and its name hints on its first accepted claim) and retires what a `change` supersedes. `retire`/`merge` ops aren't implemented yet (phase 2/3). `reject_proposal(id, reviewer, reason)` stores the reason for the agent. Both are service-role only.

## Reads (published only, whoever calls)

`cards()`, `get_entity(slug)`, `search_entities(q, kind, all, limit)` (exact → typo-tolerant with search.js's thresholds → whole-word containment), `related(slug, predicate, hops ≤ 2)`, `kb_version()` (last accepted decision; every MCP answer carries it). RLS lets anon read published rows plus sources/creators; `proposal`, `run_log`, `source_text` are service-role only.

## Research pipeline notes

- Search uses `web_search_20250305` on purpose (the newer variant's filtering sandbox failed in cartoTacoMenuExtract); URLs come from the search result blocks, not model text. Social/aggregator hosts are skipped (`SKIP_HOSTS`).
- Pages are fetched by us (`fetch.py`, stdlib): robots.txt with our UA, `noai` meta respected, text hashed for the future auditor.
- Extraction: `claude-sonnet-5-5`, streamed, effort `medium`, structured output whose predicate enum comes from the DB. Server-side `fallbacks: "default"` is on for both calls.
- A claim is dropped unless its quote is on the fetched page (`quote_found`). Every run writes a `run_log` row (cost, drops, skips, caps).

## Conventions

- New migration: `supabase migration new <NNN_name>`; CI applies migrations on merge to `main` (`migrate.yml`). Never paste SQL into the Supabase editor.
- Mark deliberate shortcuts with a `ponytail:` comment naming the ceiling and the upgrade path.
