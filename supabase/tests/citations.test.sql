-- Citation rules and the publish gate. Run: supabase test db
begin;
select plan(25);

-- Deferred constraint triggers fire at commit, which a test never reaches:
-- run the statement, then flush the checks, then go back to deferred.
create function pg_temp.committed(sql text) returns void language plpgsql as $$
begin
  execute sql;
  set constraints all immediate;
  set constraints all deferred;
end $$;

insert into source (url, title, publisher) values ('https://example.com/chavindeca', 'Chavindecas', 'Example Eats');
insert into source (url, title) values ('https://anon.example/page', 'No credit at all');
insert into creator (name, do_not_use) values ('Opted Out', true);
insert into source (url, title, publisher) values ('https://optout.example/a', 'Opted out work', 'Optout Press');
insert into source_creator values ((select id from source where url = 'https://optout.example/a'),
                                   (select id from creator where name = 'Opted Out'));

insert into entity (slug, kind, name) values ('chavindeca', 'dish', 'Chavindeca');
insert into alias (entity_id, text, lang) values ((select id from entity where slug = 'chavindeca'), 'chavindeca', 'es');
insert into proposal (author, rationale) values ('test', 'p1');

select throws_like(
  $$ insert into claim (entity_id, predicate, value) values ((select id from entity where slug = 'chavindeca'), 'flavor', 'x') $$,
  '%is not allowed for entity%', 'a predicate outside the kind''s list is refused');

insert into claim (entity_id, predicate, value, proposal_id)
values ((select id from entity where slug = 'chavindeca'), 'originates_in', 'Chavinda, Michoacán',
        (select id from proposal where rationale = 'p1'));

select throws_like(
  $$ select pg_temp.committed('update claim set status = ''published'' where value = ''Chavinda, Michoacán''') $$,
  '%without credited evidence%', 'a claim cannot be published without evidence');

select throws_ok(
  $$ insert into evidence (claim_id, source_id, quote) values ((select id from claim limit 1), 1, repeat('x', 401)) $$,
  '23514', null, 'a quote over 400 characters is refused');

insert into evidence (claim_id, source_id, quote, proposal_id)
values ((select id from claim where value = 'Chavinda, Michoacán'),
        (select id from source where url = 'https://anon.example/page'),
        'from Chavinda, Michoacán', (select id from proposal where rationale = 'p1'));

select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p1''), ''tester'')') $$,
  '%credit%', 'evidence from a source with no author or publisher does not count as credit');

update evidence set source_id = (select id from source where url = 'https://optout.example/a');
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p1''), ''tester'')') $$,
  '%credit%', 'evidence from a do_not_use creator does not count');

update evidence set source_id = (select id from source where url = 'https://example.com/chavindeca');
select lives_ok(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p1''), ''tester'')') $$,
  'accepting a credited proposal commits');

select is((select status::text from entity where slug = 'chavindeca'), 'published',
  'the first accepted claim publishes its seeded entity');
select is((select status::text from alias where text = 'chavindeca'), 'published',
  'and the entity''s name hints');

select is((select jsonb_array_length(card -> 'claims') from (select get_entity('chavindeca') as card) c), 1,
  'the card carries the claim');
select is((select get_entity('chavindeca') #>> '{claims,0,sources,0,publisher}'), 'Example Eats',
  'with its credit');

select is((select slug from search_entities('chavideca')), 'chavindeca', 'a typo still finds the entity');

-- Credit is checked per quote: an opted-out source can't ride along with a credited one
insert into proposal (author, rationale) values ('test', 'p3');
insert into evidence (claim_id, source_id, quote, proposal_id)
values ((select id from claim where value = 'Chavinda, Michoacán'), (select id from source where url = 'https://optout.example/a'),
        'Chavinda, Michoacán', (select id from proposal where rationale = 'p3'));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p3''), ''tester'')') $$,
  '%opted out%', 'an opted-out quote is refused even beside a credited one');
select reject_proposal((select id from proposal where rationale = 'p3'), 'tester', 'creator opted out');

-- A creator who opts out after publication disappears from every card
insert into creator (name) values ('Later Opted Out');
insert into source (url, title) values ('https://late.example/a', 'Late work');
insert into source_creator values ((select id from source where url = 'https://late.example/a'),
                                   (select id from creator where name = 'Later Opted Out'));
