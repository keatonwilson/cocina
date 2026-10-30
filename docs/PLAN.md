# Plan: cocina, a culinary knowledgebase (phase 1 as planned, phase 2 as built, phase 3 next)

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

## Status (2026-10-30)

- **Phase 1: done (2026-10-27).** cartoTaco staging answers "what is chavindeca?" from cocina with spots and a Sources line (cartoTaco #93).
- **Phase 2: done (2026-10-30).** See below.
- **Phase 3: next.** It starts from the section further down.

The live knowledgebase (snapshot `kb-2026.10.28.2`) holds 76 foods, 1271 claims and 71 writeups. About 100 flagged proposals wait in the inbox: source conflicts and judgement calls.

## Phase 2, as built: curation and coverage

- **Coverage.** Every seeded entity was researched (82 runs, about $14), and auto-review published about 1200 proposals. Six names turned up nothing to propose; they look like individual spots' names for their own specials: caramelo, el-pueblito-burrito, razurados, taco-hass, tacontento and toritos. They stay proposed and out of every read.
- **Writeups** (migration 003, `write.py`):
  - Each is 2–4 sentences made only from published claims, and every sentence cites the claims it states (`sentences_ok`).
  - Code refuses a sentence that copies 8 or more consecutive words of a quote. The 15% part of the overlap threshold was dropped.
  - Writeups are auto-reviewed against the claims they cite. Articles stay human-only.
- **Auto-review** (`review --auto`), extended to:
  - Reject clear-cut "unsupported" proposals: anything not `borderline`.
  - Flag conflicts and judgement calls for a person.
  - Reject in code a restated fact that a person rejected or retired, and a "kind of taco" alias on a menu category.
  - Flag an alias that another entity uses (`name_clash`). Only a person's rejection blocks a fact from returning.
  - `--recheck` re-judges the flagged backlog: it can reject, never accept.
- **Fast reads** (migration 004): `cards_of()` builds every card in one set-based pass. The old per-entity `card()` exceeded anon's 3-second statement timeout at about 1200 claims.
- **Retire** (migration 005, `retire.py`):
  - A person takes published claims down. An entity left with no published claims goes too, with its names and writeup, and its open proposals are rejected. Nothing publishes onto a retired entity.
  - `--into` passes a duplicate's name to the entry that stays. It was used for gringas → gringa and sonoran-hot-dog → sonoran-dog.
- **Wikidata names** (`wikidata.py`):
  - The one approved robots exception: Wikidata's Action API, recorded in CLAUDE.md.
  - A match needs exactly one food item with the exact name. Its English and Spanish labels and aliases become `also_known_as` claims credited to Wikidata (CC0).
- **Web inbox and MCP deploy:**
  - `app/` is on Vercel at https://cocina-tau-six.vercel.app, with the inbox at `/review` and the MCP server at `/mcp`.
  - Sign-in: Supabase magic links for addresses on the `REVIEWERS` list, via `/auth/confirm` (a token-hash link behind a button, so a mail app's link preview can't use it up).
  - The app runs Svelte 5 (SvelteKit 2.70 needs it), and CI builds it and smoke-tests the pages.
- **cartoTaco** (#96, #97):
  - Spot cards show a specialty's writeup and Sources when its name exactly matches a cocina entry, served from `/api/kb`. Otherwise the old text stays.
  - The Sources list is one work per line (#94).
  - The evals pin `kb-2026.10.28.2`.
- **Changed from the plan:**
  - No `kb_slug` migration and no nightly sync: cartoTaco matches by name at runtime.
  - No batch accept: auto-review covers it.
  - Creator and entity pages deferred.

## Phase 3: long form and autonomy (next)

**Goal:** cocina grows from what people ask, keeps its sources honest, and adds long-form pieces.

**Done when:** a concierge miss becomes a researched, published entity within a week without a person starting it, and 5 articles are published.

Suggested order, each its own PR series:

1. **Gap filler.** Turn the concierge's misses into entities.
   - **The signal:** in cartoTaco, `describe_dish` returns `found: false` for a food it doesn't know, and replies carry 👍/👎 feedback.
   - **Find out first** where cartoTaco records these. LangSmith traces carry `kb_version` and the tool results; check whether there's a feedback table.
   - **Then, on a schedule:** gather new misses, dedupe them through `search_entities`, then run `seed --add` → `research` → `review --auto`.
   - **Where it runs:** a GitHub Actions cron (it needs `ANTHROPIC_API_KEY` and `COCINA_SUPABASE_SERVICE_KEY` as repo secrets) or Vercel cron. Either way, with a per-run cost cap. Research costs about $0.17 per entity.
   - **Skip** names that aren't foods: an entity research finds nothing for is skipped and listed, like the six above.
2. **Auditor.** Keep sources honest.
   - **Re-fetch** cited pages politely (robots and `noai` as usual). Compare the text hash `content_hash`, and re-run `quote_found` for each published quote.
   - **When a quote is gone,** a page is dead or a new `noai` appears, propose a retire with the reason.
   - **Needed first:** `retire` proposals have no inbox UI yet; that comes before automatic retire proposals. Or let them auto-accept when a claim has other credited evidence left.
   - **Record when each source was last checked,** and keep `verified_at` on cards meaningful.
3. **Articles.** Long-form history and technique, with a citation on every sentence.
   - **Tables:** `article` and `passage`, with per-sentence citations to claims or source locators (a page or a timestamp).
   - **The flow:** an article brief (the owner picks the topic and sources; books and interviews can be entered by hand), an agent draft, then the owner edits and publishes. This is the one human-only publish path.
   - **Retrieval:** pgvector over passages for retrieval.
   - **Copying:** reuse the 8-word copy check.
   - **Open question:** may agents find article sources themselves, or only use the ones the owner picks?
4. **Linker and merge.** `variant_of` and other links between entries, a real `merge` op that moves claims rather than retiring the duplicate's (`retire --into` does that today), and links surfaced in cards (`linked_from` exists).

**Carried over:**
- **Issue #1:** set the hosted Supabase auth settings (Site URL, redirect URLs, email templates) from the repo. The owner set them by hand on 2026-10-30.
- **Sign-in email:** custom SMTP; the built-in sender allows only a few emails an hour.
- **Decisions still open:** whether the MCP server and data go public, and under what license (the README says none is chosen); and who reviews besides the owner.
- **Scale:** `cards()` takes about 0.8 s on the server for 76 entities. Past a few hundred entities, page it, or let `getKb()` read the snapshot.
- **Flag backlog:** in the web inbox.

## Phase 4 (milestone)

**Reach.** Hybrid full-text and embedding search, a cartoTaco glossary built from a snapshot, other consumers and domains.

## Verification (phase 1)

- **DB:** `supabase db reset && supabase test db`. `citations.test.sql` asserts that publishing a claim without evidence fails, that a 401-character quote fails, that `accept_proposal` publishes entity + claim + alias together, that a `do_not_use` creator blocks publication, and that anon can't see proposed rows or `source_text`.
- **Pipeline:**
  - `pytest pipeline/tests`: quote check drops an invented quote, grouping merges three agreeing sources, `names.golden.json` resolves through `search_entities` against local Supabase.
  - `python -m cocina.research chavindeca` against the real API, then `python -m cocina.review` to accept, then `cards()` shows the claim with author and publisher.
- **MCP:** `claude mcp add --transport http cocina <url>/mcp --header "Authorization: Bearer …"`, then ask Claude Code "what is chavindeca?" and check that the result carries sources and `kb_version`.
- **cartoTaco:**
  - `pnpm check && pnpm test && pnpm eval --category dish --judge` against the pinned snapshot.
  - On staging (after the PR lands on `staging`), ask "What is chavindeca?" in the chat panel: the reply explains it, names spots 10 and 16, and shows a Sources line. The LangSmith trace carries `kb_version`.
- **Last step:** record the decisions (name, Supabase setup) in the design doc's open decisions.
