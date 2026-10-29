# CLAUDE.md

cocina is a curated knowledgebase of Mexican food concepts (dishes, salsas, chiles, techniques). Every fact is a small **claim** backed by **evidence**: a short quote from a credited **source**. Scripts and agents only *propose*. Auto-review publishes uncontested claims and writeups that say only what their cited claims say; a human decides the flagged ones (a conflict, a quote that doesn't state the claim, a sentence its claims don't support). Articles (phase 3) stay human-only. Consumers (cartoTaco's concierge first) read published data only. Design doc: "Culinary Knowledgebase: Design" (claude.ai/artifact/JKErHUCn6rYC4jYppQbJtv); phase plan in `docs/PLAN.md`.

## Layout

- `supabase/migrations/` – the schema, citation rules and read functions (`001_core`), proposal flags (`002`), writeups (`003`)
- `supabase/tests/` – pgTAP tests of the citation rules (`supabase test db`)
- `pipeline/` – Python 3.12: `seed`, `research`, `write`, `review`, `snapshot` (run as `python -m cocina.<name>` from `pipeline/`)
- `app/` – SvelteKit on Vercel: the read-only MCP server at `/mcp` and the review inbox at `/review` (Supabase magic-link sign-in, `REVIEWERS` allowlist; decisions go through the same `accept_proposal`/`reject_proposal`, run server-side with the secret key and signed with the reviewer's email)
- `.env` at the repo root is shared by the pipeline (symlinked as `pipeline/.env`) and the app (`kit.env.dir`); see `.env.example`

## Commands

- `supabase start` – local Postgres + REST (migrations applied). `supabase db reset` re-applies them
- `supabase test db` – citation-rule tests
- `cd pipeline && .venv/bin/pytest -q` – quote check, grouping, page text (no network)
- `cd app && pnpm check && pnpm test` – type check, node:test; `pnpm dev` serves `/mcp` and `/review`
- `python -m cocina.seed` – proposed entities from cartoTaco's spec tables and Salsa Atlas (read-only, anon key; re-runnable)
- `python -m cocina.research <slug>...` – search → fetch → extract → quote check → proposals (~$0.15–0.25 and ~30 s per entity)
- `python -m cocina.wikidata <slug>... | --all` – English/Spanish labels and aliases of the one Wikidata food item named exactly like the entity, proposed as `also_known_as` claims (source: Wikidata, CC0); free
- `python -m cocina.write <slug>... | --all` – a writeup (2–4 cited sentences) from an entity's published claims; `--all` = served entities with no live writeup (~$0.02 each)
- `python -m cocina.review --auto [--entity slug]` – after research or write: Claude flags conflicting or unsupported proposals (`proposal.flag`), `accept_proposal` publishes the rest as `auto-review` (~$0.02 per entity). Checked in code whatever the model says: a fact a person rejected or retired, and an `also_known_as` that is another entity's name or a kind of this one named after another (`name_clash`, plurals folded)
- `python -m cocina.review [--entity slug] [--kind dish]` – a human decides what's still open, flag shown
- `python -m cocina.retire <claim id>... | --entity slug [--into slug] --why "…"` – a person takes published claims down (shows them, asks first). `--entity` retires the entity with its last claim; `--into` gives its name to a survivor as a search hint (duplicates)
- `python -m cocina.snapshot [--release]` – `snapshots/kb-YYYY.MM.DD.jsonl`, optionally a GitHub release (a second one that day is tagged `.2`)

## Data model (and why)

- `entity` (slug, kind, name, `note`) → `claim` (predicate, `value text`, optional `object_id`) → `evidence` (quote ≤ 400 chars, locator) → `source` (+ `source_creator` → `creator`). `source_text` holds the fetched text quotes are checked against; it is never served.
- **Edges are claims** with an `object_id` (`variant_of`, `uses`, `served_with`, `originates_in`…), so links need evidence like any fact. Which predicates may link, per kind, is in `predicate`.
- **One fact per claim**, `value` as short text; agreeing sources become several evidence rows on one claim (unique on entity + predicate + `norm(value)`).
- **Drafts live in the real tables**: `entity`, `claim`, `alias` and `evidence` carry `status` (`proposed | published | rejected | retired`) and `proposal_id`. `proposal` is a thin header (author, rationale, decision, reason).
- `confidence` (distinct sites/works agreeing) and `verified_at` are derived in `card()`, never stored.
- `alias.claim_id` is set for a sourced name (`also_known_as`), null for a search hint (seed names, misspellings).
- Seed text from cartoTaco goes into `entity.note`: a research hint ("may contain errors"), never evidence.
- `writeup` (entity, `sentences` jsonb `[{text, claims: [ids]}]`, status, proposal): prose made only from published claims, every sentence citing the claims it states. One published per entity; accepting a new one retires the old. `write.py` refuses a sentence that copies 8+ consecutive words of a quote.

## Rules the database enforces

Deferred constraint triggers check at commit, so a whole proposal is judged at once:
- a published claim needs published evidence from a source with an author or a publisher, and no `do_not_use` creator
- a published entity needs a published claim; a published claim's `object_id` must be published
- an alias with a `claim_id` is published only with its claim
- a published writeup's every sentence cites at least one claim, and each cited claim is its entity's own, published and still credited (`sentences_ok`); `card()` and RLS hide a writeup once a cited claim loses its credit

`accept_proposal(id, reviewer)` is the one gate: it publishes the proposal's rows (plus a seeded entity and its name hints on its first accepted claim) and retires what a `change` supersedes. `retire` (proposal.`retires` = claim ids, migration 005) takes claims and the aliases they back down, and an entity left with none goes too, with its names and writeup; nothing is deleted. `merge` isn't implemented (retire `--into` covers duplicates). `reject_proposal(id, reviewer, reason)` stores the reason for the agent. Both are service-role only.

## Reads (published only, whoever calls)

`cards()`, `get_entity(slug)` (claims with ids, the live `writeup`), `search_entities(q, kind, all, limit)` (exact → typo-tolerant with search.js's thresholds → whole-word containment), `related(slug, predicate, hops ≤ 2)`, `kb_version()` (last accepted decision; every MCP answer carries it). RLS lets anon read published rows plus sources/creators; `proposal`, `run_log`, `source_text` are service-role only.

## Research pipeline notes

- Search uses `web_search_20250305` on purpose (the newer variant's filtering sandbox failed in cartoTacoMenuExtract); URLs come from the search result blocks, not model text. Social/aggregator hosts are skipped (`SKIP_HOSTS`).
- Pages are fetched by us (`fetch.py`, stdlib): robots.txt with our UA, `noai` meta respected, text hashed for the future auditor.
- **One robots exception:** Wikidata's Action API (`www.wikidata.org/w/api.php`, used only by `wikidata.py`). Its robots.txt disallows crawlers on `/w/`, but the API is documented for programs; the owner approved using it on 2026-10-28 with our UA, one request at a time and `maxlag`. Nothing else skips `fetch.allowed()`.
- Extraction: `claude-sonnet-5-5`, streamed, effort `medium`, structured output whose predicate enum comes from the DB. Server-side `fallbacks: "default"` is on for both calls.
- A claim is dropped unless its quote is on the fetched page (`quote_found`). Every run writes a `run_log` row (cost, drops, skips, caps).

## Conventions

- New migration: `supabase migration new <NNN_name> < /dev/null` (it reads SQL from stdin, so a non-interactive shell hangs without the redirect); CI applies migrations on merge to `main` (`migrate.yml`). Never paste SQL into the Supabase editor.
- Mark deliberate shortcuts with a `ponytail:` comment naming the ceiling and the upgrade path.
