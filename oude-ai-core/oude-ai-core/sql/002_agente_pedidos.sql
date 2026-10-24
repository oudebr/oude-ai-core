-- oude-ai-core, etapa 2: config do agente, histórico, auditoria, pedidos e pagamentos. Idempotente.
-- Pré-requisitos: org_membros e minhas_orgs() (script 20261022210000) e sql/001_ai_core.sql.
-- Todas as tabelas: RLS ligado; o navegador só LÊ (membros da org). Escrita: só service_role (este serviço).

-- 0) Índice único exigido para "salvar integração" (upsert por org_id + provider).
do $$
begin
  if to_regclass('public.org_integrations') is not null then
    if exists (select 1 from public.org_integrations group by org_id, provider having count(*) > 1) then
      raise notice 'ATENÇÃO: org_integrations tem (org_id, provider) duplicado. Remova os duplicados e rode de novo.';
    else
      create unique index if not exists org_integrations_org_provider_uniq on public.org_integrations (org_id, provider);
    end if;
  end if;
end $$;

create table if not exists public.ai_agent_config (
  org_id         uuid primary key,
  ativo          boolean not null default false,
  nome_agente    text not null default 'Assistente' check (char_length(nome_agente) <= 60),
  instrucoes     text not null default '' check (char_length(instrucoes) <= 4000),
  tom            text not null default 'cordial' check (tom in ('cordial', 'formal', 'descontraido', 'consultivo')),
  atualizado_por uuid,
  updated_at     timestamptz not null default now()
);

create table if not exists public.ai_conversas (
  id         bigint generated always as identity primary key,
  org_id     uuid not null,
  contato    text not null,
  role       text not null check (role in ('user', 'assistant')),
  content    text not null,
  created_at timestamptz not null default now()
);
create index if not exists ai_conversas_idx on public.ai_conversas (org_id, contato, created_at desc);

create table if not exists public.ai_audit (
  id         bigint generated always as identity primary key,
  org_id     uuid not null,
  user_id    uuid,
  acao       text not null,
  detalhe    text not null default '',
  created_at timestamptz not null default now()
);
create index if not exists ai_audit_idx on public.ai_audit (org_id, created_at desc);

create table if not exists public.ai_pedidos (
  id                   uuid primary key default gen_random_uuid(),
  org_id               uuid not null,
  contato              text not null,
  itens                jsonb not null,
  total_centavos       integer not null check (total_centavos > 0),
  status               text not null default 'aguardando_pagamento'
                         check (status in ('aguardando_pagamento', 'pago', 'cancelado')),
  checkout_resposta    jsonb,
  transaction_nsu      text,
  paid_amount_centavos integer,
  paid_at              timestamptz,
  created_at           timestamptz not null default now()
);
create index if not exists ai_pedidos_org_idx on public.ai_pedidos (org_id, created_at desc);

create table if not exists public.ai_pagamento_eventos (
  org_id          uuid not null,
  transaction_nsu text not null,
  payload         jsonb not null,
  created_at      timestamptz not null default now(),
  primary key (org_id, transaction_nsu)
);

do $$
declare t text;
begin
  foreach t in array array['ai_agent_config', 'ai_conversas', 'ai_audit', 'ai_pedidos', 'ai_pagamento_eventos'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
    execute format('grant all on public.%I to service_role', t);
  end loop;
  -- leitura pelo navegador (membros da organização), só onde faz sentido
  foreach t in array array['ai_agent_config', 'ai_pedidos'] loop
    execute format('drop policy if exists %I on public.%I', t || '_select', t);
    execute format('create policy %I on public.%I for select to authenticated using (org_id in (select public.minhas_orgs()))', t || '_select', t);
    execute format('grant select on public.%I to authenticated', t);
  end loop;
end $$;

notify pgrst, 'reload schema';
