-- Retiring published claims, and the entity that loses its last one. Run: supabase test db
begin;
select plan(15);

create function pg_temp.committed(sql text) returns void language plpgsql as $$
begin
  execute sql;
  set constraints all immediate;
  set constraints all deferred;
end $$;

-- A claim with credited evidence, published through its own proposal
create function pg_temp.published_claim(slug text, pred text, val text) returns bigint language plpgsql as $$
declare
  pid bigint;
  cid bigint;
begin
  insert into proposal (author, rationale) values ('test', 'c-' || val) returning id into pid;
  insert into claim (entity_id, predicate, value, proposal_id)
  values ((select id from entity e where e.slug = published_claim.slug), pred, val, pid) returning id into cid;
  insert into evidence (claim_id, source_id, quote, proposal_id)
  values (cid, (select id from source where publisher = 'Example Eats'), 'about ' || val, pid);
  if pred = 'also_known_as' then
    insert into alias (entity_id, text, claim_id, proposal_id)
    values ((select id from entity e where e.slug = published_claim.slug), val, cid, pid);
  end if;
  perform pg_temp.committed(format('select accept_proposal(%s, ''tester'')', pid));
  return cid;
end $$;

insert into source (url, title, publisher) values ('https://example.com/tacos', 'Tacos', 'Example Eats');
insert into entity (slug, kind, name) values
  ('taco', 'menu_category', 'Taco'), ('gringa', 'dish', 'Gringa'), ('gringas', 'dish', 'Gringas');
select pg_temp.published_claim('taco', 'note', 'a folded tortilla with a filling');
select pg_temp.published_claim('taco', 'also_known_as', 'tacos al pastor');
select pg_temp.published_claim('gringa', 'uses', 'flour tortilla');
select pg_temp.published_claim('gringas', 'uses', 'al pastor');
insert into proposal (author, rationale) values ('test', 'w-gringas');
insert into writeup (entity_id, sentences, proposal_id) values ((select id from entity where slug = 'gringas'),
  jsonb_build_array(jsonb_build_object('text', 'Gringas use al pastor.', 'claims', jsonb_build_array((select id from claim where value = 'al pastor')))),
  (select id from proposal where rationale = 'w-gringas'));
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''w-gringas''), ''tester'')');

select throws_ok(
  $$ insert into proposal (op, author, rationale) values ('retire', 'tester', 'no claims named') $$,
  '23514', null, 'a retire must name the claims it takes down');
select throws_ok(
  $$ insert into proposal (author, rationale, retires) values ('tester', 'an add', array[1]::bigint[]) $$,
  '23514', null, 'and only a retire may name any');

-- A rejected retire changes nothing
insert into proposal (op, author, rationale, retires)
values ('retire', 'tester', 'r-rejected', array[(select id from claim where value = 'tacos al pastor')]);
select reject_proposal((select id from proposal where rationale = 'r-rejected'), 'tester', 'keep it');
select is((select status::text from claim where value = 'tacos al pastor'), 'published', 'a rejected retire changes nothing');

-- Retiring a wrong alias takes down the claim and the name it backs; the entity stays
insert into proposal (op, author, rationale, retires)
values ('retire', 'tester', 'r-alias', array[(select id from claim where value = 'tacos al pastor')]);
select lives_ok(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''r-alias''), ''tester'')') $$,
  'a retire is accepted');
select is((select status::text from claim where value = 'tacos al pastor'), 'retired', 'the claim is retired');
select is((select status::text from alias where text = 'tacos al pastor'), 'retired', 'and the alias it backs');
select is(get_entity('taco') -> 'aliases', '[]'::jsonb, 'the card no longer carries the name');
select is(jsonb_array_length(get_entity('taco') -> 'claims'), 1, 'while the entity keeps its other claims');
select is((select count(*) from search_entities('tacos al pastor') where slug = 'taco'), 0::bigint,
  'and search no longer finds it by that name');

-- Retiring a duplicate's last claim retires the entity, its names and writeup; its name moves to the survivor
insert into proposal (author, rationale) values ('test', 'pending-on-twin');
insert into claim (entity_id, predicate, value, proposal_id)
values ((select id from entity where slug = 'gringas'), 'uses', 'cheese', (select id from proposal where rationale = 'pending-on-twin'));
insert into proposal (op, author, rationale, retires)
values ('retire', 'tester', 'r-twin', array[(select id from claim where value = 'al pastor')]);
insert into alias (entity_id, text, proposal_id)
values ((select id from entity where slug = 'gringa'), 'Gringas', (select id from proposal where rationale = 'r-twin'));
select lives_ok(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''r-twin''), ''tester'')') $$,
  'retiring an entity''s last claim commits');
select is((select status::text from entity where slug = 'gringas'), 'retired', 'the entity is retired with it');
select is((select count(*) from cards() c where c ->> 'slug' = 'gringas'), 0::bigint, 'and leaves every read');
select is(get_entity('gringa') -> 'aliases', '["Gringas"]'::jsonb, 'its name now finds the survivor');
select is((select status from proposal where rationale = 'pending-on-twin'), 'rejected',
  'its open proposals are rejected with it');
insert into proposal (author, rationale) values ('test', 'late-add');
insert into claim (entity_id, predicate, value, proposal_id)
values ((select id from entity where slug = 'gringas'), 'uses', 'pineapple', (select id from proposal where rationale = 'late-add'));
select throws_like(
  $$ select accept_proposal((select id from proposal where rationale = 'late-add'), 'tester') $$,
  '%about a retired entity%', 'and nothing new publishes onto it');

select * from finish();
rollback;
