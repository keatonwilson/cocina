// The inbox holds what auto-review flagged for a human (pipeline: review --auto).
// Decisions go through accept_proposal / reject_proposal, the same gate the
// terminal review uses, signed with the reviewer's email.
import { error, fail } from '@sveltejs/kit';
import { serviceDb } from '$lib/server/db.js';
import { SELECT, inbox, citedIds } from '$lib/inbox.js';

export async function load() {
  const db = serviceDb();
  // ponytail: flagged proposals are a small share of the open ones; page with range() past 1000
  const [flagged, waiting] = await Promise.all([
    db.from('proposal').select(SELECT).eq('status', 'open').not('flag', 'is', null).order('id'),
    db.from('proposal').select('id', { count: 'exact', head: true }).eq('status', 'open').is('flag', null)
  ]);
  if (flagged.error) error(500, flagged.error.message);
  const groups = inbox(flagged.data);

  // Context: the published facts of each entity (what a flag clashes with) and the claims writeups cite
  const entityIds = groups.map((g) => g.entity.id);
  const cited = citedIds(flagged.data);
  const [facts, citedClaims] = await Promise.all([
    entityIds.length
      ? db.from('claim').select('id, entity_id, predicate, value').in('entity_id', entityIds).eq('status', 'published').order('id')
      : { data: [], error: null },
    cited.length ? db.from('claim').select('id, predicate, value, status').in('id', cited) : { data: [], error: null }
  ]);
  // Without the context a reviewer could accept a conflict without seeing what it clashes with
  const failed = facts.error ?? citedClaims.error;
  if (failed) error(500, failed.message);
  return {
    groups: groups.map((g) => ({ ...g, facts: (facts.data ?? []).filter((c) => c.entity_id === g.entity.id) })),
    claims: Object.fromEntries((citedClaims.data ?? []).map((c) => [c.id, c])),
    waiting: waiting.count ?? 0
  };
}

const proposalId = async (request) => {
  const form = await request.formData();
  return { id: Number(form.get('id')), reason: String(form.get('reason') ?? '').trim() };
};

export const actions = {
  accept: async ({ request, locals }) => {
    const { id } = await proposalId(request);
    const { error: e } = await serviceDb().rpc('accept_proposal', { p_id: id, p_reviewer: locals.reviewer });
    // A citation rule refused it: say which, and leave it in the inbox
    if (e) return fail(409, { id, message: e.message });
    return { id, done: 'accepted' };
  },
  reject: async ({ request, locals }) => {
    const { id, reason } = await proposalId(request);
    if (!reason) return fail(400, { id, message: 'Say why: the reason goes back to the agent.' });
    const { error: e } = await serviceDb().rpc('reject_proposal', { p_id: id, p_reviewer: locals.reviewer, p_reason: reason });
    if (e) return fail(409, { id, message: e.message });
    return { id, done: 'rejected' };
  }
};
