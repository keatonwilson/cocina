-- 001_core: entities, sourced claims, proposals, and the citation rules.
--
-- Every fact is a claim backed by evidence (a short quote from a credited
-- source). Agents and scripts only insert rows with status 'proposed';
-- accept_proposal() publishes them, and deferred constraint triggers refuse
-- the commit if anything published lacks credit. Consumers read published
-- rows only: RLS for raw REST, explicit filters in the read functions.

create extension if not exists unaccent with schema extensions;
create extension if not exists fuzzystrmatch with schema extensions;

-- The one name-matching rule: lowercase, accents stripped, whitespace collapsed.
create function public.norm(t text) returns text
language sql immutable parallel safe
as $$
  select btrim(regexp_replace(lower(extensions.unaccent('extensions.unaccent'::regdictionary, coalesce(t, ''))), '\s+', ' ', 'g'))
$$;

create type public.kb_status as enum ('proposed', 'published', 'rejected', 'retired');

create table public.run_log (
  id bigint generated always as identity primary key,
  agent text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  cost_usd numeric(10, 4) not null default 0,
  proposals int not null default 0,
  errors jsonb not null default '[]'
);

create table public.proposal (
  id bigint generated always as identity primary key,
  op text not null default 'add' check (op in ('add', 'change', 'retire', 'merge')),
  author text not null,                -- agent id or reviewer
  rationale text,
  status text not null default 'open' check (status in ('open', 'accepted', 'rejected')),
  decided_by text,
  decided_at timestamptz,
  reason text,                         -- why it was rejected; fed back to the agent
  run_id bigint references public.run_log,
  created_at timestamptz not null default now()
);
create index on public.proposal (status) where status = 'open';

create table public.creator (
  id bigint generated always as identity primary key,
  name text not null,
  role text check (role in ('writer', 'historian', 'cook', 'photographer', 'institution')),
  url text,
  bio text,
  do_not_use boolean not null default false
);
-- ponytail: one creator per normalised name; namesakes get split by hand
create unique index creator_name_key on public.creator (public.norm(name));

