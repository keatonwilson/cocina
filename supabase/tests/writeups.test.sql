-- Writeups publish only when every sentence cites its entity's published, credited claims.
-- Run: supabase test db
begin;
select plan(14);

create function pg_temp.committed(sql text) returns void language plpgsql as $$
begin
  execute sql;
  set constraints all immediate;
  set constraints all deferred;
end $$;

-- A sentence citing these claim values
create function pg_temp.sentence(txt text, variadic vals text[]) returns jsonb language sql as $$
  select jsonb_build_object('text', txt, 'claims',
    (select jsonb_agg(c.id order by c.id) from claim c where c.value = any (vals)))
$$;

create function pg_temp.propose_writeup(slug text, tag text, sentences jsonb) returns void language sql as $$
  insert into proposal (author, rationale) values ('test', tag);
  insert into writeup (entity_id, sentences, proposal_id)
  values ((select id from entity e where e.slug = propose_writeup.slug), sentences,
          (select id from proposal where rationale = tag));
$$;

insert into source (url, title, publisher) values ('https://example.com/birria', 'Birria', 'Example Eats');
insert into creator (name) values ('Birria Writer');
insert into source (url, title) values ('https://writer.example/birria', 'On birria');
insert into source_creator values ((select id from source where url = 'https://writer.example/birria'),
                                   (select id from creator where name = 'Birria Writer'));
insert into entity (slug, kind, name) values ('birria', 'dish', 'Birria'), ('pozole', 'dish', 'Pozole');

-- Published: birria is from Jalisco (Example Eats) and uses goat (Birria Writer); pozole is from Guerrero
insert into proposal (author, rationale) values ('test', 'c1');
insert into claim (entity_id, predicate, value, proposal_id) values
  ((select id from entity where slug = 'birria'), 'originates_in', 'Jalisco', (select id from proposal where rationale = 'c1')),
  ((select id from entity where slug = 'birria'), 'uses', 'goat', (select id from proposal where rationale = 'c1')),
  ((select id from entity where slug = 'pozole'), 'originates_in', 'Guerrero', (select id from proposal where rationale = 'c1'));
insert into evidence (claim_id, source_id, quote, proposal_id) values
  ((select id from claim where value = 'Jalisco'), (select id from source where publisher = 'Example Eats'),
   'birria comes from Jalisco', (select id from proposal where rationale = 'c1')),
  ((select id from claim where value = 'goat'), (select id from source where url = 'https://writer.example/birria'),
   'traditionally made with goat', (select id from proposal where rationale = 'c1')),
  ((select id from claim where value = 'Guerrero'), (select id from source where publisher = 'Example Eats'),
   'pozole from Guerrero', (select id from proposal where rationale = 'c1'));
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''c1''), ''tester'')');

-- Proposed only: birria uses beef
insert into proposal (author, rationale) values ('test', 'c2');
insert into claim (entity_id, predicate, value, proposal_id)
values ((select id from entity where slug = 'birria'), 'uses', 'beef', (select id from proposal where rationale = 'c2'));

select pg_temp.propose_writeup('birria', 'w-proposed-claim', jsonb_build_array(pg_temp.sentence('Birria uses beef.', 'beef')));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w-proposed-claim''), ''tester'')') $$,
  '%cites no published, credited claim%', 'a writeup citing a proposed claim is refused');

select pg_temp.propose_writeup('birria', 'w-other-entity', jsonb_build_array(pg_temp.sentence('It is from Guerrero.', 'Guerrero')));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w-other-entity''), ''tester'')') $$,
  '%cites no published, credited claim%', 'a writeup citing another entity''s claim is refused');

select pg_temp.propose_writeup('birria', 'w-uncited', jsonb_build_array(
  pg_temp.sentence('Birria is from Jalisco.', 'Jalisco'), jsonb_build_object('text', 'It is delicious.', 'claims', '[]'::jsonb)));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w-uncited''), ''tester'')') $$,
  '%cites no published, credited claim%', 'a sentence that cites nothing is refused');

select pg_temp.propose_writeup('birria', 'w-malformed', jsonb_build_array(jsonb_build_object('text', 'Birria.', 'claims', '"x"'::jsonb)));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w-malformed''), ''tester'')') $$,
  '%cites no published, credited claim%', 'a malformed sentence fails the check rather than erroring');

select pg_temp.propose_writeup('birria', 'w1', jsonb_build_array(
  pg_temp.sentence('Birria is a dish from Jalisco.', 'Jalisco'), pg_temp.sentence('It is made with goat.', 'goat')));
select lives_ok(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w1''), ''tester'')') $$,
  'a writeup citing its entity''s published, credited claims is accepted');
select is(jsonb_array_length(get_entity('birria') -> 'writeup'), 2, 'the card carries the writeup');
select is(get_entity('birria') #>> '{writeup,0,text}', 'Birria is a dish from Jalisco.', 'first sentence first');
select ok((get_entity('birria') #> '{writeup,1,claims,0}') = (
  select c -> 'id' from jsonb_array_elements(get_entity('birria') -> 'claims') c where c ->> 'value' = 'goat'),
  'a sentence''s citations point at the card''s claims');

select pg_temp.propose_writeup('birria', 'w2', jsonb_build_array(
  pg_temp.sentence('Birria comes from Jalisco and is made with goat.', 'Jalisco', 'goat')));
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w2''), ''tester'')');
select is((select count(*) from writeup where status = 'published'), 1::bigint, 'a new writeup leaves one live per entity');
select is((select status::text from writeup where proposal_id = (select id from proposal where rationale = 'w1')),
  'retired', 'the one it replaced is retired');

-- Proposed writeups stay private; published ones are readable, until a cited claim loses its credit
select pg_temp.propose_writeup('birria', 'w-draft', jsonb_build_array(pg_temp.sentence('Draft.', 'Jalisco')));
set local role anon;
select is((select count(*) from writeup), 1::bigint, 'anon sees the published writeup and not the draft');
reset role;

update creator set do_not_use = true where name = 'Birria Writer';
select is(get_entity('birria') -> 'writeup', 'null'::jsonb, 'a writeup citing a claim that lost its credit is hidden');
select is(get_entity('birria') #>> '{claims,0,value}', 'Jalisco', 'while the entity is still served');
set local role anon;
select is((select count(*) from writeup), 0::bigint, 'over raw REST too');
reset role;

select * from finish();
rollback;
