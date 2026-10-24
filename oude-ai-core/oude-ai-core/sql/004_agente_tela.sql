-- oude-ai-core, etapa 4: tela "Meu Agente de IA" do CRM. Idempotente.
-- Pré-requisitos: sql/001, 002 e 003 + org_membros/minhas_orgs() (script 20261022210000).
--
-- Onde fica cada coisa (sem duplicar o que já existe):
--   ai_agent_config   (002)  ativo, nome_agente, tom, instrucoes (texto livre)  [+ pausado_em, tom 'persuasivo']
--   agent_settings    (NOVA) identidade e personalidade
--   agent_doctrines   (NOVA) doutrinação (história, pagamentos, entrega, FAQ...)
--   agent_permissions (NOVA) autonomia (interruptores + limite de desconto)
--   agent_integrations(NOVA) SÓ estado/diagnóstico de cada integração. NENHUM SEGREDO aqui:
--                            as chaves continuam cifradas em org_integrations.
--   agent_logs        (NOVA) execução de ferramentas, testes e erros
--
-- Segurança: RLS ligado em todas. O navegador só LÊ (membros da organização; logs: dono/admin).
-- Toda escrita passa pelo oude-ai-core (service_role), que valida papel, tamanho e conteúdo.

-- 0) ai_agent_config: marca de pausa e novo tom.
alter table public.ai_agent_config add column if not exists pausado_em timestamptz;
alter table public.ai_agent_config drop constraint if exists ai_agent_config_tom_check;
alter table public.ai_agent_config
  add constraint ai_agent_config_tom_check check (tom in ('cordial', 'formal', 'descontraido', 'consultivo', 'persuasivo'));

-- 1) Identidade e personalidade
create table if not exists public.agent_settings (
  org_id            uuid primary key,
  nome_loja         text not null default '' check (char_length(nome_loja) <= 120),
  segmento          text not null default 'perfumaria'
                      check (segmento in ('perfumaria', 'clinica', 'ecommerce', 'servicos', 'outro')),
  saudacao          text not null default '' check (char_length(saudacao) <= 300),
  estilo_comunicacao text not null default '' check (char_length(estilo_comunicacao) <= 600),
  emojis            text not null default 'moderados' check (emojis in ('permitidos', 'moderados', 'proibidos')),
  idioma            text not null default 'pt-BR' check (idioma in ('pt-BR', 'en', 'es')),
  objetivos         text not null default '' check (char_length(objetivos) <= 800),
  transbordo        text not null default '' check (char_length(transbordo) <= 800),
  updated_by        uuid,
  updated_at        timestamptz not null default now()
);

-- 2) Doutrinação
create table if not exists public.agent_doctrines (
  org_id             uuid primary key,
  historia           text not null default '' check (char_length(historia) <= 2000),
  publico_alvo       text not null default '' check (char_length(publico_alvo) <= 1000),
  produtos_servicos  text not null default '' check (char_length(produtos_servicos) <= 2000),
  pagamentos         text not null default '' check (char_length(pagamentos) <= 1000),
  entrega            text not null default '' check (char_length(entrega) <= 1000),
  trocas             text not null default '' check (char_length(trocas) <= 1000),
  horarios           text not null default '' check (char_length(horarios) <= 600),
  promocoes          text not null default '' check (char_length(promocoes) <= 1000),
  regras_comerciais  text not null default '' check (char_length(regras_comerciais) <= 1500),
  faq                jsonb not null default '[]'::jsonb check (jsonb_typeof(faq) = 'array' and jsonb_array_length(faq) <= 30),
  updated_by         uuid,
  updated_at         timestamptz not null default now()
);

-- 3) Autonomia (padrão SEGURO: só responde, recomenda, consulta e encaminha)
create table if not exists public.agent_permissions (
  org_id                 uuid primary key,
  responder_duvidas      boolean not null default true,
  recomendar_produtos    boolean not null default true,
  consultar_estoque      boolean not null default true,
  registrar_oportunidades boolean not null default false,
  criar_pedidos          boolean not null default false,
  enviar_whatsapp        boolean not null default false,
  aplicar_descontos      boolean not null default false,
  desconto_max_pct       numeric(5,2) not null default 0 check (desconto_max_pct >= 0 and desconto_max_pct <= 50),
  criar_cobrancas        boolean not null default false,
  encaminhar_funcionario boolean not null default true,
  updated_by             uuid,
  updated_at             timestamptz not null default now()
);

-- 4) Estado das integrações (sem segredos)
create table if not exists public.agent_integrations (
  org_id          uuid not null,
  provider        text not null check (provider in ('llm', 'whatsapp', 'supplier', 'infinitepay', 'mercadopago', 'pix')),
  status          text not null default 'configurado' check (status in ('configurado', 'conectado', 'erro')),
  ultimo_teste_em timestamptz,
  ultimo_teste_ok boolean,
  ultimo_detalhe  text not null default '' check (char_length(ultimo_detalhe) <= 300),
  updated_at      timestamptz not null default now(),
  primary key (org_id, provider)
);

-- 5) Logs do agente
create table if not exists public.agent_logs (
  id         bigint generated always as identity primary key,
  org_id     uuid not null,
  tipo       text not null check (tipo in ('chat', 'teste', 'ferramenta', 'integracao', 'erro')),
  ferramenta text,
  contato    text,
  ok         boolean not null default true,
  detalhe    text not null default '' check (char_length(detalhe) <= 500),
  created_at timestamptz not null default now()
);
create index if not exists agent_logs_org_idx on public.agent_logs (org_id, created_at desc);

-- RLS: leitura por membros; logs só dono/admin; nenhuma escrita pelo navegador.
do $$
declare t text;
begin
  foreach t in array array['agent_settings', 'agent_doctrines', 'agent_permissions', 'agent_integrations', 'agent_logs'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
    execute format('grant all on public.%I to service_role', t);
    execute format('drop policy if exists %I on public.%I', t || '_select', t);
  end loop;

  foreach t in array array['agent_settings', 'agent_doctrines', 'agent_permissions', 'agent_integrations'] loop
    execute format('create policy %I on public.%I for select to authenticated using (org_id in (select public.minhas_orgs()))', t || '_select', t);
    execute format('grant select on public.%I to authenticated', t);
  end loop;

  create policy agent_logs_select on public.agent_logs
    for select to authenticated using (org_id in (select public.minhas_orgs_admin()));
  grant select on public.agent_logs to authenticated;
end $$;

grant usage, select on all sequences in schema public to service_role;
notify pgrst, 'reload schema';

-- Conferência rápida (deve listar as 5 tabelas com RLS ligado = true):
-- select relname, relrowsecurity from pg_class where relname in
--   ('agent_settings','agent_doctrines','agent_permissions','agent_integrations','agent_logs');
