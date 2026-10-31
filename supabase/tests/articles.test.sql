-- Articles publish only through a person, with every sentence citing a credited source.
-- Run: supabase test db
begin;
select plan(14);

create function pg_temp.committed(sql text) returns void language plpgsql as $$
begin
  execute sql;
  set constraints all immediate;
  set constraints all deferred;
end $$;

-- One sentence citing a source (by title) with a quote
create function pg_temp.sections(title text, quote text default 'goats were roasted whole in pits') returns jsonb
language sql as $$
  select jsonb_build_array(jsonb_build_object('heading', 'Origins', 'paragraphs', jsonb_build_array(jsonb_build_array(
    jsonb_build_object('text', 'Birria began as goat roasted in a pit.',
                       'cites', jsonb_build_array(jsonb_build_object(
                         'source', (select id from source s where s.title = sections.title),
                         'quote', quote, 'locator', 'p. 12')))))))
$$;

create function pg_temp.propose(tag text, sections jsonb) returns void language sql as $$
  insert into proposal (author, rationale) values ('draft', tag);
  insert into article (slug, title, sections, proposal_id)
  values ('birria-history', 'How birria came to be', sections, (select id from proposal where rationale = tag));
$$;

insert into creator (name) values ('Birria Historian');
insert into source (title, work_type, publisher) values ('Goats of Jalisco', 'book', 'University Press');
insert into source_creator values ((select id from source where title = 'Goats of Jalisco'),
                                   (select id from creator where name = 'Birria Historian'));
insert into source (url, title) values ('https://anon.example/birria', 'No credit at all');

select pg_temp.propose('v1', pg_temp.sections('Goats of Jalisco'));

select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''v1''), ''auto-review'')') $$,
  '%only a person publishes it%', 'auto-review cannot publish an article');
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''v1''), ''draft'')') $$,
  '%only a person publishes it%', 'nor can the drafter');

set local role anon;
select is((select count(*) from article), 0::bigint, 'anon sees no proposed article');
select is(articles(), '[]'::jsonb, 'and articles() lists none');
reset role;

select lives_ok(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''v1''), ''keaton'')') $$,
  'a person publishes it');
select is((select status::text from article where proposal_id = (select id from proposal where rationale = 'v1')),
          'published', 'the article is published');

set local role anon;
select is((select count(*) from article), 1::bigint, 'anon sees the published article');
select is(get_article('birria-history') #>> '{sources}' like '%Birria Historian%', true,
          'get_article carries the cited source''s credit');
reset role;

-- A sentence that cites nothing, or a source with no credit, doesn't publish
select pg_temp.propose('uncited', jsonb_build_array(jsonb_build_object('heading', null, 'paragraphs',
  jsonb_build_array(jsonb_build_array(jsonb_build_object('text', 'Birria is loved.', 'cites', '[]'::jsonb))))));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''uncited''), ''keaton'')') $$,
  '%cites no credited source%', 'an uncited sentence blocks publication');

select pg_temp.propose('uncredited', pg_temp.sections('No credit at all'));
select throws_like(
  $$ select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''uncredited''), ''keaton'')') $$,
  '%cites no credited source%', 'a source with neither author nor publisher is not credit');

-- A second version retires the first
select pg_temp.propose('v2', pg_temp.sections('Goats of Jalisco', 'roasted whole in pits lined with stone'));
select pg_temp.committed('select accept_proposal((select id from proposal where rationale = ''v2''), ''keaton'')');
select is((select array_agg(status::text order by id) from article
           where proposal_id in (select id from proposal where rationale in ('v1', 'v2'))),
          array['retired', 'published'], 'accepting a new version retires the old');

select pg_temp.propose('dropped', pg_temp.sections('Goats of Jalisco'));
select reject_proposal((select id from proposal where rationale = 'dropped'), 'keaton', 'not needed');
select is((select status::text from article where proposal_id = (select id from proposal where rationale = 'dropped')),
          'rejected', 'rejecting the proposal rejects the article');

-- A creator who opts out after publication takes the article down with them
update creator set do_not_use = true where name = 'Birria Historian';
set local role anon;
select is((select count(*) from article), 0::bigint, 'an opted-out creator hides the article');
select is(get_article('birria-history'), null, 'from get_article too');
reset role;

select * from finish();
rollback;
