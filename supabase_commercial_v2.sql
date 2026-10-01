-- The Brink World — Commercial Physical Risk V2 foundation
-- Additive migration. Safe to run after:
--   supabase_commercial_products.sql
--   supabase_commercial_activation.sql
--
-- This migration does not change the existing V1 request/activation/report path.
-- It adds versioned facility profiles, contractual acceptance records,
-- auditable evidence items, risk findings, and report-governance metadata.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. Versioned facility profile 
-- ---------------------------------------------------------------------------

create table if not exists public.brink_facility_profiles (
  id uuid primary key default gen_random_uuid(),
  facility_id uuid not null references public.brink_facilities(id) on delete cascade,
  profile_version integer not null default 1,

  construction_type text,
  year_built integer,
  floors_above_ground integer,
  basement_present text,
  critical_equipment_level text,
  backup_power text,
  water_dependency text,
  cooling_dependency text,
  practical_access_routes text,

  roof_type text,
  drainage_protection text,
  previous_disruptions jsonb not null default '[]'::jsonb,
  critical_dependencies jsonb not null default '{}'::jsonb,
  resilience_measures jsonb not null default '{}'::jsonb,
  notes text,

  source text not null default 'client_declared',
  declared_by text,
  declared_at timestamptz,
  valid_from timestamptz not null default now(),
  valid_to timestamptz,
  is_current boolean not null default true,
  created_at timestamptz not null default now(),

  constraint brink_facility_profiles_version_unique
    unique (facility_id, profile_version),

  constraint brink_facility_profiles_basement_check
    check (basement_present is null or basement_present in ('yes','no','unknown')),

  constraint brink_facility_profiles_water_check
    check (water_dependency is null or water_dependency in ('low','moderate','high','critical','unknown')),

  constraint brink_facility_profiles_cooling_check
    check (cooling_dependency is null or cooling_dependency in ('low','moderate','high','critical','unknown'))
);

create index if not exists brink_facility_profiles_current_idx
  on public.brink_facility_profiles(facility_id, is_current, profile_version desc);

-- ---------------------------------------------------------------------------
-- 2. Legal / contractual acceptance audit
-- ---------------------------------------------------------------------------

create table if not exists public.brink_contract_acceptances (
  id uuid primary key default gen_random_uuid(),
  facility_id uuid references public.brink_facilities(id) on delete set null,
  subscription_id uuid references public.brink_monitoring_subscriptions(id) on delete set null,

  contact_name text,
  contact_email text,
  organization_name text,

  terms_version text not null,
  privacy_version text not null,
  authority_confirmed boolean not null default false,
  client_declaration_confirmed boolean not null default false,
  accepted_at timestamptz not null default now(),

  -- Store only a non-reversible request fingerprint if the application later
  -- chooses to record one. Avoid persisting raw IP/user-agent unless legally
  -- justified and disclosed in the Privacy Notice.
  request_fingerprint_hash text,

  acceptance_context jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists brink_contract_acceptances_subscription_idx
  on public.brink_contract_acceptances(subscription_id, accepted_at desc);

create index if not exists brink_contract_acceptances_facility_idx
  on public.brink_contract_acceptances(facility_id, accepted_at desc);

-- ---------------------------------------------------------------------------
-- 3. Evidence ledger
-- ---------------------------------------------------------------------------

create table if not exists public.brink_evidence_items (
  id uuid primary key default gen_random_uuid(),
  report_run_id uuid references public.brink_report_runs(id) on delete cascade,
  facility_id uuid not null references public.brink_facilities(id) on delete cascade,

  hazard_type text,
  metric_code text not null,
  metric_name text not null,

  value_numeric double precision,
  value_text text,
  unit text,

  source_name text not null,
  source_dataset text,
  source_reference text,
  source_version text,

  evidence_class text not null,
  retrieved_at timestamptz not null default now(),
  observation_start timestamptz,
  observation_end timestamptz,

  spatial_resolution text,
  temporal_resolution text,

  scenario text,
  time_horizon text,

  confidence text not null default 'unresolved',
  confidence_reason text,
  limitations text,

  raw_evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),

  constraint brink_evidence_items_class_check
    check (evidence_class in (
      'observed',
      'official_warning',
      'modelled',
      'mapped',
      'client_declared',
      'client_verified',
      'interpreted'
    )),

  constraint brink_evidence_items_confidence_check
    check (confidence in ('high','medium','low','unresolved'))
);

