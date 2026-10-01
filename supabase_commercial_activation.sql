-- The Brink World commercial activation workflow
-- Run once after supabase_commercial_products.sql.

alter table public.brink_monitoring_subscriptions
  add column if not exists approval_token_hash text,
  add column if not exists approval_token_expires_at timestamptz,
  add column if not exists requested_at timestamptz default now(),
  add column if not exists approved_at timestamptz,
  add column if not exists approved_by text;

create index if not exists brink_subscriptions_status_idx
  on public.brink_monitoring_subscriptions(status, requested_at desc);