insert into proposal (author, rationale) values ('test', 'p4');
insert into evidence (claim_id, source_id, quote, proposal_id)
values ((select id from claim where value = 'Chavinda, Michoacán'), (select id from source where url = 'https://late.example/a'),
        'Chavinda again', (select id from proposal where rationale = 'p4'));
select lives_ok(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p4''), ''tester'')') $$,
  'a second credited source is accepted');
select is((select jsonb_array_length(get_entity('chavindeca') #> '{claims,0,sources}')), 2, 'both sources are served');
update creator set do_not_use = true where name = 'Later Opted Out';
select is((select jsonb_array_length(get_entity('chavindeca') #> '{claims,0,sources}')), 1,
  'a creator who opts out later is no longer served');

-- An entity whose only credit opts out disappears, and a new source can bring it back
insert into creator (name) values ('Solo Author');
insert into source (url, title) values ('https://solo.example/molote', 'Molotes');
insert into source_creator values ((select id from source where url = 'https://solo.example/molote'),
                                   (select id from creator where name = 'Solo Author'));
insert into entity (slug, kind, name) values ('molote', 'dish', 'Molote');
insert into alias (entity_id, text) values ((select id from entity where slug = 'molote'), 'molote');
insert into proposal (author, rationale) values ('test', 'p6');
insert into claim (entity_id, predicate, value, proposal_id)
values ((select id from entity where slug = 'molote'), 'originates_in', 'Veracruz', (select id from proposal where rationale = 'p6'));
insert into evidence (claim_id, source_id, quote, proposal_id)
values ((select id from claim where value = 'Veracruz'), (select id from source where url = 'https://solo.example/molote'),
        'molotes from Veracruz', (select id from proposal where rationale = 'p6'));
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p6''), ''tester'')');
update creator set do_not_use = true where name = 'Solo Author';
select is(get_entity('molote'), null, 'an entity left with no credited claim is no longer served');
select is((select count(*) from search_entities('molote')), 0::bigint, 'nor found by search');
set local role anon;
select is((select count(*) from evidence where quote = 'molotes from Veracruz'), 0::bigint,
  'raw REST hides the opted-out quote too');
select is((select count(*) from entity where slug = 'molote'), 0::bigint, 'and the entity it left without credit');
select is((select count(*) from alias where text = 'molote'), 0::bigint, 'and that entity''s names');
reset role;

insert into proposal (author, rationale) values ('test', 'p7');
select lives_ok(
  $$ select pg_temp.committed('insert into evidence (claim_id, source_id, quote, proposal_id) values (
       (select id from claim where value = ''Veracruz''), (select id from source where url = ''https://example.com/chavindeca''),
       ''molotes are from Veracruz'', (select id from proposal where rationale = ''p7''))') $$,
  'a claim that lost its credit can still take new evidence');
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''p7''), ''tester'')');
select is(get_entity('molote') #>> '{claims,0,sources,0,publisher}', 'Example Eats', 'and a new credited source brings it back');

-- A rejected name doesn't block the same name proposed again
insert into proposal (author, rationale) values ('test', 'p5');
insert into alias (entity_id, text, proposal_id)
values ((select id from entity where slug = 'chavindeca'), 'Sincronizada michoacana', (select id from proposal where rationale = 'p5'));
select reject_proposal((select id from proposal where rationale = 'p5'), 'tester', 'no source yet');
select lives_ok(
  $$ insert into alias (entity_id, text) values ((select id from entity where slug = 'chavindeca'), 'sincronizada michoacana') $$,
  'a rejected name can be proposed again');

-- A proposed claim stays invisible to consumers
insert into proposal (author, rationale) values ('test', 'p2');
insert into claim (entity_id, predicate, value, proposal_id)
values ((select id from entity where slug = 'chavindeca'), 'note', 'unreviewed', (select id from proposal where rationale = 'p2'));

set local role anon;
select is((select count(*) from claim where value = 'unreviewed'), 0::bigint, 'anon cannot see proposed claims');
select is((select count(*) from source_text), 0::bigint, 'anon cannot read source text');
reset role;

select * from finish();
rollback;
