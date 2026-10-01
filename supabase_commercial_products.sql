-- The Brink World commercial monitoring foundation
-- Run once in Supabase SQL Editor.

create extension if not exists pgcrypto;

create table if not exists public.brink_facilities (
  id uuid primary key default gen_random_uuid(),
  organization_name text,
  facility_name text not null,
  location_label text not null,
  latitude double precision,
  longitude double precision,
  country text,
  country_code text,
  facility_type text,
  critical_function text,
  dependencies jsonb not null default '{}'::jsonb,
  contact_name text,
  contact_email text,
  status text not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.brink_monitoring_subscriptions (
  id uuid primary key default gen_random_uuid(),
  facility_id uuid not null references public.brink_facilities(id) on delete cascade,
  product_type text not null,
  cadence text not null default 'monthly',
  status text not null default 'pilot',
  started_at timestamptz not null default now(),
  renews_at timestamptz,
  last_report_at timestamptz,
  next_report_at timestamptz,
  commercial_terms jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.brink_report_runs (
  id uuid primary key default gen_random_uuid(),
  facility_id uuid references public.brink_facilities(id) on delete set null,
  subscription_id uuid references public.brink_monitoring_subscriptions(id) on delete set null,
  product_type text not null,
  report_ref text,
  evidence_as_of timestamptz,
  report_status text not null default 'queued',
  evidence_summary jsonb not null default '{}'::jsonb,
  output_location text,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

create table if not exists public.brink_material_changes (
  id uuid primary key default gen_random_uuid(),
  facility_id uuid not null references public.brink_facilities(id) on delete cascade,
  detected_at timestamptz not null default now(),
  signal_type text not null,
  source text,
  severity text,
  headline text not null,
  evidence jsonb not null default '{}'::jsonb,
  acknowledged_at timestamptz
);

create index if not exists brink_facilities_country_idx
  on public.brink_facilities(country_code);

create index if not exists brink_subscriptions_next_idx
  on public.brink_monitoring_subscriptions(status, next_report_at);

create index if not exists brink_material_changes_facility_idx
  on public.brink_material_changes(facility_id, detected_at desc);

alter table public.brink_facilities enable row level security;
alter table public.brink_monitoring_subscriptions enable row level security;
alter table public.brink_report_runs enable row level security;
alter table public.brink_material_changes enable row level security;

grant select, insert, update, delete on public.brink_facilities to service_role;
grant select, insert, update, delete on public.brink_monitoring_subscriptions to service_role;
grant select, insert, update, delete on public.brink_report_runs to service_role;
grant select, insert, update, delete on public.brink_material_changes to service_role;

-- No anonymous access is granted. Customer-facing access should be mediated by
-- authenticated API endpoints or signed links rather than direct table access.
