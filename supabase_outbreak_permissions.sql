-- The Brink World — Outbreak intelligence permissions
-- Run once in Supabase SQL Editor.

grant usage on schema public to anon, authenticated, service_role;

grant select on table public.outbreak_signals to anon, authenticated;
grant select, insert, update, delete on table public.outbreak_signals to service_role;

grant select, insert, update, delete on table public.outbreak_signal_history to service_role;
grant select, insert, update, delete on table public.outbreak_source_runs to service_role;

alter table public.outbreak_signals enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'outbreak_signals'
      and policyname = 'Public read active outbreak signals'
  ) then
    create policy "Public read active outbreak signals"
      on public.outbreak_signals
      for select
      to anon, authenticated
      using (is_active = true);
  end if;
end
$$;
 
