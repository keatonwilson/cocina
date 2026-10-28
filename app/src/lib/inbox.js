// The review inbox's logic, kept out of the routes so node:test can run it.
// The same shapes as pipeline/cocina/review.py: a proposal carries a new claim,
// more evidence for a published one, or a writeup.

/** Proposals with what a reviewer needs: claim or writeup, quotes, sources and their credit. */
export const SELECT =
  'id, author, rationale, flag, created_at, ' +
  'claim(id, predicate, value, entity:entity!claim_entity_id_fkey(id, slug, name, kind), object:entity!claim_object_id_fkey(name)), ' +
  'evidence(quote, locator, claim:claim(id, predicate, value, entity:entity!claim_entity_id_fkey(id, slug, name, kind)), ' +
  'source(title, url, publisher, published_on, source_creator(creator(name)))), ' +
  'writeup(id, sentences, entity:entity(id, slug, name, kind))';

/** Whether an email is on the REVIEWERS list (comma-separated, any case). */
export function isReviewer(email, list) {
  const allowed = String(list ?? '').split(',').map((e) => e.trim().toLowerCase()).filter(Boolean);
  return Boolean(email) && allowed.includes(String(email).trim().toLowerCase());
}

/** The claim a proposal is about: its new claim, or the published one it adds evidence to. */
export const targetOf = (p) => p.claim?.[0] ?? p.evidence?.[0]?.claim ?? null;

export const entityOf = (p) => p.writeup?.[0]?.entity ?? targetOf(p)?.entity ?? null;

/** Open proposals grouped by entity (by name), each group oldest first. */
export function inbox(proposals) {
  const groups = new Map();
  for (const p of proposals) {
    const e = entityOf(p);
    if (!e) continue;
    if (!groups.has(e.id)) groups.set(e.id, { entity: e, proposals: [] });
    groups.get(e.id).proposals.push(p);
  }
  return [...groups.values()]
    .sort((a, b) => a.entity.name.localeCompare(b.entity.name))
    .map((g) => ({ ...g, proposals: g.proposals.sort((a, b) => a.id - b.id) }));
}

/** "Author · Title · Publisher · 2024", as the terminal review prints it. */
export function credit(source) {
  const authors = (source.source_creator ?? []).map((sc) => sc.creator.name).join(', ');
  const bits = [authors, source.title, source.publisher, (source.published_on ?? '').slice(0, 4)];
  return bits.filter(Boolean).join(' · ') || '(no credit)';
}

/** Claim ids a set of writeup proposals cite. */
export const citedIds = (proposals) =>
  [...new Set(proposals.flatMap((p) => p.writeup?.[0]?.sentences.flatMap((s) => s.claims) ?? []))];
