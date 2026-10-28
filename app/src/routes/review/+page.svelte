<script>
  import { enhance } from '$app/forms';
  import { tick } from 'svelte';
  import { credit, targetOf } from '$lib/inbox.js';

  export let data;
  /** @type {{ id?: number, message?: string, done?: string } | null} */
  export let form;

  let focus = 0;
  $: items = data.groups.flatMap((g) => g.proposals);
  $: focus = Math.min(focus, Math.max(items.length - 1, 0));

  // a accept · r reason, then Enter rejects · s / j next · k previous
  async function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea, button')) return;
    const p = items[focus];
    if (!p) return;
    if (e.key === 'j' || e.key === 's') focus = Math.min(focus + 1, items.length - 1);
    else if (e.key === 'k') focus = Math.max(focus - 1, 0);
    else if (e.key === 'a') /** @type {HTMLFormElement | null} */ (document.getElementById(`accept-${p.id}`))?.requestSubmit();
    else if (e.key === 'r') document.getElementById(`reason-${p.id}`)?.focus();
    else return;
    e.preventDefault();
    await tick();
    document.getElementById(`p-${items[focus]?.id}`)?.scrollIntoView({ block: 'nearest' });
  }
</script>

<svelte:window on:keydown={onKey} />
<svelte:head><title>Review · cocina</title></svelte:head>

<header>
  <h1>Review</h1>
  <p>
    {items.length} flagged for you{#if data.waiting} · {data.waiting} more waiting for auto-review{/if}
    · keys: <kbd>a</kbd> accept, <kbd>r</kbd> reject, <kbd>j</kbd>/<kbd>k</kbd> move
  </p>
  <form method="POST" action="?/logout" use:enhance><button class="quiet">Sign out</button></form>
</header>

<main>
  {#if !items.length}
    <p class="empty">Nothing needs you right now.</p>
  {/if}
  {#each data.groups as g (g.entity.id)}
    <section>
      <h2>{g.entity.name} <span class="kind">{g.entity.kind}</span></h2>
      {#each g.proposals as p (p.id)}
        {@const claim = p.claim?.[0]}
        {@const target = targetOf(p)}
        {@const w = p.writeup?.[0]}
        <article id="p-{p.id}" class:focused={items[focus]?.id === p.id}>
          {#if w}
            <h3>Writeup <span class="meta">#{p.id} · {p.author}</span></h3>
            <ol>
              {#each w.sentences as s}
                <li>
                  {s.text}
                  <ul class="cites">
                    {#each s.claims as id}
                      {@const c = data.claims[id]}
                      <li>[{id}] {c ? `${c.predicate}: ${c.value}${c.status === 'published' ? '' : ` (${c.status})`}` : 'missing'}</li>
                    {/each}
                  </ul>
                </li>
              {/each}
            </ol>
          {:else if target}
            <h3>
              {target.predicate} = “{target.value}”{#if claim?.object} → {claim.object.name}{/if}
              <span class="meta">#{p.id} · {claim ? 'new claim' : `more evidence for P${target.id}`} · {p.author}</span>
            </h3>
            <ul class="quotes">
              {#each p.evidence as ev}
                <li>
                  <q>{ev.quote}</q>{#if ev.locator} ({ev.locator}){/if}
                  <div class="credit">
                    {credit(ev.source)}
                    {#if ev.source.url}· <a href={ev.source.url} target="_blank" rel="noopener noreferrer">open</a>{/if}
                  </div>
                </li>
              {/each}
            </ul>
            {@const same = g.facts.filter((f) => f.predicate === target.predicate)}
            {#if same.length}
              <p class="facts">Published {target.predicate}: {same.map((f) => `P${f.id} ${f.value}`).join(' · ')}</p>
            {/if}
          {/if}

          <p class="flag">⚠ {p.flag}</p>
          {#if p.rationale}<p class="meta">{p.rationale}</p>{/if}

          <div class="actions">
            <form id="accept-{p.id}" method="POST" action="?/accept" use:enhance>
              <input type="hidden" name="id" value={p.id} />
              <button>Accept</button>
            </form>
            <form method="POST" action="?/reject" use:enhance>
              <input type="hidden" name="id" value={p.id} />
              <input id="reason-{p.id}" name="reason" placeholder="Why reject? (goes back to the agent)" required />
              <button class="quiet">Reject</button>
            </form>
          </div>
          {#if form?.id === p.id && form.message}<p class="error">{form.message}</p>{/if}
        </article>
      {/each}
    </section>
  {/each}
</main>

<style>
  :global(body) { margin: 0; background: #fbfaf8; color: #1f1d1a; font: 15px/1.5 system-ui, sans-serif; }
  header, main { max-width: 52rem; margin: 0 auto; padding: 0 16px; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; padding-top: 24px; }
  header h1 { margin: 0; }
  header p { margin: 0; flex: 1; color: #6b665f; }
  h2 { margin: 28px 0 8px; font-size: 1.15rem; }
  h3 { margin: 0 0 8px; font-size: 1rem; }
  .kind, .meta { color: #6b665f; font-weight: normal; font-size: 0.85rem; }
  article { background: #fff; border: 1px solid #e4e0da; border-radius: 8px; padding: 14px 16px; margin: 10px 0; }
  article.focused { border-color: #c2571a; box-shadow: 0 0 0 2px #c2571a33; }
  .quotes { padding-left: 18px; margin: 0; }
  .quotes li { margin-bottom: 6px; }
  .credit { color: #6b665f; font-size: 0.85rem; }
  .cites { color: #6b665f; font-size: 0.85rem; padding-left: 16px; }
  .facts { font-size: 0.85rem; color: #6b665f; }
  .flag { background: #fdf3e7; border-radius: 6px; padding: 6px 10px; margin: 10px 0 6px; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .actions form { display: flex; gap: 6px; flex: 0 1 auto; }
  .actions form:last-child { flex: 1 1 18rem; }
  .actions input[name='reason'] { flex: 1; min-width: 0; }
  input, button { font: inherit; padding: 6px 10px; border-radius: 6px; border: 1px solid #cfc9c0; }
  button { background: #1f1d1a; color: #fff; border-color: #1f1d1a; cursor: pointer; }
  button.quiet { background: transparent; color: inherit; border-color: #cfc9c0; }
  .error { color: #b3261e; }
  .empty { color: #6b665f; margin-top: 40px; }
  kbd { font: 0.8rem ui-monospace, monospace; border: 1px solid #cfc9c0; border-radius: 4px; padding: 0 4px; }
  @media (prefers-color-scheme: dark) {
    :global(body) { background: #171614; color: #ece8e2; }
    article { background: #201e1b; border-color: #3a3631; }
    .flag { background: #3a2a18; }
    .kind, .meta, .credit, .cites, .facts, header p, .empty { color: #a9a39a; }
    input { background: #171614; color: inherit; border-color: #4a453e; }
    button { background: #ece8e2; color: #171614; border-color: #ece8e2; }
    button.quiet { background: transparent; color: inherit; border-color: #4a453e; }
  }
</style>
