# oude-ai-core

Microserviço Python (FastAPI) do agente de IA do OÚDE CRM. Multitenant por `org_id`.

## Estado real (2026-10-22, 2ª rodada)

| Área | Situação |
|---|---|
| Catálogo e estoque: tabela oficial **`produtos`** (`user_id` = `org_id`); `inventory` não é mais usada | Implementado; filtro por org provado com cliente Supabase gravador. **Consulta real ao banco: nunca executada** |
| Credenciais do lojista cifradas em `org_integrations` (Fernet, vinculadas à `org_id`); o agente só usa as da organização do contexto | Implementado e testado |
| WhatsApp: **Meta Cloud, Evolution API e Z-API** (formato de envio conferido na doc oficial de Evolution e Z-API) | Implementado; testado com HTTP simulado. **Nunca enviou mensagem real** |
| Pagamentos extensíveis por organização: **InfinitePay, Mercado Pago, Pix (confirmação manual)** | Implementado; testado com simulação. **Nunca chamou nenhum provedor real** |
| Verificação servidor-a-servidor (`payment_check` / `GET /v1/payments/{id}`), valor conferido, idempotência, isolamento por org e por provedor | Implementado e testado |
| Rotas da tela do lojista: salvar instruções, ativar/pausar, gerir integrações (schema dos formulários, salvar, testar, remover) | Implementado; rotas só em `tests/test_api.py` (pulado sem fastapi) |
| Autenticação por token assinado + papel real em `org_membros`, anti-replay, rotação | Implementado e testado |
| Sincronização com fornecedor | **NÃO implementada** (as credenciais do lojista já são guardadas; falta a documentação da API do fornecedor) |
| Engajamento (aniversário, leads, pós-venda) | **NÃO implementado** (job é só um marcador) |
| FastAPI/supabase-py subindo de verdade | **Nunca executado** (PyPI bloqueado neste ambiente) |

## Procedimento reproduzível

```bash
cd oude-ai-core
./scripts/verificar.sh
```
Instala dependências, roda a suíte, roda os testes de rotas, sobe a API em `:8765` e consulta `/health` e uma rota protegida (deve dar 401). Sem acesso ao PyPI, o script roda só a suíte sem rede (stubs de httpx/supabase) e avisa.
Suíte sem dependências externas (exceto `cryptography`): `python3 -m unittest discover -s tests -t .`

## Contrato com o backend do CRM

Toda rota de lojista exige `Authorization: Bearer <token>`. O token é um JWT HS256 de 60 s assinado com `CRM_SHARED_SECRET` (exemplo pronto em `docs/crm-signer.ts`, verificado contra o Python). O CRM deve conferir que o usuário logado pertence à organização antes de assinar. **Nunca** exponha o segredo nem chame este serviço do navegador.

| Rota | Papéis | O que faz |
|---|---|---|
| `GET /v1/tenants/{org}/agente` | dono, admin, membro | config + quais integrações existem |
| `PUT /v1/tenants/{org}/agente` | dono, admin | salva `instrucoes`, `nome_agente`, `tom` |
| `POST .../agente/ativar` · `.../pausar` | dono, admin | liga/pausa (ligar exige chave de IA) |
| `GET .../integracoes/schema` | dono, admin | campos de cada integração (para montar o formulário; sem segredos) |
| `GET .../integracoes` | dono, admin | só booleanos por integração, nunca chaves |
| `PUT .../integracoes/{llm,whatsapp,supplier,infinitepay,mercadopago,pix}` | dono, admin | valida, cifra e salva (substitui) |
| `DELETE .../integracoes/{provider}` | dono, admin | remove |
| `POST .../integracoes/{provider}/testar` | dono, admin | teste real em `llm` (GET /models), `whatsapp` Meta e Evolution; os demais informam que não há teste confirmado |
| `POST .../chat` | dono, admin, membro | conversa (409 se pausado, 429 acima do limite) |
| `POST .../jobs/{job}` | dono, admin | roda um job para a org |
| `POST .../pedidos` | dono, admin | cria pedido e cobrança no meio escolhido (`provedor`; opcional se só houver um conectado) |
| `GET .../pedidos/{id}` | todos | status do pedido (404 se for de outra org) |
| `POST .../pedidos/{id}/confirmar-manual` | dono, admin | só Pix estático; fica na auditoria |
| `POST /webhooks/pagamentos/{provedor}/{org}/{segredo}` | público, segredo na URL | confirma pagamento (rota antiga `/webhooks/infinitepay/{org}/{segredo}` mantida) |
| `GET /health` · `GET /ready` | público | saúde / banco |

