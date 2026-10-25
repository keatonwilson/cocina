// The works a card relies on, once each, with the facts each one supports.
// Plain JS (no SvelteKit imports), so `node --test` can run it.

/** @param {{ claims: Array<{ predicate: string, value: string, sources: object[] }> }} card */
export function sourcesOf(card) {
  const works = new Map();
  for (const claim of card.claims) {
    for (const { quote, locator, ...work } of claim.sources) {
      const key = work.url || `${work.publisher}|${work.title}`;
      if (!works.has(key)) works.set(key, { ...work, supports: [] });
      works.get(key).supports.push(`${claim.predicate}: ${claim.value}`);
    }
  }
  return [...works.values()];
}
