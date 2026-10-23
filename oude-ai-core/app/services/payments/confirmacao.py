"""
Confirmação de pagamento, comum a todos os provedores (sem rede direta: `provedor` é injetado).
Garantias: (1) o webhook sozinho nunca marca pago; só a verificação servidor-a-servidor com a credencial do
lojista; (2) valor conferido; (3) idempotente por (provedor, transação); (4) pedido de OUTRA organização ou de
OUTRO provedor é invisível; (5) falha do provedor => ErroTemporario (o provedor tenta de novo).
"""
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4


class ErroTemporario(Exception):
    """O webhook deve responder erro para o provedor tentar de novo."""


@dataclass(frozen=True)
class Resultado:
    status: str  # "pago" | "duplicado" | "ignorado" | "divergente" | "nao_pago"
    detalhe: str = ""


async def confirmar_pagamento(repo, provedor, http: Any, cred: dict[str, Any], org_id: UUID, payload: dict[str, Any]) -> Resultado:
    evento = provedor.extrair_evento(payload)
    if evento is None:
        return Resultado("ignorado", "evento não reconhecido")

    if evento.pedido_id is not None:  # rejeita cedo (sem chamar o provedor) o que não é desta org/provedor
        pre = await repo.get_pedido(org_id, evento.pedido_id)
        if pre is None or pre.get("provedor") != provedor.nome:
            return Resultado("ignorado", "pedido inexistente")
        if pre["status"] == "pago":
            return Resultado("duplicado", "pedido já pago")

    chave = f"{provedor.nome}:{evento.transacao_id}"
    if not await repo.reservar_evento_pagamento(org_id, chave, payload):
        return Resultado("duplicado", "evento repetido")
    try:
        ver = await provedor.verificar(http, cred, evento)
    except Exception as exc:  # noqa: BLE001
        await repo.liberar_evento_pagamento(org_id, chave)  # permite o retry do provedor
        raise ErroTemporario(str(exc)) from exc

    if not ver.pago:
        await repo.liberar_evento_pagamento(org_id, chave)
        return Resultado("nao_pago", "provedor não confirma pagamento")
    if evento.pedido_id and ver.pedido_id and evento.pedido_id != ver.pedido_id:
        await repo.auditar(org_id, None, "pagamento_divergente", f"{chave} pedido do evento != pedido do provedor")
        return Resultado("divergente", "pedido do evento difere do pedido do provedor")

    pedido_id = ver.pedido_id or evento.pedido_id
    pedido = await repo.get_pedido(org_id, pedido_id) if pedido_id else None
    if pedido is None or pedido.get("provedor") != provedor.nome:
        return Resultado("ignorado", "pedido inexistente")
    if pedido["status"] == "pago":
        return Resultado("duplicado", "pedido já pago")

    # `pago` pode exceder `valor` (ex.: juros de parcelamento no exemplo da InfinitePay); nunca ser menor.
    if ver.valor_centavos != pedido["total_centavos"] or ver.pago_centavos is None or ver.pago_centavos < ver.valor_centavos:
        await repo.auditar(org_id, None, "pagamento_divergente",
                           f"{chave} esperado={pedido['total_centavos']} valor={ver.valor_centavos} pago={ver.pago_centavos}")
        return Resultado("divergente", "valor diferente do pedido")
    if not await repo.marcar_pedido_pago(org_id, str(pedido["id"]), chave, ver.pago_centavos):
        return Resultado("duplicado", "pedido já havia sido pago")
    await repo.auditar(org_id, None, "pedido_pago", f"pedido={pedido['id']} via {provedor.nome}")
    return Resultado("pago")


async def confirmar_manual(repo, org_id: UUID, user_id: UUID, pedido_id: str) -> Resultado:
    """Pix estático: o dono/admin confirma depois de ver o dinheiro na conta. Só vale para pedidos Pix."""
    pedido = await repo.get_pedido(org_id, pedido_id)
    if pedido is None:
        return Resultado("ignorado", "pedido inexistente")
    if pedido.get("provedor") != "pix":
        return Resultado("ignorado", "confirmação manual só existe para Pix")
    if pedido["status"] != "aguardando_pagamento":
        return Resultado("duplicado", "pedido não está aguardando pagamento")
    if not await repo.marcar_pedido_pago(org_id, pedido_id, f"manual:{uuid4()}", pedido["total_centavos"]):
        return Resultado("duplicado", "pedido já havia sido pago")
    await repo.auditar(org_id, user_id, "pedido_pago_manual", f"pedido={pedido_id}")
    return Resultado("pago", "confirmado manualmente")
