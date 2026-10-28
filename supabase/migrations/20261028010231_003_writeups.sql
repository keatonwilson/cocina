-- Writeups: a few plain sentences about an entity, made only from its published
-- claims, each sentence citing the claims it states. Agents write them; like
-- claims they publish only through accept_proposal (auto-review or a human).

create table public.writeup (
  id bigint generated always as identity primary key,
  entity_id bigint not null references public.entity on delete cascade,
  -- [{"text": "…", "claims": [claim ids]}]; the first sentence stands alone as the short description
  sentences jsonb not null
    check (jsonb_typeof(sentences) = 'array' and jsonb_array_length(sentences) between 1 and 6),
  status public.kb_status not null default 'proposed',
  proposal_id bigint references public.proposal,
  created_at timestamptz not null default now()
);
-- One live writeup per entity; accepting a new one retires the old
create unique index writeup_live on public.writeup (entity_id) where status = 'published';

-- Every sentence has text and cites at least one claim, and every cited claim is
-- the entity's own, published and still credited. Takes the row's columns rather
-- than its id, so the RLS policy below can use it without reading writeup again.
-- (The CASEs keep a malformed element from raising instead of failing the check:
-- SQL doesn't promise to short-circuit.)
create function public.sentences_ok(p_entity bigint, p_sentences jsonb) returns boolean
language sql stable as $$
  with s as (
    select e from jsonb_array_elements(
      case when jsonb_typeof(p_sentences) = 'array' then p_sentences else '[]' end) e
  )
  select exists (select 1 from s)
    and not exists (
      select 1 from s
      where coalesce(jsonb_typeof(e -> 'text'), '') <> 'string' or btrim(e ->> 'text') = ''
         or case when jsonb_typeof(e -> 'claims') = 'array' then jsonb_array_length(e -> 'claims') = 0
                 else true end)
    and not exists (
      select 1 from s, jsonb_array_elements(
        case when jsonb_typeof(e -> 'claims') = 'array' then e -> 'claims' else '[]' end) cid
      where case when jsonb_typeof(cid) = 'number' then not exists (
              select 1 from public.claim c
              where c.id = (cid::text)::numeric and c.entity_id = p_entity
                and c.status = 'published' and public.has_credit(c.id))
            else true end)
$$;

create function public.trg_writeup_ok() returns trigger
language plpgsql as $$
declare
  w public.writeup;
begin
  select * into w from public.writeup where id = new.id;
  if not found or w.status <> 'published' then return null; end if;
  if not exists (select 1 from public.entity where id = w.entity_id and status = 'published') then
    raise exception 'writeup % is published without its entity', w.id;
  end if;
  if not public.sentences_ok(w.entity_id, w.sentences) then
    raise exception 'writeup % is published with a sentence that cites no published, credited claim of its entity', w.id;
  end if;
  return null;
end $$;

create constraint trigger writeup_ok after insert or update on public.writeup
  deferrable initially deferred for each row execute function public.trg_writeup_ok();

-- ---------------------------------------------------------------------------
-- The gate learns writeups
-- ---------------------------------------------------------------------------

create or replace function public.accept_proposal(p_id bigint, p_reviewer text) returns void
language plpgsql as $$
declare
  p public.proposal;
begin
  select * into p from public.proposal where id = p_id for update;
  if not found or p.status <> 'open' then
    raise exception 'proposal % is not open', p_id;
  end if;
  -- ponytail: add and change only; retire and merge arrive with the auditor and linker
  if p.op not in ('add', 'change') then
    raise exception 'op % is not implemented yet', p.op;
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

  update public.proposal set status = 'accepted', decided_by = p_reviewer, decided_at = now()
  where id = p_id;
end $$;

create or replace function public.reject_proposal(p_id bigint, p_reviewer text, p_reason text) returns void
language plpgsql as $$
begin
  if not exists (select 1 from public.proposal where id = p_id and status = 'open') then
    raise exception 'proposal % is not open', p_id;
  end if;
  update public.writeup set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.alias set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.evidence set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.claim set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.entity set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.proposal set status = 'rejected', decided_by = p_reviewer, decided_at = now(), reason = p_reason
  where id = p_id;
end $$;

-- ---------------------------------------------------------------------------
-- Reads: the card gains claim ids and the live writeup
-- ---------------------------------------------------------------------------

create or replace function public.card(p_entity bigint) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'slug', e.slug,
    'kind', e.kind,
    'name', e.name,
    'aliases', coalesce((
      select jsonb_agg(a.text order by a.text) from public.alias a
      where a.entity_id = e.id and a.status = 'published' and public.norm(a.text) <> public.norm(e.name)
        and (a.claim_id is null or public.has_credit(a.claim_id))
    ), '[]'),
    'claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id', c.id,
        'predicate', c.predicate,
        'value', c.value,
        'object', (select jsonb_build_object('slug', o.slug, 'name', o.name)
                   from public.entity o where o.id = c.object_id and public.served(o.id)),
        'confidence', ev.independent,   -- distinct sites or works that agree
        'verified_at', ev.verified_at,
        'sources', ev.sources
      ) order by c.predicate = 'note', c.predicate, c.id)
      from public.claim c
      cross join lateral (
        select
          jsonb_agg(jsonb_build_object(
            'title', s.title,
            'url', s.url,
            'publisher', s.publisher,
            'published_on', s.published_on,
            'work_type', s.work_type,
            'license', s.license,
            'authors', (select coalesce(jsonb_agg(cr.name order by cr.name), '[]')
                        from public.source_creator sc join public.creator cr on cr.id = sc.creator_id
                        where sc.source_id = s.id),
            'quote', ev.quote,
            'locator', ev.locator
          ) order by ev.fetched_at) as sources,
          count(distinct coalesce(substring(s.url from '^[a-z]+://(?:www\.)?([^/]+)'),
                                  lower(s.publisher), lower(s.title))) as independent,
          max(ev.fetched_at) as verified_at
        from public.evidence ev
        join public.source s on s.id = ev.source_id
        where ev.claim_id = c.id and ev.status = 'published' and public.credited(ev.source_id)
      ) ev
      where c.entity_id = e.id and c.status = 'published' and public.has_credit(c.id)
    ), '[]'),
    -- [{text, claims: [ids from "claims" above]}], or null; hidden once a cited claim loses its credit
    'writeup', (select w.sentences from public.writeup w
                where w.entity_id = e.id and w.status = 'published' and public.sentences_ok(e.id, w.sentences)),
    -- Incoming links: "chavindeca is a variant_of this"
    'linked_from', coalesce((
      select jsonb_agg(jsonb_build_object('slug', f.slug, 'name', f.name, 'predicate', c.predicate)
                       order by f.slug)
      from public.claim c join public.entity f on f.id = c.entity_id
      where c.object_id = e.id and c.status = 'published' and public.served(f.id) and public.has_credit(c.id)
    ), '[]')
  )
  from public.entity e
  where e.id = p_entity and public.served(e.id)
$$;

-- ---------------------------------------------------------------------------
-- Access: the same rule as card()
-- ---------------------------------------------------------------------------

alter table public.writeup enable row level security;
create policy "served writeups" on public.writeup for select to anon, authenticated using (
  status = 'published' and public.sentences_ok(entity_id, sentences)
  -- the entity's own policy decides whether it is served
  and exists (select 1 from public.entity e where e.id = writeup.entity_id));
revoke insert, update, delete on public.writeup from anon, authenticated;
