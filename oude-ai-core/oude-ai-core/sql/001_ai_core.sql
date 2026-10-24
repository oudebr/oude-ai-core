-- Tabelas usadas pelo oude-ai-core. Rode no SQL Editor (idempotente).
-- Pré-requisito: org_integrations (org_id, provider text, encrypted_credentials jsonb) e a função
-- public.minhas_orgs() do script 20261022210000.

-- Idempotência: uma linha por ação já executada (ex.: 'estoque:<id>:2026-10-22').
create table if not exists public.ai_execucoes (
  org_id     uuid not null,
  chave      text not null,
  created_at timestamptz not null default now(),
  primary key (org_id, chave)
);
alter table public.ai_execucoes enable row level security;   -- sem política: só service_role
revoke all on public.ai_execucoes from anon, authenticated;
grant all on public.ai_execucoes to service_role;

-- Fallback: avisos do agente no painel do CRM (quando o lojista não tem WhatsApp).
create table if not exists public.ai_notificacoes (
  id         uuid primary key default gen_random_uuid(),
  org_id     uuid not null,
  tipo       text not null,
  titulo     text not null,
  mensagem   text not null,
  lida       boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists ai_notificacoes_org_idx on public.ai_notificacoes (org_id, created_at desc);
alter table public.ai_notificacoes enable row level security;

drop policy if exists ai_notificacoes_select on public.ai_notificacoes;
create policy ai_notificacoes_select on public.ai_notificacoes
  for select to authenticated using (org_id in (select public.minhas_orgs()));
drop policy if exists ai_notificacoes_marcar_lida on public.ai_notificacoes;
create policy ai_notificacoes_marcar_lida on public.ai_notificacoes
  for update to authenticated
  using (org_id in (select public.minhas_orgs())) with check (org_id in (select public.minhas_orgs()));
revoke all on public.ai_notificacoes from anon;
revoke insert, delete, truncate on public.ai_notificacoes from authenticated;
revoke update on public.ai_notificacoes from authenticated;
grant update (lida) on public.ai_notificacoes to authenticated;   -- o lojista só marca como lida
grant all on public.ai_notificacoes to service_role;

notify pgrst, 'reload schema';
