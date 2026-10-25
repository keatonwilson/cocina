import { test } from 'node:test';
import assert from 'node:assert/strict';
import { sourcesOf } from './sources.js';

test('one entry per work, with every fact it supports and no quotes', () => {
  const web = { title: 'Slang', url: 'https://example.com/slang', publisher: 'MND', authors: ['A. B.'], quote: 'q1', locator: null };
  const book = { title: 'Tacos', url: null, publisher: 'Press', authors: [], quote: 'q2', locator: 'p. 12' };
  const card = { claims: [
    { predicate: 'originates_in', value: 'Michoacán', sources: [web, book] },
    { predicate: 'uses', value: 'corn tortillas', sources: [{ ...web, quote: 'q3' }] }
  ] };
  assert.deepEqual(sourcesOf(card), [
    { title: 'Slang', url: 'https://example.com/slang', publisher: 'MND', authors: ['A. B.'],
      supports: ['originates_in: Michoacán', 'uses: corn tortillas'] },
    { title: 'Tacos', url: null, publisher: 'Press', authors: [], supports: ['originates_in: Michoacán'] }
  ]);
});
