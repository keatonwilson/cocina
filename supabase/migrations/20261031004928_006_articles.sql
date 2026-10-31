-- Articles: long-form history and technique, every sentence citing places in
-- sources (a quote and a locator: a book page, an interview timestamp). An agent
-- drafts from the owner's brief; only a person publishes (python -m cocina.article).
-- Whether a quote is really in its source, and whether a sentence copies one,
-- is checked in Python, as for evidence and writeups.

create table public.article (
  id bigint generated always as identity primary key,
  slug text not null,
  title text not null check (btrim(title) <> ''),
  -- [{heading, paragraphs: [[{text, cites: [{source, quote, locator}]}]]}]
  sections jsonb not null check (jsonb_typeof(sections) = 'array'),
  sources bigint[] not null default '{}',   -- the owner's picks, from the brief
  found bigint[] not null default '{}',     -- pages the drafter found itself
  status public.kb_status not null default 'proposed',
  proposal_id bigint references public.proposal,
  created_at timestamptz not null default now()
);
-- One live version per slug; accepting a new one retires the old
create unique index article_live on public.article (slug) where status = 'published';

-- Every section holds paragraphs, every paragraph sentences, every sentence text and
-- at least one cite, and every cite a credited source and a quote of 1–400 characters.
-- Takes the column rather than the row's id, so the RLS policy below can use it.
-- (The CASEs keep a malformed element from raising instead of failing the check.)
create function public.article_ok(p_sections jsonb) returns boolean
language sql stable as $$
  with sec as (
    select s from jsonb_array_elements(case when jsonb_typeof(p_sections) = 'array' then p_sections else '[]' end) s
  ), para as (
    select p from sec, jsonb_array_elements(
      case when jsonb_typeof(s -> 'paragraphs') = 'array' then s -> 'paragraphs' else '[]' end) p
  ), sent as (
    select e from para, jsonb_array_elements(case when jsonb_typeof(p) = 'array' then p else '[]' end) e
  ), cite as (
    select c from sent, jsonb_array_elements(
      case when jsonb_typeof(e -> 'cites') = 'array' then e -> 'cites' else '[]' end) c
  )
  select exists (select 1 from sent)
    and not exists (
      select 1 from sec
      where coalesce(jsonb_typeof(s -> 'paragraphs'), '') <> 'array'
         or coalesce(jsonb_typeof(s -> 'heading'), 'null') not in ('string', 'null'))
    and not exists (
      select 1 from para
      where case when jsonb_typeof(p) = 'array' then jsonb_array_length(p) = 0 else true end)
    and not exists (
      select 1 from sent
      where coalesce(jsonb_typeof(e -> 'text'), '') <> 'string' or btrim(e ->> 'text') = ''
         or case when jsonb_typeof(e -> 'cites') = 'array' then jsonb_array_length(e -> 'cites') = 0 else true end)
    and not exists (
      select 1 from cite
      where case when coalesce(c ->> 'source', '') ~ '^[0-9]{1,18}$' and jsonb_typeof(c -> 'source') = 'number'
                      and jsonb_typeof(c -> 'quote') = 'string'
                      and coalesce(jsonb_typeof(c -> 'locator'), 'null') in ('string', 'null')
                 then length(c ->> 'quote') not between 1 and 400 or not public.credited((c ->> 'source')::bigint)
                 else true end)
$$;

create function public.trg_article_ok() returns trigger
language plpgsql as $$
declare
  a public.article;
begin
  select * into a from public.article where id = new.id;
  if not found or a.status <> 'published' then return null; end if;
  if not public.article_ok(a.sections) then
    raise exception 'article % is published with a sentence that cites no credited source', a.id;
  end if;
  return null;
end $$;

create constraint trigger article_ok after insert or update on public.article
  deferrable initially deferred for each row execute function public.trg_article_ok();

