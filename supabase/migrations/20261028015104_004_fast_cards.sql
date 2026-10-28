-- cards() timed out for anon once ~1200 claims were published (3 s statement
-- timeout; 2 s even as the service role): card() ran per entity, with
-- credited / has_credit / served evaluated row by row inside nested subqueries,
-- and under anon every table read added its RLS policy on top.
--
-- cards_of() builds the same cards in one set-based pass: credited sources,
-- credited evidence, claims that have it and served entities are each computed
-- once, then joined and aggregated. It runs as definer (no RLS) because these
-- CTEs are the read rules; every row it returns passes the same tests as
-- credited(), has_credit(), served() and sentences_ok().

create function public.cards_of(p_ids bigint[]) returns table (entity_id bigint, card jsonb)
language sql stable security definer set search_path = '' as $$
  with
  -- credited(): named by a person or a publisher, and no creator opted out
  ok_source as (
    select s.* from public.source s
    where (s.publisher is not null or exists (select 1 from public.source_creator sc where sc.source_id = s.id))
      and not exists (select 1 from public.source_creator sc join public.creator cr on cr.id = sc.creator_id
                      where sc.source_id = s.id and cr.do_not_use)
  ),
  ok_evidence as (
    select ev.* from public.evidence ev
    where ev.status = 'published' and ev.source_id in (select id from ok_source)
  ),
  -- has_credit(): a published claim with at least one credited, published quote
  ok_claim as (
    select c.* from public.claim c
    where c.status = 'published' and c.id in (select claim_id from ok_evidence)
  ),
  -- served(): a published entity with at least one such claim
  served as (
    select e.* from public.entity e
    where e.status = 'published' and e.id in (select ok_claim.entity_id from ok_claim)
  ),
  wanted as (select * from served where p_ids is null or id = any (p_ids)),
  authors as (
    select sc.source_id, jsonb_agg(cr.name order by cr.name) as names
    from public.source_creator sc join public.creator cr on cr.id = sc.creator_id
    group by sc.source_id
  ),
  claim_json as (
    select c.entity_id, c.id, c.predicate,
      jsonb_build_object(
        'id', c.id,
        'predicate', c.predicate,
        'value', c.value,
        'object', (select jsonb_build_object('slug', o.slug, 'name', o.name) from served o where o.id = c.object_id),
        'confidence', count(distinct coalesce(substring(s.url from '^[a-z]+://(?:www\.)?([^/]+)'),
                                              lower(s.publisher), lower(s.title))),   -- distinct sites or works
        'verified_at', max(ev.fetched_at),
        'sources', jsonb_agg(jsonb_build_object(
          'title', s.title, 'url', s.url, 'publisher', s.publisher, 'published_on', s.published_on,
          'work_type', s.work_type, 'license', s.license, 'authors', coalesce(a.names, '[]'),
          'quote', ev.quote, 'locator', ev.locator
        ) order by ev.fetched_at)
      ) as j
    from ok_claim c
    join wanted w on w.id = c.entity_id
    join ok_evidence ev on ev.claim_id = c.id
    join ok_source s on s.id = ev.source_id
    left join authors a on a.source_id = s.id
    group by c.entity_id, c.id, c.predicate, c.value, c.object_id
  ),
  claims as (
    select cj.entity_id, jsonb_agg(cj.j order by cj.predicate = 'note', cj.predicate, cj.id) as j
    from claim_json cj group by cj.entity_id
  ),
  aliases as (
    select a.entity_id, jsonb_agg(a.text order by a.text) as j
    from public.alias a join wanted w on w.id = a.entity_id
    where a.status = 'published' and public.norm(a.text) <> public.norm(w.name)
      and (a.claim_id is null or a.claim_id in (select id from ok_claim))
    group by a.entity_id
  ),
  -- Incoming links: "chavindeca is a variant_of this"
  linked as (
    select c.object_id as entity_id,
      jsonb_agg(jsonb_build_object('slug', f.slug, 'name', f.name, 'predicate', c.predicate) order by f.slug) as j
    from ok_claim c join served f on f.id = c.entity_id
    where c.object_id in (select id from wanted)
    group by c.object_id
  ),
  -- The live writeup, or null once a cited claim has lost its credit
  writeups as (
    select wu.entity_id, wu.sentences from public.writeup wu join wanted w on w.id = wu.entity_id
    where wu.status = 'published' and public.sentences_ok(wu.entity_id, wu.sentences)
  )
  select w.id, jsonb_build_object(
    'slug', w.slug,
    'kind', w.kind,
    'name', w.name,
    'aliases', coalesce(al.j, '[]'),
    'claims', coalesce(cl.j, '[]'),
    'writeup', wu.sentences,
    'linked_from', coalesce(lf.j, '[]')
  )
  from wanted w
  left join claims cl on cl.entity_id = w.id
  left join aliases al on al.entity_id = w.id
  left join linked lf on lf.entity_id = w.id
  left join writeups wu on wu.entity_id = w.id
  order by w.slug
$$;

create or replace function public.card(p_entity bigint) returns jsonb
language sql stable as $$
  select c.card from public.cards_of(array[p_entity]) c
$$;

create or replace function public.cards() returns setof jsonb
language sql stable as $$
  select c.card from public.cards_of(null) c
$$;

create or replace function public.get_entity(p_slug text) returns jsonb
language sql stable as $$
  select c.card from public.cards_of(array(select e.id from public.entity e where e.slug = p_slug)) c
$$;
