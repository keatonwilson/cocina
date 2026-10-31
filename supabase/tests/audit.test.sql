-- The auditor's retires: a quote its claim doesn't need goes without a person; a claim
-- never does. Run: supabase test db
begin;
select plan(10);

create function pg_temp.committed(sql text) returns void language plpgsql as $$
begin
  execute sql;
  set constraints all immediate;
  set constraints all deferred;
end $$;

create function pg_temp.accept(rationale text, reviewer text) returns text language sql as $$
  select format('select pg_temp.committed(%L)',
                format('select accept_proposal((select id from proposal where rationale = %L), %L)', rationale, reviewer))
$$;

create function pg_temp.quote(val text, url text) returns bigint language sql as $$
  select ev.id from evidence ev join claim c on c.id = ev.claim_id join source s on s.id = ev.source_id
  where c.value = val and s.url = quote.url
$$;

-- Memela uses masa (quoted on two pages) and beans (quoted on one)
insert into source (url, title, publisher) values
  ('https://example.com/a', 'A', 'Example Eats'), ('https://example.com/b', 'B', 'Other Eats');
insert into entity (slug, kind, name) values ('memela', 'dish', 'Memela');
insert into proposal (author, rationale) values ('test', 'seed');
insert into claim (entity_id, predicate, value, proposal_id)
select (select id from entity where slug = 'memela'), 'uses', v, (select id from proposal where rationale = 'seed')
from unnest(array['masa', 'beans']) v;
insert into evidence (claim_id, source_id, quote, proposal_id)
select c.id, s.id, 'memelas use ' || c.value, (select id from proposal where rationale = 'seed')
from claim c, source s where c.value = 'masa' or s.url = 'https://example.com/a';
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''seed''), ''tester'')');

select is((select failures from source where url = 'https://example.com/a'), 0::smallint, 'a page starts with no failed checks');

-- A quote gone from page A, where masa keeps page B's
insert into proposal (op, author, rationale, retires_evidence)
values ('retire', 'audit', 'e-masa', array[pg_temp.quote('masa', 'https://example.com/a')]);
select lives_ok(pg_temp.accept('e-masa', 'audit'), 'the auditor retires a quote its claim doesn''t need');
select is((select status::text from evidence where id = pg_temp.quote('masa', 'https://example.com/a')), 'retired',
  'the quote is retired');
select is((select status::text from claim where value = 'masa'), 'published', 'and its claim stays, on its other quote');

-- Beans' only quote
insert into proposal (op, author, rationale, retires_evidence)
values ('retire', 'audit', 'e-beans', array[pg_temp.quote('beans', 'https://example.com/a')]);
select throws_like(pg_temp.accept('e-beans', 'audit'), '%without credited evidence%',
  'a quote whose claim has no other is refused');

insert into proposal (op, author, rationale, retires, retires_evidence)
values ('retire', 'audit', 'c-beans', array[(select id from claim where value = 'beans')],
        array[pg_temp.quote('beans', 'https://example.com/a')]);
select throws_like(pg_temp.accept('c-beans', 'audit'), '%only a person%', 'the auditor can''t retire a claim');
select lives_ok(pg_temp.accept('c-beans', 'tester'), 'a person can');
select is((select status::text from claim where value = 'beans'), 'retired', 'the claim is retired');
select is((select status::text from evidence where id = pg_temp.quote('beans', 'https://example.com/a')), 'retired',
  'with its quote');

-- A person keeps one
insert into proposal (op, author, rationale, retires)
values ('retire', 'audit', 'c-masa', array[(select id from claim where value = 'masa')]);
select reject_proposal((select id from proposal where rationale = 'c-masa'), 'tester', 'still true');
select is((select status::text from claim where value = 'masa'), 'published', 'a kept claim stays published');

select * from finish();
rollback;
