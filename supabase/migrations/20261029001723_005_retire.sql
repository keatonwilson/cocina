-- Retire: take published claims down on a person's decision (python -m cocina.retire).
-- Nothing is deleted: the claims, their quotes and aliases stay as 'retired', so
-- history is kept, kb_version moves, and auto-review treats a retired fact like a
-- rejected one if research proposes it again.

alter table public.proposal add column retires bigint[];   -- the claims an op = 'retire' proposal takes down
alter table public.proposal add constraint retire_names_claims
  check ((op = 'retire') = (coalesce(cardinality(retires), 0) > 0));

create or replace function public.accept_proposal(p_id bigint, p_reviewer text) returns void
language plpgsql as $$
declare
  p public.proposal;
begin
  select * into p from public.proposal where id = p_id for update;
  if not found or p.status <> 'open' then
    raise exception 'proposal % is not open', p_id;
  end if;
  -- ponytail: merge arrives with the linker; retire --into covers duplicates until then
  if p.op not in ('add', 'change', 'retire') then
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

  -- A retire takes its claims down, with the names they back. A writeup citing one
  -- stops being served (sentences_ok) until `write --all` rewrites it.
  if p.op = 'retire' then
    update public.claim set status = 'retired' where id = any (p.retires) and status = 'published';
    update public.alias set status = 'retired' where claim_id = any (p.retires) and status = 'published';
    -- An entity left with no published claim goes too, with its names and writeup.
    -- ponytail: other entities' claims linking to it keep their text; card() drops the link
    with gone as (
      update public.entity e set status = 'retired'
      where e.status = 'published'
        and e.id in (select entity_id from public.claim where id = any (p.retires))
        and not exists (select 1 from public.claim c where c.entity_id = e.id and c.status = 'published')
      returning e.id
    ), names as (
      update public.alias set status = 'retired'
      where entity_id in (select id from gone) and status = 'published'
    )
    update public.writeup set status = 'retired'
    where entity_id in (select id from gone) and status = 'published';
  end if;

  update public.proposal set status = 'accepted', decided_by = p_reviewer, decided_at = now()
  where id = p_id;
end $$;