create index if not exists brink_evidence_items_report_idx
  on public.brink_evidence_items(report_run_id, hazard_type, metric_code);

create index if not exists brink_evidence_items_facility_idx
  on public.brink_evidence_items(facility_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 4. Brink risk findings
-- ---------------------------------------------------------------------------

create table if not exists public.brink_risk_findings (
  id uuid primary key default gen_random_uuid(),
  report_run_id uuid references public.brink_report_runs(id) on delete cascade,
  facility_id uuid not null references public.brink_facilities(id) on delete cascade,

  hazard_type text not null,
  materiality text not null,
  facility_sensitivity text not null default 'unresolved',

  finding_text text not null,
  reasoning_summary text,
  operational_consequence text,

  confidence text not null default 'unresolved',
  confidence_reason text,

  action_type text,
  recommended_action text,
  specialist_review_required boolean not null default false,

  gross_risk_context text,
  residual_concern text,

  methodology_version text,
  materiality_rules_version text,
  confidence_rules_version text,

  created_at timestamptz not null default now(),

  constraint brink_risk_findings_materiality_check
    check (materiality in ('material','monitor','low_relevance','evidence_gap')),

  constraint brink_risk_findings_sensitivity_check
    check (facility_sensitivity in ('low','moderate','high','unresolved')),

  constraint brink_risk_findings_confidence_check
    check (confidence in ('high','medium','low','unresolved')),

  constraint brink_risk_findings_action_check
    check (
      action_type is null or
      action_type in ('verify','monitor','mitigate','specialist_assessment','routine_reassessment')
    ),

  constraint brink_risk_findings_logical_check
    check (
      not (materiality = 'evidence_gap' and confidence <> 'unresolved')
      and not (materiality = 'low_relevance' and confidence = 'unresolved')
    )
);

create index if not exists brink_risk_findings_report_idx
  on public.brink_risk_findings(report_run_id, hazard_type);

create index if not exists brink_risk_findings_facility_idx
  on public.brink_risk_findings(facility_id, created_at desc);

create table if not exists public.brink_risk_finding_evidence (
  finding_id uuid not null references public.brink_risk_findings(id) on delete cascade,
  evidence_id uuid not null references public.brink_evidence_items(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (finding_id, evidence_id)
);

-- ---------------------------------------------------------------------------
-- 5. Reproducibility / governance metadata on each report run
-- ---------------------------------------------------------------------------

alter table public.brink_report_runs
  add column if not exists facility_profile_id uuid
    references public.brink_facility_profiles(id) on delete set null;

alter table public.brink_report_runs
  add column if not exists report_methodology_version text;

alter table public.brink_report_runs
  add column if not exists materiality_rules_version text;

alter table public.brink_report_runs
  add column if not exists confidence_rules_version text;

alter table public.brink_report_runs
  add column if not exists evidence_schema_version text;

alter table public.brink_report_runs
  add column if not exists engine_commit_sha text;

alter table public.brink_report_runs
  add column if not exists terms_version text;

-- ---------------------------------------------------------------------------
-- 6. RLS and service-role access
-- ---------------------------------------------------------------------------

alter table public.brink_facility_profiles enable row level security;
alter table public.brink_contract_acceptances enable row level security;
alter table public.brink_evidence_items enable row level security;
alter table public.brink_risk_findings enable row level security;
alter table public.brink_risk_finding_evidence enable row level security;

grant select, insert, update, delete on public.brink_facility_profiles to service_role;
grant select, insert, update, delete on public.brink_contract_acceptances to service_role;
grant select, insert, update, delete on public.brink_evidence_items to service_role;
grant select, insert, update, delete on public.brink_risk_findings to service_role;
grant select, insert, update, delete on public.brink_risk_finding_evidence to service_role;

-- No anonymous access is granted. Client-facing access should remain behind
-- authenticated/signed application endpoints.
