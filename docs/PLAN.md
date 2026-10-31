# Plan: cocina, a culinary knowledgebase (phase 1 as planned, phase 2 as built, phase 3 under way)

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

## Status (2026-10-31)

- **Phase 1: done (2026-10-27).** cartoTaco staging answers "what is chavindeca?" from cocina with spots and a Sources line (cartoTaco #93).
- **Phase 2: done (2026-10-30).** See below.
- **Phase 3: under way.** Step 1, the gap filler, is live on staging. It researched and published a miss asked after the previous run (`memela`, 2026-10-31), but that run was started by hand: the first scheduled run never fired (below). Step 3, articles, comes next, ahead of the auditor: the done-criterion needs 5 published articles, not the auditor, and articles take the owner's time. The owner approved its plan (below) on 2026-10-30. It's merged (cocina #3, 2026-10-31) and live: migration 006 is applied and the MCP `articles` tool answers in production. Briefs for the five suggested topics are in the git-ignored `briefs/`, waiting for the owner's books. Step 2, the auditor, was planned, approved, merged (cocina #4) and first run on 2026-10-31. A fix for a false alarm in that run waits in cocina #5.

The live knowledgebase holds 78 foods, 1318 claims and 73 writeups (2026-10-31) (the last snapshot, `kb-2026.10.28.2`, had 76, 1271 and 71; the evals pin it). 112 flagged proposals wait in the inbox: source conflicts and judgement calls.

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

## Phase 3: long form and autonomy (under way)

**Goal:** cocina grows from what people ask, keeps its sources honest, and adds long-form pieces.

**Done when:** a concierge miss becomes a researched, published entity within a week without a person starting it, and 5 articles are published.

Suggested order, each its own PR series:

1. **Gap filler** (live 2026-10-30: `gaps.py`, cocina #2; cartoTaco #98 on `staging`). Turn the concierge's misses into entities.
   - **First run**, dispatched by hand on 2026-10-30 at 17:13 Tucson time: one miss, `tlayuda`. It was researched, auto-review published 25 claims and a writeup, and it reached cartoTaco's `cards` read. About a minute and $0.22.
   - **The first scheduled run, 2026-10-31, never fired.**
     - The 06:23 Tucson slot (13:23 UTC) was the first one after `gaps.yml` reached `main` (2026-10-30, 17:07 Tucson). By 08:05 no run had been queued, and the repo has never had a `schedule` run.
     - Nothing in the repo was wrong: the cron is valid, the file is on the default branch, the workflow is active, Actions is enabled, and the repo is public and not archived. A dispatched run worked.
     - GitHub listed no incident that day; its last Actions incident was 2026-10-30, 17:50–19:23 UTC, before the workflow existed.
     - GitHub documents `schedule` as best effort: delayed under load, and dropped when load is high enough. A slot with no run two hours later was dropped, not delayed. GitHub keeps no scheduler log to say why.
   - **Started by hand instead,** 2026-10-31 at 08:24 Tucson: two misses since the previous run.
     - `memela`, asked on staging after the previous run, was researched. Auto-review published 19 claims, flagged 4 for a person (two conflicts over whether it's a sope, an origin conflict, and one unsupported ingredient) and published a writeup. About a minute and $0.22.
     - `tlayuda` came back as an `alias_gap`, most likely the same miss re-read through the 10-minute overlap, since it was asked just before the previous run.
     - That proves a miss asked on staging becomes a published entity. It doesn't yet prove "without a person starting it".
   - **Still to see:** a scheduled run that fires on its own. Check the 2026-11-01 slot with `gh run list --repo keatonwilson/cocina --workflow gaps.yml --event schedule`. A dropped day costs nothing, because each run reads every miss since the last finished one. If slots keep dropping, a scheduler outside Actions should call the dispatch API (a Vercel cron, say).
   - **Staging only, by choice:** cartoTaco's concierge isn't on `main`, so `cartotaco-production` has no traces. The gap filler reads it anyway and needs no change when the concierge ships.
   - **What cartoTaco recorded:**
     - 👍/👎 goes only to LangSmith, as feedback `user_score` on the root `concierge` run. There's no table.
     - Misses weren't recorded anywhere you could query. Tool calls aren't traced as runs, so `found: false` only showed up inside the next LLM call's input.
   - **Why feedback and not run metadata:** on staging, 4 of the last 10 traces never received their final update. They stay `pending`, without outputs or end-of-run metadata, though the user got a reply. 👎 feedback on those same runs landed.
     - So `/api/agent` sends each missed name as feedback `kb_miss` (value: the name), an awaited call made only on a miss.
     - The lost final updates are a cartoTaco tracing bug still to fix. Local traces all finish.
   - **The run:** `.github/workflows/gaps.yml`, daily at 06:23 Tucson time, needs `ANTHROPIC_API_KEY`, `COCINA_SUPABASE_SERVICE_KEY` and `LANGSMITH_API_KEY` as secrets and `COCINA_SUPABASE_URL` as a variable.
     - **Read:** traces in `cartotaco-production` and `cartotaco-staging` with `kb_miss` feedback since the last finished run started. Each miss is weighted by 👎.
     - **Dedupe:** `search_entities(p_all)`. A published hit is listed as an `alias_gap` (cartoTaco's matching missed a known entry). A proposed hit was tried before or is waiting for a person.
     - **Food check:** one Claude call for the whole batch. It drops non-foods and other cuisines, fixes misspellings, and gives a kind and the usual name, which is deduped again.
     - **Fill:** `add_entity` → `research` → `review --auto` → `write` → `review --auto`, capped at 5 entities and $2 a run. What the cap leaves goes first next run.
     - An entity research finds nothing for stays proposed and isn't tried again, like the six above.
   - **The repo is public:** the Actions log shows counts and added slugs only, and the `run_log` row (agent `gaps`) has the names and reasons.
   - **Not done:**
     - `alias_gap`s are listed, not fixed (a search hint would fix them).
     - A 👎 on an answer about a known entry isn't read: it would need the lookup recorded on the trace too.
2. **Auditor** (plan approved 2026-10-31; merged in cocina #4 and live on 2026-10-31; a fix waits in cocina #5). Keep sources honest: re-fetch every cited page each week, re-check every published quote, and take down what's gone.
   - **The owner's decisions (2026-10-31):**
     - **Gone quotes:** a quote is gone when its page is dead, the quote is no longer on it, or the page now says `noai`.
       - When its claim still has other live evidence, the evidence row is retired automatically. The claim stays published.
       - A claim that loses its last quote becomes a retire proposal, and a person decides it.
     - **Rewording:** a live page whose quoted sentence changed counts as a gone quote for now. Each run counts these separately from dead pages, so it's clear whether re-quoting is worth adding.
     - **Review:** in the terminal (`python -m cocina.review`), not the web inbox.
     - **Opt-outs:** a page that now says `noai` has opted out, so its quotes are gone. A robots.txt that now blocks us only means we can't check: its quotes stay, and the run counts them.
   - **Lighter than the design doc, on purpose:**
     - **No model calls.** It re-fetches, compares `content_hash` and re-runs `quote_found`, so a run costs $0.
     - **No hunt for disagreeing sources.** Auto-review already flags a conflict when a claim is proposed.
     - **No per-claim re-check dates.** Every cited page is re-fetched weekly, so a card's `verified_at` is when its quote was last seen.
     - **No archive.org copies** of dead pages. That would be a new fetch exception, like Wikidata's. Add it if dead pages turn out to take down claims nothing else backs.
     - **No "flags on articles whose claims changed".** Articles cite sources, not claims, so the auditor checks their quotes the same way it checks evidence.
     - **Not re-fetched:** Wikidata's 10 dataset sources (CC0 names) and sources entered by hand (books, interviews).
   - **Migration `007_audit`:**
     - **`source`** gains `checked_at`, `failures` (consecutive failed checks, default 0) and `check_error`.
     - **`proposal.retires_evidence bigint[]`:** the evidence rows a `retire` takes down. A retire must now name claims, evidence rows or both.
     - **`accept_proposal`:**
       - A retire also retires its `retires_evidence` rows.
       - The existing rule that a published claim needs published, credited evidence refuses an evidence retire that would leave a claim bare. The auditor has to put such a claim in `retires` instead.
       - It refuses a claim retire when the reviewer is `auto-review` or `audit`, so only a person takes a claim down. An evidence-only retire may be accepted by `audit`.
     - **pgTAP:**
       - An evidence-only retire, accepted by `audit`, leaves its claim published.
       - One that strips a claim's last evidence fails.
       - `audit` can't accept a claim retire.
       - A rejected retire changes nothing.
   - **`python -m cocina.audit [--dry-run]`** (`pipeline/cocina/audit.py`):
     1. **Collects** the web sources cited by published evidence or published articles, with each one's quotes (evidence id and claim, or article slug).
     2. **Fetches** each with `fetch_page`, one page at a time (robots and `noai` as usual):
        - **Same `content_hash`:** every quote is still there.
        - **New hash:** `quote_found` runs on each quote against the whole new text, not cut to 30k.
          - If every quote is found, the new text (cut to 30k), hash and `fetched_at` replace the stored ones.
          - If any is gone, the old text stays, as the record of what the quote was checked against.
        - **`noai`:** every quote from the page is gone.
        - **robots.txt block:** unchecked. Nothing changes except `check_error`.
        - **Any other failure** (an HTTP error, unreachable, not a web page): `failures` goes up by one. At 3 failed weekly checks in a row the page counts as dead, and its quotes are gone. A site down for a day doesn't lose anything.
     3. **Records** each quote it found: the evidence row's `fetched_at` becomes now, which is what cards show as `verified_at` (no change to the reads), and the source gets `checked_at` and `failures = 0`.
     4. **Acts on gone quotes:**
        - **Still backed:** gone evidence on claims that keep other live evidence goes into one proposal (op `retire`, author `audit`, `retires_evidence`), which `audit` accepts at once.
        - **Last quote gone:** each such claim gets its own retire proposal (author `audit`, with the reason and URL in its rationale), so a person can keep one and retire another.
        - **Not proposed again:** a claim that already has an open audit proposal, or one a person kept.
        - **Article quotes are never retired.** An article changes only through a version a person edits, so the run lists them instead.
     5. **Logs a `run_log` row** (agent `audit`) with the URLs and reasons. Stdout carries counts only, because the Actions log is public:
        - pages checked, unchanged and changed;
        - quotes gone, split into dead, dropped from a live page, and `noai`;
        - pages unchecked and pages failing;
        - evidence retired, and claims proposed.
     - **`--dry-run`** fetches and prints the counts, but writes nothing.
   - **Review:**
     - **`python -m cocina.review`** shows the auditor's claim retires in their own section. Each one shows:
       - the entity and claim;
       - the gone quote, with its source and the reason;
       - the choices: retire (accepted as the person) or keep (rejected; it isn't proposed again).
     - The section ends with the latest run's gone article quotes, each as "`article edit <slug>`: N quotes gone from <source>".
     - **Other tools:**
       - **Auto-review** skips retire proposals. `open_proposals` filters them, as it does articles.
       - **The web inbox** shows only flagged proposals, so audit proposals don't appear there.
     - **When a person retires a claim,** a writeup citing it stops being served until `write --all`, as with `retire` today.
   - **The run:** `.github/workflows/audit.yml`, weekly on Sundays at 07:47 Tucson time.
     - It needs `COCINA_SUPABASE_SERVICE_KEY` as a secret and `COCINA_SUPABASE_URL` as a variable. There's no Anthropic key.
     - About 306 pages on 185 sites, so a few minutes of Actions time.
   - **Test:**
     - `tests/test_audit.py`, with no network. A fake fetch covers each case: unchanged, changed with the quote, changed without it, `noai`, a robots block, and an HTTP error three times.
       - Each case must lead to the right outcome: verified, gone (retired when backed, proposed when it's the last quote), unchecked, failing, or dead.
     - Plus the pgTAP tests above.
   - **First run:**
     1. `--dry-run` locally against the hosted database before the workflow lands.
     2. One dispatched run.
     3. Then the weekly schedule.
   - **As built (cocina #4), where it differs from the above:**
     - **`fetch.py`:** an unreachable robots.txt, or one returning 5xx, read as a robots block, so a dead site would never have died. `Skipped` now says "robots.txt can't be read (…)", which counts as a failure.
     - **A 401/403 on the page** counts as refusing us, like robots.txt: a bot block, not a dead page.
     - **Auto retires:** one proposal per claim, so a refusal only affects that claim; a refused one is rejected rather than left open.
     - **A person's claim retire** also carries the claim's gone quotes, so they're retired with it.
     - **`accept_proposal`** also refuses a claim retire signed `draft`, and an article signed `audit`.
     - **The article notes** print at the start of the review walk.
     - **Not audited:** quotes on retired claims (`retire` leaves them published), and pages from creators who opted out (their quotes aren't served, and `evidence_ok` refuses to touch them).
     - **Dead after 3 checks, not 3 weeks.** A hand-run audit between the weekly ones brings death sooner (marked `ponytail:`).
   - **Hosted dry run, 2026-10-31** (writes nothing; six days after the first research):
     - 308 pages: 239 unchanged, 64 changed with every quote still there, 1 missing quotes, 1 failing (a timeout) and 3 refusing us (403).
     - The page missing quotes had been rewritten by its site on 2026-10-29. None of its 6 quotes are on it now; 4 would be retired automatically, and 2 claims would wait for a person.
   - **First run** (dispatched 2026-10-31, 09:07 Tucson; 11 minutes, $0):
     - 308 pages: 229 unchanged, 58 changed with every quote still there, 3 missing quotes, and 18 refusing GitHub's runners (403). Only 3 refused a home connection in the dry run, so about 6% of pages can't be checked from Actions; their `verified_at` won't move. If that matters, run the audit from somewhere else.
     - **One real rewrite** (a blog, rewritten 2026-10-29). Its 4 quotes on claims with other quotes were retired automatically. 2 claims wait for a person.
     - **Two false alarms:** chron.com and houstonchronicle.com served the runner another page with 200 OK. From a home connection every quote is still on both, but 13 claims were proposed for retirement. Nothing published changed, because those claims waited for a person.
       - The 13 proposals were closed as `audit`, with the reason.
       - Cocina #5 leaves a page unchecked when it keeps under a quarter of its stored words (measured: a real rewrite kept 37%, other pages 1–19%). Proposals the auditor closes itself no longer count as kept by a person.
   - **After four weekly runs:** count the quotes dropped from live pages, and the claims that waited for a person. More than a handful a week means adding re-quoting: one small Claude call finds the new wording on the same page, and it's kept when `quote_found` passes and auto-review's check agrees.
   - **Not done:**
     - re-quoting reworded pages (decided after four runs, above);
     - archive.org copies;
     - disagreeing sources;
     - Wikidata and hand-entered sources;
     - a web inbox view.
3. **Articles** (plan approved 2026-10-30; merged in cocina #3 and live on 2026-10-31). Long-form history and technique, with a citation on every sentence. The flow: the owner writes a brief, an agent drafts, and the owner edits and publishes in the terminal. This is the one publish path only a person can take.
   - **The owner's decisions (2026-10-30):**
     - **Sources:** the owner picks them in a brief, and the drafter may add up to 4 pages it finds itself. Those are marked, and publish counts the citations that rest on them. This replaces the design doc's "human-picked sources only".
     - **Editing and publishing:** in the terminal, not the web inbox.
     - **Citations:** a sentence cites places in sources, each a source, a quote and a locator (a book page, an interview timestamp; none for a web page). It doesn't cite claims, because history from books and interviews doesn't break down into entity/predicate facts.
     - **Readers:** the MCP server only, for now.
   - **Lighter than the design doc, on purpose:**
     - **No pgvector and no `passage` table.** The drafter reads whole sources in one prompt. Stored web texts are capped at 30k characters (median 8k), so ten sources stay under about 75k tokens. Five articles don't need retrieval for readers either. Add passages and embeddings when a brief outgrows one prompt or articles reach the dozens.
     - **No `citation` table.** Citations live in the article's jsonb, as a writeup's do.
     - **No `kind`, `authored_by` or `version` columns.** The drafter is `proposal.author`, the person who published it is `decided_by`, and earlier versions are the slug's retired rows.
     - **Not linked yet:** articles aren't linked to entities, and they aren't in cards, snapshots or cartoTaco until a reader needs them.
   - **Migration `006_articles`:**
     - `article(slug, title, sections jsonb, sources bigint[], found bigint[], status, proposal_id)`.
       - `sections` = `[{heading, paragraphs: [[{text, cites: [{source, quote, locator}]}]]}]`.
       - `sources` holds the owner's picks and `found` the drafter's.
     - One published article per slug. Accepting a new version retires the old one, as with writeups.
     - **`article_ok`** (deferred trigger on published rows): every sentence has text and at least one cite, and every cite names a credited source (`credited()`) with a 1–400 character quote. RLS serves an article only while `article_ok` holds, so a creator who opts out hides it, as with a writeup.
     - **`accept_proposal`** publishes articles. It refuses when the reviewer is `auto-review` or `draft` (the drafter), which puts the human-only rule in the database. That isn't "the proposal's own author", because a new version made in `edit` is authored by the person who then publishes it. `reject_proposal` learns articles too.
     - **Reads:** `get_article(slug)` returns the article with each cite's credit (authors, title, publisher, year, URL, locator, quote). `articles()` lists slug, title and sentence count.
     - **pgTAP:** an uncited sentence, a source with no credit and an `auto-review` accept all fail; a second version retires the first; anon can't see a proposed article.
   - **`python -m cocina.article`** (terminal):
     - **The brief** is a TOML file in a git-ignored `briefs/`, since book excerpts don't belong in a public repo.
       - It holds slug, title, the angle (what the piece should cover) and sources.
       - A source is a KB source id, a URL, or a book or interview entered by hand: title, authors, publisher, year, ISBN, and a file of the owner's excerpts or transcript marked `[p. 112]` or `[12:30]`.
       - `search = false` turns off the drafter's own search.
     - **`article sources <brief>`** stores the brief's sources and prints their ids.
       - URLs go through `fetch.py` (robots, `noai`) and `save_source`.
       - Books and interviews become `source` rows (`work_type`, `isbn`, `citation_text`, no URL), with the excerpts as their `source_text`, which is never served.
       - A source with neither an author nor a publisher is refused.
     - **`article draft <brief>`:**
       1. Stores the sources, as above.
       2. Searches, unless `search = false`: `find_urls` with an article prompt (historians, cookbook authors, institutions; `SKIP_HOSTS`). It fetches up to 4 new pages under the same rules and records them in `found`.
       3. Makes one Sonnet 5.5 call, streamed, with structured output through `output_config.format` (not the deprecated `output_format=`). The call gets every source's whole text, numbered. It returns sections of sentences, each citing source numbers and exact quotes, plus credit (title, authors, publisher, date) for the pages it found.
       4. Checks each sentence:
          - it has at least one cite;
          - each quote is in that source's text (`quote_found`);
          - it doesn't copy 8 consecutive words from any of the article's source texts. Writeups check only the quotes, but an article draws on whole pages.
          - The locator isn't checked: code sets it from the last `[p. …]` or `[mm:ss]` marker before the quote.
       5. Keeps a draft that fails a check. It costs about $0.25, and the owner edits it anyway. The problems go in the proposal's rationale and show in the edit file, and publish refuses until they're fixed. There's no retry.
       6. Writes a `run_log` row (agent `draft`), a proposal (author `draft`; op `add`, or `change` over a published version) and the proposed article.
     - **`article edit <slug>`** opens the proposed article in `$EDITOR`.
       - **Layout:** `# title`, `## heading`, one sentence per line, a blank line between paragraphs, and `[n]` markers at the end of a line.
       - **Cites:** after `---`, one line per cite, `[n] <source id> [locator] | "quote"`, then a legend naming each source. An `*` marks the drafter's finds.
       - **Problems** show as `%` comments above their sentence.
       - **On save** it parses and checks again, and stores whatever parses, even with problems.
       - **No stored text:** a quote from a source whose text we don't hold (a book the owner reads from) passes on the owner's word.
       - **A published article** is copied into a new proposed version first.
     - **`article publish <slug>`** runs the checks and prints the counts: sentences, cites, sources, and how many cites rest on pages the drafter found. It asks for confirmation, then calls `accept_proposal` signed with `COCINA_REVIEWER` (or the login name), as the terminal review does. `article drop <slug> --why "…"` rejects the draft.
     - **Other tools:** `review.py`'s human walk skips article proposals. Auto-review and the web inbox pass them by already: an article proposal has no claim, writeup or flag.
     - **Test:** `tests/test_article.py`, with no network. The edit file round-trips, and the checks catch an uncited sentence, a quote not in its source, an 8-word copy, and the locator taken from the markers.
   - **MCP:** one `articles` tool. With no slug it lists the published articles; with a slug it returns one with every sentence's credit. It's a thin RPC call, like the other four tools.
   - **Cost:** about $0.15–0.30 a draft (a few searches, about 20–75k tokens in, 6–10k out), within the design doc's "under $1".
   - **First test** (local, 2026-10-30): a throwaway al pastor brief (Wikipedia's page, plus *Planet Taco* entered without excerpts) with search on.
     - **First draft:** 33 sentences, 9 with problems. It named sources in the prose ("Wikipedia says…"), which pulled in 8-word runs, and it left lead-in sentences uncited. The prompt now forbids both.
     - **Second draft:** 26 sentences and 1 problem, $0.17, about a minute.
     - **Found pages:** 20 of its 28 cites rested on pages the drafter found, one of them a restaurant's. Expect to check those at publish, or set `search = false`.
   - **Topics for the first five** (suggestions, chosen where the KB already holds history and origin claims; the owner swaps any):
     - **Al pastor:** from shawarma to the trompo (`al-pastor`).
     - **The cheese crisp and Sonora's flour tortilla:** Tucson's own (`cheese-crisp`, `burro`).
     - **Gringa, sincronizada, vampiro, chavindeca:** what the names mean (#92's family).
     - **Machaca:** Sonora's dried beef (`machaca`).
     - **Aguachile:** Sinaloa's raw shrimp in lime and chile (`aguachile`).
   - **Not done:**
     - claim citations;
     - a way to leave a transition sentence uncited (every sentence cites, as in writeups; revisit if the first five chafe);
     - the auditor re-checking article quotes (step 2 can read `sections` as it reads evidence);
     - cartoTaco and cards.
4. **Linker and merge.** `variant_of` and other links between entries, a real `merge` op that moves claims rather than retiring the duplicate's (`retire --into` does that today), and links surfaced in cards (`linked_from` exists).

**Carried over:**
- **Issue #1:** set the hosted Supabase auth settings (Site URL, redirect URLs, email templates) from the repo. The owner set them by hand on 2026-10-30.
- **Sign-in email:** custom SMTP; the built-in sender allows only a few emails an hour.
- **Decisions still open:** whether the MCP server and data go public, and under what terms (the code is MIT-licensed since 2026-10-31; the data isn't covered); and who reviews besides the owner.
- **Scale:** `cards()` takes about 0.8 s on the server for 76 entities. Past a few hundred entities, page it, or let `getKb()` read the snapshot.
- **Flag backlog:** in the web inbox.
- **API drift:** `review._check` passes the deprecated `output_format=`; `output_config.format` replaces it.

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
