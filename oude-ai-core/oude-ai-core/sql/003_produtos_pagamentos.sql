-- oude-ai-core, etapa 3: pedidos com meio de pagamento por organização. Idempotente. Rode depois de 002.
-- Catálogo/estoque usam a tabela oficial `produtos` (user_id = org_id); nada a criar para isso.
alter table public.ai_pedidos add column if not exists provedor text;
alter table public.ai_pedidos add column if not exists checkout_url text;
do $$
begin
  if exists (select 1 from pg_constraint where conrelid = 'public.ai_pedidos'::regclass and conname = 'ai_pedidos_provedor_chk') then
    alter table public.ai_pedidos drop constraint ai_pedidos_provedor_chk;
  end if;
  alter table public.ai_pedidos add constraint ai_pedidos_provedor_chk
    check (provedor is null or provedor in ('infinitepay', 'mercadopago', 'pix')) not valid;
end $$;
create index if not exists produtos_user_nome_idx on public.produtos (user_id, nome);
notify pgrst, 'reload schema';
