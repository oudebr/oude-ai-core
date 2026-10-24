"""
Acesso a dados POR TENANT.

IMPORTANTE: a service role ignora o RLS. O isolamento multitenant, aqui, é garantido pelo código:
toda leitura/escrita passa por este repositório, e todo método exige `org_id` e filtra por ele.
Não use `client.table(...)` direto nos serviços/jobs.
"""
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from supabase import AsyncClient

from app.core.providers import INTEGRACOES as PROVIDERS  # noqa: E402


class TenantRepository:
    def __init__(self, client: AsyncClient) -> None:
        self._db = client

    # ---- descoberta de tenants -------------------------------------------------------------
    async def listar_orgs_com_integracao(self, pagina: int = 1000):
        """Gera os org_id que têm ao menos uma integração, em páginas (escala para milhares)."""
        inicio = 0
        vistos: set[str] = set()
        while True:
            r = await (
                self._db.table("org_integrations").select("org_id").order("org_id")
                .range(inicio, inicio + pagina - 1).execute()
            )
            linhas = r.data or []
            for linha in linhas:
                org = str(linha["org_id"])
                if org not in vistos:
                    vistos.add(org)
                    yield UUID(org)
            if len(linhas) < pagina:
                return
            inicio += pagina

    async def integracoes(self, org_id: UUID) -> list[dict[str, Any]]:
        r = await (
            self._db.table("org_integrations").select("provider, encrypted_credentials")
            .eq("org_id", str(org_id)).in_("provider", list(PROVIDERS)).execute()
        )
        return r.data or []

    # ---- catálogo e estoque: tabela OFICIAL `produtos` ---------------------------------------------------
    # Convenção do OÚDE: org_id == id da conta dona (user_id). `produtos` não tem org_id, então o filtro de
    # isolamento é SEMPRE `user_id = org_id`. (A tabela `inventory` não é mais usada.)
    async def estoque_baixo(self, org_id: UUID) -> list[dict[str, Any]]:
        # PostgREST não compara duas colunas; filtramos em memória (só itens com mínimo configurado).
        r = await (
            self._db.table("produtos").select("id, nome, estoque, estoque_minimo")
            .eq("user_id", str(org_id)).gt("estoque_minimo", 0).execute()
        )
        return [i for i in (r.data or []) if i["estoque"] <= i["estoque_minimo"]]

    # ---- idempotência (evita mensagem duplicada, mesmo com reinício/réplicas) -----------------
    async def reservar_execucao(self, org_id: UUID, chave: str) -> bool:
        """True se esta chave ainda não tinha sido processada para o tenant (e a reserva foi feita)."""
        r = await (
            self._db.table("ai_execucoes")
            .upsert({"org_id": str(org_id), "chave": chave}, on_conflict="org_id,chave", ignore_duplicates=True)
            .execute()
        )
        return bool(r.data)

    async def liberar_execucao(self, org_id: UUID, chave: str) -> None:
        await self._db.table("ai_execucoes").delete().eq("org_id", str(org_id)).eq("chave", chave).execute()

    # ---- fallback: notificação no painel do CRM ------------------------------------------------
    async def criar_notificacao(self, org_id: UUID, tipo: str, titulo: str, mensagem: str) -> None:
        await self._db.table("ai_notificacoes").insert(
            {
                "org_id": str(org_id), "tipo": tipo, "titulo": titulo[:120], "mensagem": mensagem[:1000],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()

    async def ping(self) -> None:
        await self._db.table("org_integrations").select("org_id").limit(1).execute()

    # ---- autorização: papel REAL do usuário na organização (org_membros) ---------------------------
    async def papel_do_usuario(self, org_id: UUID, user_id: UUID) -> str | None:
        r = await (
            self._db.table("org_membros").select("papel")
            .eq("org_id", str(org_id)).eq("user_id", str(user_id)).limit(1).execute()
        )
        return r.data[0]["papel"] if r.data else None

    # ---- configuração do agente --------------------------------------------------------------------
    async def get_config(self, org_id: UUID) -> dict[str, Any] | None:
        r = await self._db.table("ai_agent_config").select("*").eq("org_id", str(org_id)).limit(1).execute()
        return r.data[0] if r.data else None

    async def salvar_config(self, org_id: UUID, campos: dict[str, Any], user_id: UUID) -> None:
        permitido = {"ativo", "nome_agente", "instrucoes", "tom", "pausado_em"}
        dados = {k: v for k, v in campos.items() if k in permitido}
        dados.update({"org_id": str(org_id), "atualizado_por": str(user_id),
                      "updated_at": datetime.now(timezone.utc).isoformat()})
        await self._db.table("ai_agent_config").upsert(dados, on_conflict="org_id").execute()

    async def listar_orgs_ativas(self, pagina: int = 1000):
        """Orgs com o agente LIGADO (ativo = true). Os jobs agendados usam esta lista."""
        inicio = 0
        while True:
            r = await (
                self._db.table("ai_agent_config").select("org_id").eq("ativo", True).order("org_id")
                .range(inicio, inicio + pagina - 1).execute()
            )
            linhas = r.data or []
            for linha in linhas:
                yield UUID(str(linha["org_id"]))
            if len(linhas) < pagina:
                return
            inicio += pagina

    # ---- integrações (gravação sempre cifrada; ver core/crypto) ------------------------------------------
    async def salvar_integracao(self, org_id: UUID, provider: str, armazenado: dict[str, Any]) -> None:
        if provider not in PROVIDERS:
            raise ValueError("provider inválido")
        await self._db.table("org_integrations").upsert(
            {"org_id": str(org_id), "provider": provider, "encrypted_credentials": armazenado},
            on_conflict="org_id,provider",
        ).execute()

    async def remover_integracao(self, org_id: UUID, provider: str) -> None:
        await self._db.table("org_integrations").delete().eq("org_id", str(org_id)).eq("provider", provider).execute()

    # ---- histórico de conversa (por organização + contato) -----------------------------------------------
    async def historico(self, org_id: UUID, contato: str, limite: int = 12) -> list[dict[str, str]]:
        r = await (
            self._db.table("ai_conversas").select("role, content").eq("org_id", str(org_id)).eq("contato", contato)
            .order("created_at", desc=True).limit(limite).execute()
        )
        return list(reversed(r.data or []))

    async def adicionar_mensagem(self, org_id: UUID, contato: str, role: str, content: str) -> None:
        await self._db.table("ai_conversas").insert(
            {"org_id": str(org_id), "contato": contato[:120], "role": role, "content": content[:8000]}
        ).execute()

    # ---- auditoria ----------------------------------------------------------------------------------------
    async def auditar(self, org_id: UUID, user_id: UUID | None, acao: str, detalhe: str = "") -> None:
        await self._db.table("ai_audit").insert(
            {"org_id": str(org_id), "user_id": str(user_id) if user_id else None, "acao": acao[:80], "detalhe": detalhe[:500]}
        ).execute()

    async def buscar_catalogo(self, org_id: UUID, termo: str, limite: int = 8) -> list[dict[str, Any]]:
        limpo = re.sub(r"[^\w\s-]", " ", termo, flags=re.UNICODE).strip()[:60]  # sem , ( ) * % que quebram o filtro
        consulta = (
            self._db.table("produtos")
            .select("id, nome, categoria, preco_venda, estoque, volume_ml, unidade_medida, familia_olfativa, "
                    "nota_topo, nota_coracao, nota_fundo")
            .eq("user_id", str(org_id))
        )
        if limpo:
            consulta = consulta.or_(f"nome.ilike.*{limpo}*,categoria.ilike.*{limpo}*,familia_olfativa.ilike.*{limpo}*")
        r = await consulta.limit(max(1, min(limite, 20))).execute()
        return r.data or []

    # ---- pedidos e pagamentos ---------------------------------------------------------------------------------
    async def criar_pedido(
        self, org_id: UUID, contato: str, itens: list[dict[str, Any]], total_centavos: int, provedor: str
    ) -> dict[str, Any]:
        r = await self._db.table("ai_pedidos").insert(
            {"org_id": str(org_id), "contato": contato[:120], "itens": itens, "total_centavos": total_centavos,
             "provedor": provedor}
        ).execute()
        return r.data[0]

    async def get_pedido(self, org_id: UUID, pedido_id: str) -> dict[str, Any] | None:
        r = await self._db.table("ai_pedidos").select("*").eq("org_id", str(org_id)).eq("id", pedido_id).limit(1).execute()
        return r.data[0] if r.data else None

    async def salvar_checkout(self, org_id: UUID, pedido_id: str, resposta: dict[str, Any], url: str | None = None) -> None:
        await self._db.table("ai_pedidos").update({"checkout_resposta": resposta, "checkout_url": url}).eq("org_id", str(org_id)).eq("id", pedido_id).execute()

    async def reservar_evento_pagamento(self, org_id: UUID, transaction_nsu: str, payload: dict[str, Any]) -> bool:
        """True se este transaction_nsu é novo para a organização (dedupe de webhook repetido)."""
        r = await (
            self._db.table("ai_pagamento_eventos")
            .upsert({"org_id": str(org_id), "transaction_nsu": transaction_nsu, "payload": payload},
                    on_conflict="org_id,transaction_nsu", ignore_duplicates=True).execute()
        )
        return bool(r.data)

    async def liberar_evento_pagamento(self, org_id: UUID, transaction_nsu: str) -> None:
        await self._db.table("ai_pagamento_eventos").delete().eq("org_id", str(org_id)).eq("transaction_nsu", transaction_nsu).execute()

    async def marcar_pedido_pago(self, org_id: UUID, pedido_id: str, transaction_nsu: str, pago_centavos: int) -> bool:
        """Transição aguardando_pagamento -> pago, atômica no banco. False se já não estava aguardando."""
        r = await (
            self._db.table("ai_pedidos")
            .update({"status": "pago", "transaction_nsu": transaction_nsu, "paid_amount_centavos": pago_centavos,
                     "paid_at": datetime.now(timezone.utc).isoformat()})
            .eq("org_id", str(org_id)).eq("id", pedido_id).eq("status", "aguardando_pagamento").execute()
        )
        return bool(r.data)

    # ---- tela "Meu Agente de IA": identidade, doutrinação, autonomia (tabelas agent_*, SQL 004) --------------
    async def _ler_uma(self, tabela: str, org_id: UUID) -> dict[str, Any] | None:
        r = await self._db.table(tabela).select("*").eq("org_id", str(org_id)).limit(1).execute()
        return r.data[0] if r.data else None

    async def _gravar_uma(self, tabela: str, org_id: UUID, campos: dict[str, Any], user_id: UUID) -> None:
        dados = {**campos, "org_id": str(org_id), "updated_by": str(user_id),
                 "updated_at": datetime.now(timezone.utc).isoformat()}
        await self._db.table(tabela).upsert(dados, on_conflict="org_id").execute()

    async def get_identidade(self, org_id: UUID) -> dict[str, Any] | None:
        return await self._ler_uma("agent_settings", org_id)

    async def salvar_identidade(self, org_id: UUID, campos: dict[str, Any], user_id: UUID) -> None:
        await self._gravar_uma("agent_settings", org_id, campos, user_id)

    async def get_doutrina(self, org_id: UUID) -> dict[str, Any] | None:
        return await self._ler_uma("agent_doctrines", org_id)

    async def salvar_doutrina(self, org_id: UUID, campos: dict[str, Any], user_id: UUID) -> None:
        await self._gravar_uma("agent_doctrines", org_id, campos, user_id)

    async def get_autonomia(self, org_id: UUID) -> dict[str, Any] | None:
        return await self._ler_uma("agent_permissions", org_id)

    async def salvar_autonomia(self, org_id: UUID, campos: dict[str, Any], user_id: UUID) -> None:
        await self._gravar_uma("agent_permissions", org_id, campos, user_id)

    # ---- estado das integrações (SEM segredos) e logs ------------------------------------------------------
    async def estado_integracoes(self, org_id: UUID) -> list[dict[str, Any]]:
        r = await (
            self._db.table("agent_integrations")
            .select("provider, status, ultimo_teste_em, ultimo_teste_ok, ultimo_detalhe").eq("org_id", str(org_id)).execute()
        )
        return r.data or []

    async def registrar_estado_integracao(
        self, org_id: UUID, provider: str, status: str, ok: bool | None = None, detalhe: str = ""
    ) -> None:
        agora = datetime.now(timezone.utc).isoformat()
        dados: dict[str, Any] = {"org_id": str(org_id), "provider": provider, "status": status, "updated_at": agora,
                                 "ultimo_detalhe": detalhe[:300]}
        if ok is not None or status == "configurado":
            dados.update({"ultimo_teste_ok": ok, "ultimo_teste_em": agora if ok is not None else None})
        await self._db.table("agent_integrations").upsert(dados, on_conflict="org_id,provider").execute()

    async def remover_estado_integracao(self, org_id: UUID, provider: str) -> None:
        await self._db.table("agent_integrations").delete().eq("org_id", str(org_id)).eq("provider", provider).execute()

    async def registrar_log(
        self, org_id: UUID, tipo: str, detalhe: str = "", ferramenta: str | None = None,
        contato: str | None = None, ok: bool = True,
    ) -> None:
        await self._db.table("agent_logs").insert(
            {"org_id": str(org_id), "tipo": tipo, "ferramenta": ferramenta, "contato": (contato or "")[:120] or None,
             "ok": ok, "detalhe": detalhe[:500]}
        ).execute()

    async def listar_logs(self, org_id: UUID, limite: int = 50) -> list[dict[str, Any]]:
        r = await (
            self._db.table("agent_logs").select("id, tipo, ferramenta, ok, detalhe, created_at")
            .eq("org_id", str(org_id)).order("created_at", desc=True).limit(max(1, min(limite, 200))).execute()
        )
        return r.data or []

    async def listar_auditoria(self, org_id: UUID, limite: int = 50) -> list[dict[str, Any]]:
        r = await (
            self._db.table("ai_audit").select("id, acao, detalhe, created_at")
            .eq("org_id", str(org_id)).order("created_at", desc=True).limit(max(1, min(limite, 200))).execute()
        )
        return r.data or []
