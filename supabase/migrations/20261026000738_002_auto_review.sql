-- Auto-review publishes a proposal unless it finds a reason a human must decide;
-- that reason waits here for the human (review.py shows it). Null: never flagged.
alter table public.proposal add column flag text;