-- Find or add a creator by name (the unique key is on norm(name), which
-- PostgREST upserts can't target)
create function public.creator_id(p_name text) returns bigint
language sql as $$
  insert into public.creator (name) values (btrim(p_name))
  on conflict ((public.norm(name))) do update set name = public.creator.name
  returning id
$$;

create table public.source (
  id bigint generated always as identity primary key,
  url text unique,
  title text not null,
  publisher text,
  work_type text not null default 'web page'
    check (work_type in ('book', 'article', 'interview', 'video', 'archive', 'web page', 'dataset')),
  published_on date,
  license text,
  content_hash text,                   -- the auditor re-fetches and compares
  fetched_at timestamptz,
  isbn text,
  pages text,
  citation_text text
);

create table public.source_creator (
  source_id bigint not null references public.source on delete cascade,
  creator_id bigint not null references public.creator,
  primary key (source_id, creator_id)
);

-- The copy quotes are checked against. Never served.
create table public.source_text (
  source_id bigint primary key references public.source on delete cascade,
  text text not null
);

create table public.entity (
  id bigint generated always as identity primary key,
  slug text not null unique check (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  kind text not null check (kind in ('dish', 'menu_category', 'protein_prep', 'cut', 'salsa', 'chile',
                                     'ingredient', 'technique', 'tortilla', 'region')),
  name text not null,
  domain text not null default 'mexican',
  note text,                           -- seed or curator text: a research hint, never in a card
  status public.kb_status not null default 'proposed',
  proposal_id bigint references public.proposal
);

-- The fixed predicate list per kind. `links`: the claim may point at another
-- entity (object_id), which is how edges are stored.
create table public.predicate (
  kind text not null,
  name text not null,
  links boolean not null default false,
  primary key (kind, name)
);

insert into public.predicate (kind, name, links)
select k, p, l
from unnest(array['dish', 'menu_category', 'protein_prep', 'salsa', 'tortilla']) k
cross join (values
  ('originates_in', true), ('uses', true), ('cooking_method', false), ('served_as', false),
  ('served_with', true), ('also_known_as', false), ('variant_of', true), ('history_note', false), ('note', false)
) v(p, l)
union all
select k, p, l
from unnest(array['chile', 'ingredient']) k
cross join (values
  ('originates_in', true), ('also_known_as', false), ('variant_of', true), ('dried_form_of', true),
  ('flavor', false), ('history_note', false), ('note', false)
) v(p, l)
union all
select 'technique', p, l from (values
  ('originates_in', true), ('uses', true), ('used_for', true), ('also_known_as', false),
  ('history_note', false), ('note', false)
) v(p, l)
union all
select 'cut', p, l from (values
  ('part_of', false), ('cooking_method', false), ('also_known_as', false), ('note', false)
) v(p, l)
union all
select 'region', p, l from (values
  ('part_of', true), ('also_known_as', false), ('note', false)
) v(p, l);

create table public.claim (
  id bigint generated always as identity primary key,
  entity_id bigint not null references public.entity on delete cascade,
  predicate text not null,
  value text not null check (length(value) between 1 and 500),
  object_id bigint references public.entity,
  supersedes bigint references public.claim,   -- a 'change': publishing this retires that
  status public.kb_status not null default 'proposed',
  proposal_id bigint references public.proposal
);
-- One live claim per fact, so agreeing sources become evidence on one claim
create unique index claim_fact_key on public.claim (entity_id, predicate, public.norm(value))
  where status in ('proposed', 'published');
create index on public.claim (object_id) where object_id is not null;

create table public.evidence (
  id bigint generated always as identity primary key,
  claim_id bigint not null references public.claim on delete cascade,
  source_id bigint not null references public.source,
  quote text not null check (length(quote) between 1 and 400),   -- quote little
  locator text,                        -- page, timestamp or section
  fetched_at timestamptz not null default now(),
  status public.kb_status not null default 'proposed',
  proposal_id bigint references public.proposal,
  unique (claim_id, source_id)
);

create table public.alias (
  id bigint generated always as identity primary key,
  entity_id bigint not null references public.entity on delete cascade,
  text text not null,
  lang text check (lang in ('es', 'en')),
  claim_id bigint references public.claim,     -- set for a sourced name; null for a search hint
  status public.kb_status not null default 'proposed',
  proposal_id bigint references public.proposal
);
-- Live names only, so a rejected alias doesn't block the same name proposed again
create unique index alias_key on public.alias (entity_id, public.norm(text))
  where status in ('proposed', 'published');

-- ---------------------------------------------------------------------------
-- Rules checked as rows are written
-- ---------------------------------------------------------------------------

create function public.check_claim_predicate() returns trigger
language plpgsql as $$
declare
  p public.predicate;
begin
  select pr.* into p from public.predicate pr
  join public.entity e on e.kind = pr.kind
  where e.id = new.entity_id and pr.name = new.predicate;
  if not found then
    raise exception 'predicate % is not allowed for entity %', new.predicate, new.entity_id;
  end if;
  if new.object_id is not null and not p.links then
    raise exception 'predicate % cannot link to an entity', new.predicate;
  end if;
  return new;
end $$;

create trigger claim_predicate before insert or update of entity_id, predicate, object_id on public.claim
for each row execute function public.check_claim_predicate();

-- Citation rules, checked at commit so a whole proposal is judged at once.

-- A source counts as credit when it names a person or a publisher and none of
-- its creators opted out
create function public.credited(p_source bigint) returns boolean
language sql stable as $$
  select exists (
      select 1 from public.source s
      where s.id = p_source
        and (s.publisher is not null or exists (select 1 from public.source_creator sc where sc.source_id = s.id))
    )
    and not exists (
      select 1 from public.source_creator sc
      join public.creator cr on cr.id = sc.creator_id
      where sc.source_id = p_source and cr.do_not_use
    )
$$;

-- A published claim is served only while a published quote behind it is still
-- credited, so a creator who opts out after publication drops out of every card
create function public.has_credit(p_claim bigint) returns boolean
language sql stable as $$
  select exists (select 1 from public.evidence
                 where claim_id = p_claim and status = 'published' and public.credited(source_id))
$$;

-- What consumers see: a published entity with at least one claim still credited
create function public.served(p_entity bigint) returns boolean
language sql stable as $$
  select exists (select 1 from public.entity where id = p_entity and status = 'published')
    and exists (select 1 from public.claim c
                where c.entity_id = p_entity and c.status = 'published' and public.has_credit(c.id))
$$;

create function public.assert_claim_ok(p_claim bigint) returns void
language plpgsql as $$
declare
  c public.claim;
begin
  select * into c from public.claim where id = p_claim;
  if not found then return; end if;

  if c.status = 'published' then
    if not public.has_credit(c.id) then
      raise exception 'claim % is published without credited evidence', c.id;
    end if;
    if c.object_id is not null
       and not exists (select 1 from public.entity where id = c.object_id and status = 'published') then
      raise exception 'claim % links to unpublished entity %', c.id, c.object_id;
    end if;
  end if;

  -- An entity can't stay published once its last claim goes
  if exists (select 1 from public.entity where id = c.entity_id and status = 'published')
     and not exists (select 1 from public.claim where entity_id = c.entity_id and status = 'published') then
    raise exception 'entity % would be published with no published claim', c.entity_id;
  end if;
end $$;

create function public.trg_claim_ok() returns trigger
language plpgsql as $$
begin
  perform public.assert_claim_ok(new.id);
  return null;
end $$;

create function public.trg_evidence_ok() returns trigger
language plpgsql as $$
declare
  ev public.evidence;
begin
  -- Only published quotes carry credit. A proposed or rejected one can't take any
  -- away, and must stay writable so a claim that lost its credit can win it back
  if tg_op = 'INSERT' then
    if new.status <> 'published' then return null; end if;
  elsif tg_op = 'UPDATE' then
    if new.status <> 'published' and old.status <> 'published' then return null; end if;
  elsif old.status <> 'published' then
    return null;
  end if;
  -- Every published quote needs credit, not just one per claim
  select * into ev from public.evidence where id = coalesce(new.id, old.id);
  if found and ev.status = 'published' and not public.credited(ev.source_id) then
    raise exception 'evidence % is published without credit, or from a creator who opted out', ev.id;
  end if;
  perform public.assert_claim_ok(coalesce(new.claim_id, old.claim_id));
  return null;
end $$;

create function public.trg_entity_ok() returns trigger
language plpgsql as $$
begin
  if exists (select 1 from public.entity where id = new.id and status = 'published')
     and not exists (select 1 from public.claim where entity_id = new.id and status = 'published') then
    raise exception 'entity % is published with no published claim', new.id;
  end if;
  return null;
end $$;

create function public.trg_alias_ok() returns trigger
language plpgsql as $$
begin
  if exists (
    select 1 from public.alias a
    join public.entity e on e.id = a.entity_id
    left join public.claim c on c.id = a.claim_id
    where a.id = new.id and a.status = 'published'
      and (e.status <> 'published' or (a.claim_id is not null and c.status <> 'published'))
  ) then
    raise exception 'alias % is published without its entity or claim', new.id;
  end if;
  return null;
end $$;

create constraint trigger claim_ok after insert or update on public.claim
  deferrable initially deferred for each row execute function public.trg_claim_ok();
create constraint trigger evidence_ok after insert or update or delete on public.evidence
  deferrable initially deferred for each row execute function public.trg_evidence_ok();
create constraint trigger entity_ok after insert or update on public.entity
  deferrable initially deferred for each row execute function public.trg_entity_ok();
create constraint trigger alias_ok after insert or update on public.alias
  deferrable initially deferred for each row execute function public.trg_alias_ok();

-- ---------------------------------------------------------------------------
-- The one gate to published data
-- ---------------------------------------------------------------------------

create function public.accept_proposal(p_id bigint, p_reviewer text) returns void
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

  update public.proposal set status = 'accepted', decided_by = p_reviewer, decided_at = now()
  where id = p_id;
end $$;

create function public.reject_proposal(p_id bigint, p_reviewer text, p_reason text) returns void
language plpgsql as $$
begin
  if not exists (select 1 from public.proposal where id = p_id and status = 'open') then
    raise exception 'proposal % is not open', p_id;
  end if;
  update public.alias set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.evidence set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.claim set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.entity set status = 'rejected' where proposal_id = p_id and status = 'proposed';
  update public.proposal set status = 'rejected', decided_by = p_reviewer, decided_at = now(), reason = p_reason
  where id = p_id;
end $$;

-- ---------------------------------------------------------------------------
-- Reads: published data only, whoever calls
-- ---------------------------------------------------------------------------

create function public.card(p_entity bigint) returns jsonb
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

create function public.cards() returns setof jsonb
language sql stable as $$
  select public.card(id) from public.entity where public.served(id) order by slug
$$;

create function public.get_entity(p_slug text) returns jsonb
language sql stable as $$
  select public.card(id) from public.entity where slug = p_slug and public.served(id)
$$;

-- Name and alias match: exact, then typo-tolerant (1 edit from 4 letters,
-- 2 from 7, the same thresholds as cartoTaco's search.js), then whole-word
-- containment ranked last. p_all also searches proposed entities (pipeline
-- dedup); RLS still hides them from anon.
create function public.search_entities(q text, p_kind text default null, p_all boolean default false,
                                       p_limit int default 8)
returns table (slug text, name text, kind text, matched text, distance int)
language sql stable as $$
  with names as (
    select e.slug, e.name, e.kind, e.name as label from public.entity e
    where public.served(e.id) or (p_all and e.status in ('published', 'proposed'))
    union all
    select e.slug, e.name, e.kind, a.text from public.alias a
    join public.entity e on e.id = a.entity_id
    where (public.served(e.id) and a.status = 'published' and (a.claim_id is null or public.has_credit(a.claim_id)))
       or (p_all and e.status in ('published', 'proposed') and a.status in ('published', 'proposed'))
  ),
  scored as (
    select n.*,
      case
        when public.norm(n.label) = public.norm(q) then 0
        when extensions.levenshtein(left(public.norm(n.label), 255), left(public.norm(q), 255))
             <= case when length(public.norm(q)) >= 7 then 2 when length(public.norm(q)) >= 4 then 1 else 0 end
          then extensions.levenshtein(left(public.norm(n.label), 255), left(public.norm(q), 255))
        when length(public.norm(q)) >= 4
             and ' ' || public.norm(n.label) || ' ' like '% ' || public.norm(q) || ' %' then 50
      end as distance
    from names n
    where p_kind is null or n.kind = p_kind
  ),
  best as (
    select distinct on (s.slug) s.slug, s.name, s.kind, s.label, s.distance
    from scored s where s.distance is not null
    order by s.slug, s.distance
  )
  select b.slug, b.name, b.kind, b.label, b.distance from best b
  order by b.distance, b.name
  limit p_limit
$$;

create function public.related(p_slug text, p_predicate text default null, p_hops int default 1)
returns table (slug text, name text, kind text, predicate text, direction text, hops int)
language sql stable as $$
  with recursive walk (id, predicate, direction, hops, path) as (
    select e.id, null::text, null::text, 0, array[e.id]
    from public.entity e where e.slug = p_slug and public.served(e.id)
    union all
    select n.id, n.predicate, n.direction, w.hops + 1, w.path || n.id
    from walk w
    cross join lateral (
      select c.object_id as id, c.predicate, 'out' as direction from public.claim c
      where c.entity_id = w.id and c.object_id is not null and c.status = 'published' and public.has_credit(c.id)
      union all
      select c.entity_id, c.predicate, 'in' from public.claim c
      where c.object_id = w.id and c.status = 'published' and public.has_credit(c.id)
    ) n
    where w.hops < least(greatest(p_hops, 1), 2)
      and not n.id = any (w.path)
      and public.served(n.id)   -- never walk through an entity consumers can't see
      and (p_predicate is null or n.predicate = p_predicate)
  )
  select distinct on (e.slug) e.slug, e.name, e.kind, w.predicate, w.direction, w.hops
  from walk w join public.entity e on e.id = w.id and public.served(e.id)
  where w.hops > 0
  order by e.slug, w.hops
$$;

-- When the published knowledge last changed. Definer: proposals are private.
create function public.kb_version() returns timestamptz
language sql stable security definer set search_path = '' as $$
  select max(decided_at) from public.proposal where status = 'accepted'
$$;

-- ---------------------------------------------------------------------------
-- Access
-- ---------------------------------------------------------------------------

alter table public.entity enable row level security;
alter table public.claim enable row level security;
alter table public.evidence enable row level security;
alter table public.alias enable row level security;
alter table public.source enable row level security;
alter table public.source_creator enable row level security;
alter table public.creator enable row level security;
alter table public.predicate enable row level security;
alter table public.source_text enable row level security;   -- no policy: service role only
alter table public.proposal enable row level security;      -- no policy: service role only
alter table public.run_log enable row level security;       -- no policy: service role only

-- The same rules as the read functions, so raw REST serves no more than they do:
-- credited quotes, claims that still have one, entities with such a claim (spelled
-- out rather than served(), which reads entity and would recurse into this policy)
create policy "served entities" on public.entity for select to anon, authenticated using (
  status = 'published'
  and exists (select 1 from public.claim c
              where c.entity_id = entity.id and c.status = 'published' and public.has_credit(c.id)));
create policy "credited claims" on public.claim for select to anon, authenticated using (
  status = 'published' and public.has_credit(id));
create policy "credited evidence" on public.evidence for select to anon, authenticated using (
  status = 'published' and public.credited(source_id));
create policy "served aliases" on public.alias for select to anon, authenticated using (
  status = 'published' and (claim_id is null or public.has_credit(claim_id))
  -- the entity's own policy decides whether it is served
  and exists (select 1 from public.entity e where e.id = alias.entity_id));
create policy "sources" on public.source for select to anon, authenticated using (true);
create policy "source creators" on public.source_creator for select to anon, authenticated using (true);
create policy "creators" on public.creator for select to anon, authenticated using (true);
create policy "predicates" on public.predicate for select to anon, authenticated using (true);

-- Writes go through the service role only
revoke insert, update, delete on all tables in schema public from anon, authenticated;
revoke execute on function public.accept_proposal(bigint, text) from public, anon, authenticated;
revoke execute on function public.reject_proposal(bigint, text, text) from public, anon, authenticated;
revoke execute on function public.creator_id(text) from public, anon, authenticated;
