-- The Brink World — purpose-specific paid dossier context
alter table public.dossier_requests
  add column if not exists purpose_details jsonb not null default '{}'::jsonb;

grant select, insert, update, delete on public.dossier_requests to service_role;
