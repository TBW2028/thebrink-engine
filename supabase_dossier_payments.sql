-- The Brink World — paid Location Threat Dossier manual-verification workflow
-- Run after supabase_dossier_funnel.sql.
alter table public.dossier_requests
  
  add column if not exists order_code text,
  add column if not exists client_name text,
  add column if not exists organization text,
  add column if not exists purpose text,
  add column if not exists concern text,
  add column if not exists payment_method text,
  add column if not exists payment_reference text,
  add column if not exists payment_currency text,
  add column if not exists payment_amount numeric,
  add column if not exists payment_submitted_at timestamptz,
  add column if not exists payment_verified_at timestamptz,
  add column if not exists approval_token_hash text,
  add column if not exists approval_token_expires_at timestamptz,
  add column if not exists report_ref text,
  add column if not exists fulfilled_at timestamptz;

create unique index if not exists dossier_requests_order_code_uidx
  on public.dossier_requests(order_code)
  where order_code is not null;

create index if not exists dossier_requests_payment_status_idx
  on public.dossier_requests(payment_status, created_at desc);

grant select, insert, update, delete on public.dossier_requests to service_role;