-- ---------------------------------------------------------------------------
-- The gate learns articles (005's accept_proposal, plus the two article blocks)
-- ---------------------------------------------------------------------------

create or replace function public.accept_proposal(p_id bigint, p_reviewer text) returns void
language plpgsql as $$
declare
  p public.proposal;
  gone bigint[];
  stale bigint;
begin
  select * into p from public.proposal where id = p_id for update;
  if not found or p.status <> 'open' then
    raise exception 'proposal % is not open', p_id;
  end if;
  -- ponytail: merge arrives with the linker; retire --into covers duplicates until then
  if p.op not in ('add', 'change', 'retire') then
    raise exception 'op % is not implemented yet', p.op;
  end if;
  -- Articles are the one thing only a person publishes: never auto-review, never the drafter
  if p_reviewer in ('auto-review', 'draft') and exists (select 1 from public.article where proposal_id = p_id) then
    raise exception 'proposal % is an article: only a person publishes it', p_id;
  end if;
  -- Nothing publishes onto an entity a person retired
  if exists (select 1 from public.entity e where e.status = 'retired' and e.id in (
      select entity_id from public.claim where proposal_id = p_id
      union select c.entity_id from public.evidence ev join public.claim c on c.id = ev.claim_id where ev.proposal_id = p_id
      union select entity_id from public.alias where proposal_id = p_id
      union select entity_id from public.writeup where proposal_id = p_id)) then
    raise exception 'proposal % is about a retired entity', p_id;
  end if;

  update public.entity set status = 'published' where proposal_id = p_id and status = 'proposed';
  update public.claim set status = 'published' where proposal_id = p_id and status = 'proposed';
  update public.evidence set status = 'published' where proposal_id = p_id and status = 'proposed';
  update public.alias set status = 'published' where proposal_id = p_id and status = 'proposed';

  -- The first accepted claim on a seeded entity publishes the entity and its name hints
  update public.entity set status = 'published'
  where status = 'proposed' and id in (select entity_id from public.claim where proposal_id = p_id);
  update public.alias set status = 'published'
  where status = 'proposed' and claim_id is null
    and entity_id in (select entity_id from public.claim where proposal_id = p_id);

  -- A change replaces the claim it supersedes
  with old as (
    update public.claim set status = 'retired'
    where id in (select supersedes from public.claim where proposal_id = p_id and supersedes is not null)
    returning id
  )
  update public.alias set status = 'retired' where claim_id in (select id from old);

  -- A new writeup replaces the entity's published one (retired first: one live per entity)
  update public.writeup set status = 'retired'
  where status = 'published'
    and entity_id in (select entity_id from public.writeup where proposal_id = p_id and status = 'proposed');
  update public.writeup set status = 'published' where proposal_id = p_id and status = 'proposed';

  -- A new version of an article replaces the published one (retired first: one live per slug)
  update public.article set status = 'retired'
  where status = 'published'
    and slug in (select slug from public.article where proposal_id = p_id and status = 'proposed');
  update public.article set status = 'published' where proposal_id = p_id and status = 'proposed';

  -- A retire takes its claims down, with the names they back. A writeup citing one
  -- stops being served (sentences_ok) until `write --all` rewrites it.
  if p.op = 'retire' then
    update public.claim set status = 'retired' where id = any (p.retires) and status = 'published';
    update public.alias set status = 'retired' where claim_id = any (p.retires) and status = 'published';
    -- An entity left with no published claim goes too, with its names and writeup.
    -- ponytail: other entities' claims linking to it keep their text; card() drops the link
    with g as (
      update public.entity e set status = 'retired'
      where e.status = 'published'
        and e.id in (select entity_id from public.claim where id = any (p.retires))
        and not exists (select 1 from public.claim c where c.entity_id = e.id and c.status = 'published')
      returning e.id
    )
    select coalesce(array_agg(id), '{}') into gone from g;
    update public.alias set status = 'retired' where entity_id = any (gone) and status = 'published';
    update public.writeup set status = 'retired' where entity_id = any (gone) and status = 'published';
    -- Its open proposals are about something that no longer exists
    for stale in
      select proposal_id from public.claim where entity_id = any (gone) and status = 'proposed'
      union select ev.proposal_id from public.evidence ev join public.claim c on c.id = ev.claim_id
            where c.entity_id = any (gone) and ev.status = 'proposed'
      union select proposal_id from public.alias where entity_id = any (gone) and status = 'proposed'
      union select proposal_id from public.writeup where entity_id = any (gone) and status = 'proposed'
    loop
      if exists (select 1 from public.proposal where id = stale and status = 'open') then
        perform public.reject_proposal(stale, p_reviewer, 'its entity was retired');
      end if;
    end loop;
  end if;

  update public.proposal set status = 'accepted', decided_by = p_reviewer, decided_at = now()
  where id = p_id;
end $$;

create or replace function public.reject_proposal(p_id bigint, p_reviewer text, p_reason text) returns void
language plpgsql as $$
begin
  if not exists (select 1 from public.proposal where id = p_id and status = 'open') then
    raise exception 'proposal % is not open', p_id;
  end if;
  update public.article set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.writeup set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.alias set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.evidence set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.claim set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.entity set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.proposal set status = 'rejected', decided_by = p_reviewer, decided_at = now(), reason = p_reason
  where id = p_id;
end $$;

-- ---------------------------------------------------------------------------
-- Reads (published and still credited only, whoever calls)
-- ---------------------------------------------------------------------------

-- An article as stored, plus the credit of every source it cites, keyed by source id
create function public.get_article(p_slug text) returns jsonb
language sql stable security definer set search_path = '' as $$
  select jsonb_build_object(
    'slug', a.slug,
    'title', a.title,
    'published_at', p.decided_at,
    'sections', a.sections,
    'sources', (
      select coalesce(jsonb_object_agg(s.id::text, jsonb_build_object(
        'title', s.title,
        'url', s.url,
        'publisher', s.publisher,
        'published_on', s.published_on,
        'work_type', s.work_type,
        'isbn', s.isbn,
        'citation', s.citation_text,
        'authors', (select coalesce(jsonb_agg(cr.name order by cr.name), '[]')
                    from public.source_creator sc join public.creator cr on cr.id = sc.creator_id
                    where sc.source_id = s.id))), '{}')
      from public.source s
      where s.id in (select (c ->> 'source')::bigint
                     from jsonb_array_elements(a.sections) sec,
                          jsonb_array_elements(sec -> 'paragraphs') para,
                          jsonb_array_elements(para) e,
                          jsonb_array_elements(e -> 'cites') c)))
  from public.article a
  join public.proposal p on p.id = a.proposal_id
  where a.slug = p_slug and a.status = 'published' and public.article_ok(a.sections)
$$;

create function public.articles() returns jsonb
language sql stable security definer set search_path = '' as $$
  select coalesce(jsonb_agg(jsonb_build_object(
    'slug', a.slug,
    'title', a.title,
    'published_at', p.decided_at,
    'sentences', (select count(*) from jsonb_array_elements(a.sections) sec,
                         jsonb_array_elements(sec -> 'paragraphs') para, jsonb_array_elements(para) e)
  ) order by a.title), '[]')
  from public.article a
  join public.proposal p on p.id = a.proposal_id
  where a.status = 'published' and public.article_ok(a.sections)
$$;

alter table public.article enable row level security;
create policy "served articles" on public.article for select to anon, authenticated using (
  status = 'published' and public.article_ok(sections));
revoke insert, update, delete on public.article from anon, authenticated;
