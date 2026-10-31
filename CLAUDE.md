# CLAUDE.md

cocina is a curated knowledgebase of Mexican food concepts (dishes, salsas, chiles, techniques). Every fact is a small **claim** backed by **evidence**: a short quote from a credited **source**. Scripts and agents only *propose*. Auto-review publishes uncontested claims and writeups that say only what their cited claims say; a human decides the flagged ones (a conflict, a quote that doesn't state the claim, a sentence its claims don't support). Articles (phase 3) stay human-only. Consumers (cartoTaco's concierge first) read published data only. Design doc: "Culinary Knowledgebase: Design" (claude.ai/artifact/JKErHUCn6rYC4jYppQbJtv); phase plan in `docs/PLAN.md`.

## Layout

- `supabase/migrations/` – the schema, citation rules and read functions (`001_core`), proposal flags (`002`), writeups (`003`), retire (`005`), articles (`006`)
- `supabase/tests/` – pgTAP tests of the citation rules (`supabase test db`)
- `pipeline/` – Python 3.12: `seed`, `research`, `write`, `review`, `snapshot`, `gaps`, `article` (run as `python -m cocina.<name>` from `pipeline/`)
- `app/` – SvelteKit on Vercel: the read-only MCP server at `/mcp` and the review inbox at `/review` (Supabase magic-link sign-in, `REVIEWERS` allowlist; decisions go through the same `accept_proposal`/`reject_proposal`, run server-side with the secret key and signed with the reviewer's email)
- `.env` at the repo root is shared by the pipeline (symlinked as `pipeline/.env`) and the app (`kit.env.dir`); see `.env.example`

## Commands

- `supabase start` – local Postgres + REST (migrations applied). `supabase db reset` re-applies them
- `supabase test db` – citation-rule tests
- `cd pipeline && .venv/bin/pytest -q` – quote check, grouping, page text, article checks and edit file (no network)
- `cd app && pnpm check && pnpm test` – type check, node:test; `pnpm dev` serves `/mcp` and `/review`
- Inbox sign-in setup on the hosted project (dashboard, not migrations): Authentication → URL Configuration: Site URL = the deploy URL, Redirect URLs include `<deploy>/auth/confirm` (and `http://localhost:5173/auth/confirm` for dev); Authentication → Emails: the Magic Link and Confirm signup templates hold `supabase/templates/sign_in.html` (`config.toml` applies it to local stacks)
- `python -m cocina.seed` – proposed entities from cartoTaco's spec tables and Salsa Atlas (read-only, anon key; re-runnable)
- `python -m cocina.research <slug>...` – search → fetch → extract → quote check → proposals (~$0.15–0.25 and ~30 s per entity)
- `python -m cocina.wikidata <slug>... | --all` – English/Spanish labels and aliases of the one Wikidata food item named exactly like the entity, proposed as `also_known_as` claims (source: Wikidata, CC0); free
- `python -m cocina.write <slug>... | --all` – a writeup (2–4 cited sentences) from an entity's published claims; `--all` = served entities with no live writeup (~$0.005 each, ~$0.009 with a retry; auto-review ~$0.002)
- `python -m cocina.review --auto [--entity slug] [--recheck]` – after research or write: Claude rejects proposals whose quotes clearly don't state them, flags conflicts and judgement calls for a person (`proposal.flag`), and `accept_proposal` publishes the rest as `auto-review` (~$0.02 per entity). `--recheck` re-judges what is already flagged: it can reject the clear-cut, never accept. Checked in code whatever the model says: a fact a person rejected or retired, and an `also_known_as` that is another entity's name or a kind of this one named after another (`name_clash`, plurals folded)
- `python -m cocina.review [--entity slug] [--kind dish]` – a human decides what's still open, flag shown
- `python -m cocina.retire <claim id>... | --entity slug [--into slug] --why "…"` – a person takes published claims down (shows them, asks first). `--entity` retires the entity with its last claim; `--into` gives its name to a survivor as a search hint (duplicates)
- `python -m cocina.gaps [--max 5] [--budget 2] [--dry-run]` – the gap filler (daily in `.github/workflows/gaps.yml`): names cartoTaco's concierge had no entry for, read from LangSmith feedback `kb_miss` on its production and staging traces since the last finished run, skipped when `search_entities` already finds them (published or proposed), checked by Claude for being a Mexican food (kind and usual name), then `add_entity` → `research` → `review --auto` → `write` → `review --auto`, most 👎'd and most asked first, within the per-run caps; what the cap leaves goes first next run. Stdout (a public Actions log) carries counts and added slugs only; names and reasons are in the `run_log` row (agent `gaps`). Needs `LANGSMITH_API_KEY`
- `python -m cocina.article sources|draft <brief> | edit|publish <slug> | drop <slug> --why "…"` – articles, the one human-only publish path. A brief is a TOML file in the git-ignored `briefs/` (format in the module docstring): slug, title, angle, the owner's sources (KB ids, URLs, books and interviews with excerpt files marked `[p. 112]` / `[12:30]`). `draft` stores them, searches for up to 4 more pages (`search = false` stops it), and makes one Sonnet call over the whole sources (~$0.18, ~1 min); a sentence failing a check stays in the draft, marked. `edit` opens it in `$EDITOR` (one sentence per line, cites under `---`), `publish` re-checks and accepts as `COCINA_REVIEWER`
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
- `article` (slug, title, `sections` jsonb `[{heading, paragraphs: [[{text, cites: [{source, quote, locator}]}]]}]`, `sources` = the owner's picks, `found` = pages the drafter found, status, proposal): long-form prose citing places in sources, not claims, so books and interviews can be cited by page or timestamp. One published per slug; accepting a new version retires the old. `article.py` checks each quote against the text we hold (a source with none passes on the owner's word), copying (8 words in a row from any of the article's source texts) and sets a book's locator from its excerpt markers. No passages or embeddings: the drafter reads whole sources in one prompt.

## Rules the database enforces

Deferred constraint triggers check at commit, so a whole proposal is judged at once:
- a published claim needs published evidence from a source with an author or a publisher, and no `do_not_use` creator
- a published entity needs a published claim; a published claim's `object_id` must be published
- an alias with a `claim_id` is published only with its claim
- a published writeup's every sentence cites at least one claim, and each cited claim is its entity's own, published and still credited (`sentences_ok`); `card()` and RLS hide a writeup once a cited claim loses its credit
- a published article's every sentence has text and cites at least one credited source with a 1–400 character quote (`article_ok`); RLS and the reads hide it once a cited source loses its credit. `accept_proposal` refuses an article signed `auto-review` or `draft`: only a person publishes one

`accept_proposal(id, reviewer)` is the one gate: it publishes the proposal's rows (plus a seeded entity and its name hints on its first accepted claim) and retires what a `change` supersedes. `retire` (proposal.`retires` = claim ids, migration 005) takes claims and the aliases they back down, and an entity left with none goes too, with its names and writeup; nothing is deleted. `merge` isn't implemented (retire `--into` covers duplicates). `reject_proposal(id, reviewer, reason)` stores the reason for the agent. Both are service-role only.

## Reads (published only, whoever calls)

`cards()`, `get_entity(slug)` (claims with ids, the live `writeup`), `search_entities(q, kind, all, limit)` (exact → typo-tolerant with search.js's thresholds → whole-word containment), `related(slug, predicate, hops ≤ 2)`, `kb_version()` (last accepted decision; every MCP answer carries it), `articles()` and `get_article(slug)` (sections as stored plus `sources`: each cited source's credit by id; the MCP `articles` tool). RLS lets anon read published rows plus sources/creators; `proposal`, `run_log`, `source_text` are service-role only.

## Research pipeline notes

- Search uses `web_search_20250305` on purpose (the newer variant's filtering sandbox failed in cartoTacoMenuExtract); URLs come from the search result blocks, not model text. Social/aggregator hosts are skipped (`SKIP_HOSTS`).
- Pages are fetched by us (`fetch.py`, stdlib): robots.txt with our UA, `noai` meta respected, text hashed for the future auditor.
- **One robots exception:** Wikidata's Action API (`www.wikidata.org/w/api.php`, used only by `wikidata.py`). Its robots.txt disallows crawlers on `/w/`, but the API is documented for programs; the owner approved using it on 2026-10-28 with our UA, one request at a time and `maxlag`. Nothing else skips `fetch.allowed()`.
- Extraction: `claude-sonnet-5-5`, streamed, effort `medium`, structured output whose predicate enum comes from the DB. Server-side `fallbacks: "default"` is on for both calls.
- A claim is dropped unless its quote is on the fetched page (`quote_found`). Every run writes a `run_log` row (cost, drops, skips, caps).

## Conventions

- New migration: `supabase migration new <NNN_name> < /dev/null` (it reads SQL from stdin, so a non-interactive shell hangs without the redirect); CI applies migrations on merge to `main` (`migrate.yml`). Never paste SQL into the Supabase editor.
- Mark deliberate shortcuts with a `ponytail:` comment naming the ceiling and the upgrade path.
