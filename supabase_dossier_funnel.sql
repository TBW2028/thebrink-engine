-- The Brink World: free Location Threat Snapshot funnel
-- Run once in Supabase SQL Editor before deploying the Worker routes.

create extension if not exists pgcrypto;

create table if not exists public.dossier_leads (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  email_verified boolean not null default false,
  sample_count integer not null default 0 check (sample_count between 0 and 2),
  marketing_opt_in boolean not null default false,
  marketing_consent_at timestamptz,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table if not exists public.dossier_verifications (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  code_hash text not null,
  requested_location text not null,
  requested_lat double precision,
  requested_lon double precision,
  requested_country text,
  requested_country_code text,
  marketing_opt_in boolean not null default false,
  ip_hash text,
  ip_country text,
  ip_region text,
  ip_city text,
  expires_at timestamptz not null,
  attempts integer not null default 0,
  consumed_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.dossier_requests (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid references public.dossier_leads(id) on delete set null,
  email text not null,
  request_type text not null check (request_type in ('free_sample', 'paid_full')),
  sample_number integer,
  requested_location text not null,
  requested_lat double precision,
  requested_lon double precision,
  requested_country text,
  requested_country_code text,
  ip_hash text,
  ip_country text,
  ip_region text,
  ip_city text,
  status text not null default 'created',
  sample_snapshot jsonb,
  currency text,
  amount_minor bigint,
  payment_status text,
  created_at timestamptz not null default now(),
  delivered_at timestamptz
);

create index if not exists dossier_verifications_email_created_idx
  on public.dossier_verifications (email, created_at desc);

create index if not exists dossier_verifications_ip_created_idx
  on public.dossier_verifications (ip_hash, created_at desc)
  where ip_hash is not null;

create index if not exists dossier_requests_email_created_idx
  on public.dossier_requests (email, created_at desc);

create index if not exists dossier_requests_ip_created_idx
  on public.dossier_requests (ip_hash, created_at desc)
  where ip_hash is not null;

alter table public.dossier_leads enable row level security;
alter table public.dossier_verifications enable row level security;
alter table public.dossier_requests enable row level security;

revoke all on public.dossier_leads from anon, authenticated;
revoke all on public.dossier_verifications from anon, authenticated;
revoke all on public.dossier_requests from anon, authenticated;

grant usage on schema public to service_role;
grant select, insert, update, delete on public.dossier_leads to service_role;
grant select, insert, update, delete on public.dossier_verifications to service_role;
grant select, insert, update, delete on public.dossier_requests to service_role;

create or replace function public.claim_dossier_sample(
  p_email text,
  p_requested_location text,
  p_requested_lat double precision,
  p_requested_lon double precision,
  p_requested_country text,
  p_requested_country_code text,
  p_ip_hash text,
  p_ip_country text,
  p_ip_region text,
  p_ip_city text,
  p_marketing_opt_in boolean
)
returns table (
  request_id uuid,
  sample_number integer,
  remaining_free integer
)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_email text := lower(trim(p_email));
  v_lead public.dossier_leads%rowtype;
  v_request_id uuid;
  v_sample_number integer;
begin
  insert into public.dossier_leads (
    email,
    email_verified,
    marketing_opt_in,
    marketing_consent_at,
    first_seen_at,
    last_seen_at
  )
  values (
    v_email,
    true,
    coalesce(p_marketing_opt_in, false),
    case when coalesce(p_marketing_opt_in, false) then now() else null end,
    now(),
    now()
  )
  on conflict (email) do update
  set
    email_verified = true,
    last_seen_at = now(),
    marketing_opt_in = public.dossier_leads.marketing_opt_in or excluded.marketing_opt_in,
    marketing_consent_at = case
      when public.dossier_leads.marketing_opt_in then public.dossier_leads.marketing_consent_at
      when excluded.marketing_opt_in then now()
      else public.dossier_leads.marketing_consent_at
    end;

  select *
  into v_lead
  from public.dossier_leads
  where email = v_email
  for update;

  if v_lead.sample_count >= 2 then
    raise exception 'free_sample_limit_reached' using errcode = 'P0001';
  end if;

  v_sample_number := v_lead.sample_count + 1;

  update public.dossier_leads
  set sample_count = v_sample_number, last_seen_at = now()
  where id = v_lead.id;

  insert into public.dossier_requests (
    lead_id,
    email,
    request_type,
    sample_number,
    requested_location,
    requested_lat,
    requested_lon,
    requested_country,
    requested_country_code,
    ip_hash,
    ip_country,
    ip_region,
    ip_city,
    status,
    payment_status
  )
  values (
    v_lead.id,
    v_email,
    'free_sample',
    v_sample_number,
    p_requested_location,
    p_requested_lat,
    p_requested_lon,
    p_requested_country,
    p_requested_country_code,
    p_ip_hash,
    p_ip_country,
    p_ip_region,
    p_ip_city,
    'generating',
    'not_required'
  )
  returning id into v_request_id;

  return query
  select v_request_id, v_sample_number, 2 - v_sample_number;
end;
$$;

revoke all on function public.claim_dossier_sample(
  text, text, double precision, double precision, text, text,
  text, text, text, text, boolean
) from public, anon, authenticated;

grant execute on function public.claim_dossier_sample(
  text, text, double precision, double precision, text, text,
  text, text, text, text, boolean
) to service_role;
