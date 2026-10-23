"""
InfinitePay Checkout — SOMENTE o que a documentação oficial publica
(https://www.infinitepay.io/checkout-documentacao, conferida em 2026-10-22):
  POST https://api.checkout.infinitepay.io/links          body: handle, items[{quantity, price(centavos), description}],
                                                           order_nsu, redirect_url, webhook_url, customer, address
  POST https://api.checkout.infinitepay.io/payment_check  body: handle, order_nsu, transaction_nsu, slug
                                                           resposta: success, paid, amount, paid_amount, installments, capture_method
LACUNAS da documentação (não inventadas aqui):
  - nenhuma autenticação documentada (só o `handle`);
  - o corpo de resposta de POST /links NÃO é documentado: guardamos a resposta bruta e não extraímos a URL
    do checkout até confirmar o nome do campo numa chamada real (CAMPO_URL_CHECKOUT);
  - webhook SEM assinatura documentada: por isso a confirmação de pagamento sempre passa por /payment_check.
"""
from typing import Any

import httpx

from app.services.payments.base import Cobranca, EventoPagamento, PagamentoIndisponivel, Verificacao

BASE = "https://api.checkout.infinitepay.io"
# Preencha com o nome do campo REAL após uma chamada de teste com a sua conta (ex.: inspecione a resposta bruta
# salva em ai_pedidos.checkout_resposta). Enquanto for None, o serviço não afirma nenhuma URL de pagamento.
CAMPO_URL_CHECKOUT: str | None = None


async def criar_link(
    http: httpx.AsyncClient, handle: str, order_nsu: str, itens: list[dict[str, Any]], webhook_url: str,
    redirect_url: str | None = None,
) -> dict[str, Any]:
    corpo: dict[str, Any] = {
        "handle": handle,
        "order_nsu": order_nsu,
        "webhook_url": webhook_url,
        "items": [{"quantity": i["quantidade"], "price": i["preco_centavos"], "description": i["descricao"]} for i in itens],
    }
    if redirect_url:
        corpo["redirect_url"] = redirect_url
    try:
        r = await http.post(f"{BASE}/links", json=corpo)
        r.raise_for_status()
        dados = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PagamentoIndisponivel(f"Falha ao criar link ({type(exc).__name__}).") from exc
    return dados if isinstance(dados, dict) else {"resposta": dados}


def url_checkout(resposta: dict[str, Any]) -> str | None:
    if CAMPO_URL_CHECKOUT is None:
        return None
    v = resposta.get(CAMPO_URL_CHECKOUT)
    return v if isinstance(v, str) and v.startswith("https://") else None


class InfinitePayVerificador:
    """Consulta server-to-server /payment_check (a fonte da verdade; o webhook sozinho não basta)."""

    def __init__(self, http: httpx.AsyncClient, handle: str) -> None:
        self._http, self._handle = http, handle

    async def verificar(self, order_nsu: str, transaction_nsu: str, slug: str) -> dict[str, Any]:
        try:
            r = await self._http.post(
                f"{BASE}/payment_check",
                json={"handle": self._handle, "order_nsu": order_nsu, "transaction_nsu": transaction_nsu, "slug": slug},
            )
            r.raise_for_status()
            dados = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PagamentoIndisponivel(f"Falha ao consultar pagamento ({type(exc).__name__}).") from exc
        if not isinstance(dados, dict):
            raise PagamentoIndisponivel("Resposta inesperada do provedor.")
        return dados


class InfinitePay:
    """Adaptador do contrato ProvedorPagamento para a InfinitePay (credencial do lojista: handle)."""

    nome = "infinitepay"
    usa_webhook = True

    async def criar_cobranca(self, http, cred, pedido_id, itens, total_centavos, webhook_url) -> Cobranca:
        bruto = await criar_link(http, cred["handle"], pedido_id, itens, webhook_url)
        url = url_checkout(bruto)
        aviso = None if url else "Resposta salva em ai_pedidos.checkout_resposta; campo da URL ainda não confirmado (infinitepay.CAMPO_URL_CHECKOUT)."
        return Cobranca(url=url, bruto=bruto, aviso=aviso)

    def extrair_evento(self, payload) -> EventoPagamento | None:
        order, tx, slug = payload.get("order_nsu"), payload.get("transaction_nsu"), payload.get("invoice_slug") or payload.get("slug")
        if not all(isinstance(x, str) and x for x in (order, tx, slug)):
            return None
        return EventoPagamento(transacao_id=tx, pedido_id=order, dados={"slug": slug})

    async def verificar(self, http, cred, evento) -> Verificacao:
        r = await InfinitePayVerificador(http, str(cred["handle"])).verificar(
            str(evento.pedido_id), evento.transacao_id, str(evento.dados["slug"]))
        inteiro = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None  # noqa: E731
        return Verificacao(
            pago=r.get("success") is True and r.get("paid") is True, pedido_id=evento.pedido_id,
            valor_centavos=inteiro(r.get("amount")), pago_centavos=inteiro(r.get("paid_amount")),
        )