Corpos de integração (todos salvos CIFRADOS, por organização):
- `llm`: `{provider: openai|groq|xai, model, api_key}`
- `whatsapp`: `{provider: meta_cloud, notify_to, phone_number_id, access_token}` · `{provider: evolution, notify_to, base_url, instance, apikey}` · `{provider: zapi, notify_to, instance_id, token, client_token?}`
- `infinitepay`: `{handle}` · `mercadopago`: `{access_token}` · `pix`: `{chave_pix, nome_recebedor, cidade}` · `supplier`: `{catalog_url, api_key?}`
- Pedido: `{provedor?, contato?, itens:[{descricao, quantidade, preco_centavos}]}`. Resposta: `checkout_url` (quando confirmada) ou `pix_copia_e_cola`.
- Para adicionar um meio de pagamento: classe em `app/services/payments/` (contrato em `base.py`), registrar em `registry.py`, declarar em `app/core/providers.py` e no formulário de `integracoes.py`.

## Segurança (decisões)

- A service role ignora RLS: o isolamento é feito no código (`TenantRepository` filtra tudo por `org_id`; o `org_id` das ferramentas do agente vem do servidor, nunca do modelo).
- O papel vem do banco (`org_membros`), não do token. Token vale para uma org só.
- Pagamento: a doc pública da InfinitePay **não descreve assinatura de webhook**, e a do Mercado Pago consultada **não traz o passo-a-passo do `x-signature`** (não inventei). Por isso nenhum webhook marca pago sozinho: segredo por organização/provedor na URL + consulta servidor-a-servidor com a credencial do lojista (`payment_check` / `GET /v1/payments/{id}`) + valor conferido (InfinitePay: `amount` == total e `paid_amount` ≥ `amount`; Mercado Pago: `status == approved`, BRL, `transaction_amount` == total, `external_reference` == pedido) + idempotência por (provedor, transação) + pedido precisa ser da MESMA organização e do MESMO provedor.
- Pix estático não avisa pagamento: confirmação manual por dono/admin. O BR Code (estrutura + CRC) foi verificado internamente, **não em app de banco**: teste com R$ 0,01.
- Evolution API: o `base_url` é do lojista (instância própria): anti-SSRF a cada envio e sem redirecionamento.
- Segredos globais (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CRM_SHARED_SECRET`, `CREDENTIALS_MASTER_KEY`, `PUBLIC_BASE_URL`): só variáveis de ambiente (Render), lidas em `app/config.py`; nenhum serviço de IA recebe `settings` (teste automático).
- Instruções do lojista entram no prompt depois das regras fixas, delimitadas e saneadas. Isso reduz, não elimina, injeção de prompt; por isso o agente só tem ferramenta de leitura e não fecha pedido sozinho.

## Render

1. Rode no Supabase: `sql/001_ai_core.sql`, `sql/002_agente_pedidos.sql` e `sql/003_produtos_pagamentos.sql` (pré-requisito: scripts de RLS/ABAC `20261022200000` e `20261022210000`).
2. Crie o serviço pelo Blueprint (`render.yaml`) e preencha as variáveis marcadas `sync: false`.
3. Health check: `/health`. Logs: stdout da Render (chaves e telefones não são logados).
4. **Escala:** mantenha `numInstances: 1`. Scheduler, anti-replay e limite de uso são por processo; com mais réplicas é preciso `RUN_SCHEDULER=true` em só uma e aceitar limites efetivos maiores. Persistência: nenhum disco, tudo no Supabase.
5. Jobs: rodam dentro do processo (APScheduler), apenas para orgs com agente ATIVO, com idempotência em `ai_execucoes`.

## O que ainda preciso de você

1. **InfinitePay:** o corpo de resposta de `POST /links` não é documentado: faça uma chamada de teste com um `handle` real e me envie a resposta para definir `CAMPO_URL_CHECKOUT` (enquanto isso `checkout_url` vem vazio e a resposta bruta fica em `ai_pedidos.checkout_resposta`). Confirme também a regra de `paid_amount` > `amount` e se há autenticação/assinatura de webhook (parcerias@cloudwalk.io).
2. **Mercado Pago:** envie o passo-a-passo oficial de validação do `x-signature` (aba "Sem SDKs" da doc de Webhooks) e eu adiciono a validação de assinatura.
3. **Z-API:** o endpoint de status da instância não foi confirmado, então "testar" retorna "indisponível". **Evolution:** o endpoint de estado da conexão foi visto em espelho da doc (só o status HTTP é usado). **Meta:** o formato de envio vem do esqueleto anterior e não foi reconferido nesta rodada.
4. **Fornecedor:** nome e documentação oficial da API (autenticação, catálogo, preços, estoque, pedido de compra). Cada lojista usa o seu: as credenciais já ficam cifradas por organização.
5. **Convenção `org_id` = `user_id` da conta dona:** o catálogo filtra `produtos.user_id = org_id`. Se algum dia uma organização tiver donos diferentes da conta que grava os produtos, preciso de uma tabela de mapeamento.
6. **Render:** preencher `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CRM_SHARED_SECRET` (mesmo valor no backend do CRM), `CREDENTIALS_MASTER_KEY`, `PUBLIC_BASE_URL` e rodar `sql/001`, `002` e `003`.
7. Telas e rotas do backend do CRM que chamam este serviço (usam `docs/crm-signer.ts`): ainda não escritas; este projeto não altera o código web.
