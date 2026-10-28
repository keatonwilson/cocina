# Plan: cocina, a culinary knowledgebase (phase 1 in detail, phases 2–4 as milestones)

## Context

Design doc: "Culinary Knowledgebase: Design" (claude.ai/artifact/JKErHUCn6rYC4jYppQbJtv). cartoTaco's concierge can't answer "what is chavindeca?" (issue #92). About 70 food descriptions sit in cartoTaco's `item_spec` / `protein_spec` / `salsa_spec` / `salsa_styles`, but they have no sources, contain errors ("Hailing from pueblo"), and `get_spot` passes only specialty names to the model. The fix is a separate product, **cocina**: sourced claims about food concepts, published only by a human, read by consumers as published-only cards. Phase 1 ends when "what is chavindeca?" is answered in cartoTaco staging with a credited source, and a `dish` eval category passes against a pinned snapshot.

Decisions made with the owner: new repo `cocina`; a **Python** pipeline with a **JS/SvelteKit** app; **one** new Supabase project (local `supabase start` for testing migrations); a minimal **research CLI + terminal review** moves into phase 1.

## Departures from the spec

Each one is deliberate and has a stated upgrade path.

1. **No `edge` table. An edge is a claim with an object entity.** `claim.object_id → entity` (e.g. `variant_of → sincronizada`), so links need evidence like any other fact. `related()` is a recursive CTE over published claims where `object_id` is set.
2. **`claim.value` is `text`, not jsonb: one fact per row.** "main ingredients: pork, orange" becomes two `uses` claims. Claims compare by `norm(value)`.
3. **Drafts live in the real tables, not in a proposal payload.** `entity`, `claim`, `alias` and `evidence` each carry `status` (`proposed | published | rejected | retired`) and `proposal_id`. `proposal` is a thin header (op, author, rationale, decision). FKs, grouping and dedup work on drafts, and RLS is simply `status = 'published'`.
4. **`confidence` and `last_verified_at` are derived, not stored.** `cards()` computes them as the count of distinct source domains and the max `evidence.fetched_at`. Add tier weighting when sources vary in quality.
5. **Name matching lives in Postgres.** One SQL definition (`norm()` = lower + unaccent, plus `fuzzystrmatch` levenshtein with search.js's thresholds: 1 typo from 4 letters, 2 from 7). The Python pipeline and the MCP server both call it. cartoTaco keeps its own `normalize`/`editDistance` from `src/lib/search.js`. A shared `names.golden.json` (query → slug) is tested on both sides, which is the spec's "at least one test set".
6. **cartoTaco reads all published cards into memory** (`cards()` RPC, cached 5 min, exactly like `getCatalog`), and evals read the snapshot JSONL in the same shape. This settles the spec's "MCP adapters vs Postgres, measure latency" question: there's no per-query round trip. The MCP server still ships, for Claude Code and future agents.
7. **Deferred within phase 1:** Wikidata import and `writeup` generation go to phase 2 (no phase 1 done-criterion needs them; the concierge explains from claims). `kb_slug` columns on cartoTaco's spec tables also go to phase 2, with the spec sync that needs them. Salsas seeded from `salsa_styles` reuse cartoTaco's slug, and dish lookups match by alias name.
8. **Seed text is a research hint, not draft claims.** Old descriptions go into the private `entity.note` column. The research run reads them as "may contain errors, verify". That keeps one claim path (from fetched pages) instead of two.

## Phase 1, in detail

### 1. Repo and database (`cocina`, new GitHub repo under keatonwilson)

```
cocina/
  supabase/migrations/<ts>_001_core.sql
  supabase/tests/citations.test.sql      # pgTAP via `supabase test db`
  pipeline/                              # Python 3.12, requirements.txt: anthropic, supabase, pydantic, python-dotenv
    cocina/{db,fetch,research,seed,review,snapshot}.py
    tests/test_research.py
  app/                                   # SvelteKit on Vercel: /mcp now, review inbox in phase 2
  names.golden.json
  .github/workflows/{migrate,ci}.yml     # migrate.yml copied from cartoTaco (main → prod only)
  CLAUDE.md
```

**Migration `001_core.sql`:**
- Extensions: `unaccent`, `fuzzystrmatch`. `norm(text)` is an IMMUTABLE wrapper around `lower(unaccent('unaccent', x))`.
- `creator(id, name, role, url, bio, do_not_use)`.
- `source(id, url unique, title, publisher, work_type, published_on, license, content_hash, fetched_at, isbn, pages, citation_text)`, plus `source_creator(source_id, creator_id)`.
- `source_text(source_id, text)`: the copy quotes are checked against. No anon access.
- `entity(id, slug unique, kind, name, domain default 'mexican', note, status, proposal_id)`. Kinds are a CHECK on the v1 list: dish, menu_category, protein_prep, cut, salsa, chile, ingredient, technique, tortilla, region.
- `predicate(kind, name, object_kind)`, seeded from the spec's per-kind lists. When `object_kind` is set, the claim links to an entity.
- `claim(id, entity_id, predicate, value text, object_id, supersedes, status, proposal_id)`.
- `evidence(id, claim_id, source_id, quote, locator, fetched_at, status, proposal_id)` with `CHECK (length(quote) BETWEEN 1 AND 400)`, so we quote little.
- `alias(id, entity_id, text, lang, claim_id null, status, proposal_id)`. `claim_id` is set when the alias is a sourced name (an `also_known_as` claim) and null for misspellings, which are only search hints.
- `proposal(id, op add|change|retire|merge, author, rationale, status open|accepted|rejected, decided_by, decided_at, reason, run_id, created_at)`.
- `run_log(id, agent, started_at, finished_at, cost_usd, proposals, errors jsonb)`.

**Citation rules enforced by the database**, as `DEFERRABLE INITIALLY DEFERRED` constraint triggers so a whole proposal is checked at commit:
- A published claim needs at least one published evidence row whose source has a creator or a publisher, and no `do_not_use` creator.
- A published entity needs at least one published claim.
- A published claim's object entity must be published.
- An alias with a `claim_id` publishes only alongside that claim.

**Functions:**
- `accept_proposal(id, reviewer)`: publishes the proposal's rows, plus the entity if it's still proposed; for `change` it retires the claim named in `supersedes`, and for `retire` it retires the target.
- `reject_proposal(id, reviewer, reason)`.
- `search_entities(q, kind)`.
- `entity_card(slug)`: name, kind, aliases, claims with sources (authors, work, publisher, url, quote, locator), related names, derived confidence and verified dates.
- `related(slug, relation, hops ≤ 2)`.
- `cards()`: every published card.
- `kb_version()`: the max `decided_at` of accepted proposals.

RLS: anon reads published rows, plus `source` and `creator`. `proposal`, `run_log` and `source_text` are service-role only.

### 2. Pipeline (Python; reuses cartoTacoMenuExtract patterns)
- **`seed.py`** reads cartoTaco prod with the anon key (the spec tables have public-read RLS), as read-only, and writes proposed entities: `item_spec` → dish, `protein_spec` → protein_prep, `salsa_spec` + published `salsa_styles` → salsa (salsa_styles slugs reused, deduped through `salsa_styles.spec_id`), and a hard-coded list of about 18 menu categories (taco, sope, gordita, molote…). Each entity gets an alias for the spec name, plus `note` = the old origin/short/long/history text.
- **`fetch.py`** uses the stdlib only: `urllib.robotparser` (UA `cocina-research/0.1 (+repo url)`), `urllib.request`, and an `html.parser` text extractor that skips script/style/nav/header/footer/aside. It skips pages with a `noai` robots meta tag, computes a sha256 `content_hash`, and upserts `source` + `source_text`. `# ponytail: crude main-text extraction, swap in trafilatura if pages come back noisy`.
- **`research.py <slug>`** runs one entity end to end:
  1. **Search.** `claude-sonnet-5-5` with `web_search_20260209` picks 3–6 URLs (structured output). If the sandbox errors the way `description_gen.py:_web_search_tools` documents, fall back to `web_search_20250305`; if structured output can't combine with the server tool, read the URLs from the `web_search_tool_result` blocks.
  2. **Fetch** each page with `fetch.py`.
  3. **Extract.** `client.messages.parse` with a pydantic schema: per-page metadata (title, authors, publisher, date) and claims (predicate drawn from the DB's list for this kind, value, optional object name, exact quote, url). The prompt includes the entity's `note` as an unverified hint. Long pages are capped and the cap is logged, never silently truncated.
  4. **Quote check.** Drop any claim whose whitespace- and quote-mark-normalised quote isn't in the page text, and log the count.
  5. **Match and group.** Resolve object names through `search_entities`, linking only on exactly one match (the same rule as `resolve_spec_id`). Group by `(predicate, norm(value))` into one claim with N evidence rows. A match to an existing published claim adds evidence only.
  6. **Write** one proposal per claim, plus its evidence and any `also_known_as` alias, and a `run_log` row (token cost + $10 per 1k searches).

  Add `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) to the Sonnet 5.5 calls, as the API guidance recommends. Port the prompt style from `generate_spec_descriptions`.
- **`review.py`** walks open proposals (`--entity`, `--kind` filters). It prints the claim, every quote with its source credit and URL, and the rationale. Keys: `a` accept, `r` reject (asks for a reason), `s` skip, `o` open the URL (`webbrowser`), `q` quit. Calls `accept_proposal` / `reject_proposal`.
- **`snapshot.py`** calls `cards()`, writes `kb-YYYY.MM.DD.jsonl` (one card per line), and runs `gh release create` with counts in the notes.

### 3. MCP server (`app/src/routes/mcp/+server.js`)
- Stateless Streamable HTTP via `@modelcontextprotocol/sdk`. If its transport wants Node req/res, use a plain Vercel Node function instead.
- Four tools, each a thin call to an RPC above: `search(query, kind?)`, `get_entity(slug)`, `related(slug, relation?, hops?)`, `sources(slug)`. Every response includes `kb_version`.
- Read-only by construction (anon key, published-only RLS). A bearer `MCP_TOKEN` check stays until the "public or not" decision is made.

### 4. Research and review the eval entities
- Run `research.py` for chavindeca, gringa, salsa macha and their neighbours (sincronizada, etc.), then as many of the ~70 seed entities as time allows (about $0.05–0.20 each). Review in the terminal.
- Cut the first snapshot release.

### 5. cartoTaco PR (branch off `staging`, following its CLAUDE.md conventions; no migration)
- `src/lib/server/kb.js`: `getKb(supabaseKb)` → `cards()` + `kb_version()`, cached 5 min. Env `KB_URL` / `KB_ANON_KEY` go in `.env.example` and `docs/DEPLOYMENT.md`.
- `tools.js`: add `describe_dish({ name })`, matched with `normalize` / `editDistance` from `src/lib/search.js` over card names and aliases.
  - **Returns:** facts, credited sources, other names, and `brief()` rows for spots whose specialties, menu or protein styles match the entity's names.
  - **Unknown name:** returns `found: false` plus spots whose menus match, with no invented answer. This keeps the spec's recommended strict line.
  - **Credit:** records a `sources` action, so credit comes from data, not model prose.
- Router: new intent `dish_info` in `INTENTS` (`prompt.js`) and `INTENT_TOOLS.dish_info = ['describe_dish','search_spots','show_spot']`. Add the prompt rule from #92 and bump `PROMPT_VERSION`.
- `runConcierge` takes `kb` alongside `catalog`. `/api/agent` loads it, and the trace metadata gains `kb_version`.
- `ChatPanel.svelte` / `agentStore.js` render the `sources` action as a "Sources:" line (author names linked).
- Evals:
  - `evals/fixtures/kb-<tag>.jsonl`, pinned and fetched by `pnpm eval:kb-fixture` (a `gh release download` one-liner).
  - A golden `dish` category with the five #92 examples: chavindeca must cite spots 10 and 16; the staging follow-up with `history`; "¿qué es una gringa?"; salsa macha; and a trap.
  - A deterministic check that dish answers carry a `sources` action.
  - `concierge.test.js` covers routing with the fake models.
- Docs: `CLAUDE.md` and a "Knowledgebase (#92)" section in `docs/AI_CONCIERGE_PLAN.md`.

## Phases 2–4 (milestones)

- **Phase 2, Curation.**
  - SvelteKit review inbox and entity page in `app/` (Supabase auth), with the same `accept_proposal` underneath, batch accept for 2+ independent sources, and creator pages.
  - Citation checker as a pipeline step: quote present, plus the overlap threshold (8 words / 15%; still to decide).
  - Wikidata import (CC0 labels, aliases, subclass claims).
  - `writeup` stored as sentences `[{text, claim_ids}]`, so "every sentence cites" becomes a DB check.
  - Re-research every seed entity.
  - `kb_slug` on cartoTaco's spec tables, and the nightly spec sync.
  - Done when every seed claim has a credited source and the sync is live.
- **Phase 3, Long form and autonomy.**
  - `article` / `passage` tables (per-sentence citations with locators), briefs, agent drafts edited by a human, pgvector passage retrieval.
  - Gap filler fed by concierge misses (`found: false` + 👎), then the auditor (`content_hash` re-fetch) and the linker.
  - ~~Auto-accept policy once acceptance rates exist.~~ Moved into phase 1 (2026-10-25, the owner's call after reviewing the first entity by hand): `review --auto` publishes proposals Claude finds supported and uncontested; conflicts, unsupported quotes and anything restating a rejected fact wait for a human. Articles stay human-reviewed. Writeups (short descriptions built only from published claims, every sentence citing them) are auto-reviewed against those claims, the owner's call on 2026-10-27; a flagged one waits for a human.
  - Still open, from a comment on the design doc: may agents find article sources themselves, or only use the ones the owner picks?
  - Done when 5 articles are published and a miss becomes an entity in under a week.
- **Phase 4, Reach.** Hybrid FTS + embedding search, a cartoTaco glossary built from a snapshot, other consumers and domains.

## Verification

- **DB:** `supabase db reset && supabase test db`. `citations.test.sql` asserts that publishing a claim without evidence fails, that a 401-character quote fails, that `accept_proposal` publishes entity + claim + alias together, that a `do_not_use` creator blocks publication, and that anon can't see proposed rows or `source_text`.
- **Pipeline:**
  - `pytest pipeline/tests`: quote check drops an invented quote, grouping merges three agreeing sources, `names.golden.json` resolves through `search_entities` against local Supabase.
  - `python -m cocina.research chavindeca` against the real API, then `python -m cocina.review` to accept, then `cards()` shows the claim with author and publisher.
- **MCP:** `claude mcp add --transport http cocina <url>/mcp --header "Authorization: Bearer …"`, then ask Claude Code "what is chavindeca?" and check that the result carries sources and `kb_version`.
- **cartoTaco:**
  - `pnpm check && pnpm test && pnpm eval --category dish --judge` against the pinned snapshot.
  - On staging (after the PR lands on `staging`), ask "What is chavindeca?" in the chat panel: the reply explains it, names spots 10 and 16, and shows a Sources line. The LangSmith trace carries `kb_version`.
- **Last step:** record the decisions (name, Supabase setup) in the design doc's open decisions.
