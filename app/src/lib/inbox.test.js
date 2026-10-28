import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isReviewer, inbox, credit, citedIds } from './inbox.js';

test('only listed reviewers get in, in any case or spacing', () => {
  const list = ' owner@example.com, Second@Example.com ';
  assert.equal(isReviewer('owner@example.com', list), true);
  assert.equal(isReviewer('second@example.com', list), true);
  assert.equal(isReviewer('stranger@example.com', list), false);
  assert.equal(isReviewer('', list), false);
  assert.equal(isReviewer('owner@example.com', undefined), false); // unset: nobody
});

test('proposals group by entity, entities by name, oldest proposal first', () => {
  const birria = { id: 1, slug: 'birria', name: 'Birria', kind: 'dish' };
  const asada = { id: 2, slug: 'asada', name: 'Asada', kind: 'protein_prep' };
  const claim = (id, entity) => ({ id, claim: [{ id: id * 10, predicate: 'uses', value: 'x', entity }], evidence: [] });
  const writeup = { id: 5, claim: [], evidence: [], writeup: [{ id: 1, sentences: [{ text: 'A.', claims: [7, 8] }], entity: birria }] };
  const groups = inbox([claim(9, birria), writeup, claim(3, asada), { id: 4, claim: [], evidence: [] }]);
  assert.deepEqual(groups.map((g) => g.entity.slug), ['asada', 'birria']);
  assert.deepEqual(groups[1].proposals.map((p) => p.id), [5, 9]);
  assert.deepEqual(citedIds([writeup, claim(9, birria)]), [7, 8]);
});

test('credit names the author, work, publisher and year', () => {
  const source = { title: 'Mexican Slang 101', publisher: 'Mexico News Daily', published_on: '2024-03-01',
    source_creator: [{ creator: { name: 'Alasdair Baverstock' } }] };
  assert.equal(credit(source), 'Alasdair Baverstock · Mexican Slang 101 · Mexico News Daily · 2024');
  assert.equal(credit({ title: '' }), '(no credit)');
});
