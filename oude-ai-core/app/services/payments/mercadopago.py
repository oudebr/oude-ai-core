"""
Mercado Pago (credencial do lojista: access_token). Conferido na documentação oficial em 2026-10-22:
  POST https://api.mercadopago.com/checkout/preferences  (Bearer)
       body: items[{title, quantity, unit_price, currency_id}], external_reference, notification_url, back_urls
       resposta: id, init_point, sandbox_init_point
  GET  https://api.mercadopago.com/v1/payments/{id}      (Bearer)
       resposta usada: status ("approved"), transaction_amount, external_reference, currency_id
  Webhook: corpo JSON com `type` ("payment") e `data.id`.
LACUNA: a página pública consultada NÃO traz o passo-a-passo da validação de `x-signature`. Não inventamos essa
validação: a autenticidade vem de (1) segredo por organização na URL do webhook e (2) consulta servidor-a-servidor
com o access_token do lojista (um terceiro não consegue fabricar um pagamento aprovado).
Fora de escopo: estornos/chargebacks após a aprovação.
"""
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.services.payments.base import Cobranca, EventoPagamento, PagamentoIndisponivel, Verificacao

BASE = "https://api.mercadopago.com"


def reais_para_centavos(valor: Any) -> int | None:
    """Converte o valor em reais devolvido pela API para centavos inteiros; None se não for exato."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    try:
        c = Decimal(str(valor)) * 100
    except InvalidOperation:
        return None
    return int(c) if c == c.to_integral_value() else None


class MercadoPago:
    nome = "mercadopago"
    usa_webhook = True

    async def criar_cobranca(self, http, cred, pedido_id, itens, total_centavos, webhook_url) -> Cobranca:
        corpo = {
            "items": [
                {"title": i["descricao"][:120], "quantity": i["quantidade"],
                 "unit_price": float(Decimal(i["preco_centavos"]) / 100), "currency_id": "BRL"}
                for i in itens
            ],
            "external_reference": pedido_id,
            "notification_url": webhook_url,
        }
        try:
            r = await http.post(f"{BASE}/checkout/preferences", headers={"Authorization": f"Bearer {cred['access_token']}"}, json=corpo)
            r.raise_for_status()
            dados = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PagamentoIndisponivel(f"Falha ao criar preferência ({type(exc).__name__}).") from exc
        if not isinstance(dados, dict):
            raise PagamentoIndisponivel("Resposta inesperada do Mercado Pago.")
        url = dados.get("init_point")
        return Cobranca(url=url if isinstance(url, str) and url.startswith("https://") else None, bruto=dados)

    def extrair_evento(self, payload) -> EventoPagamento | None:
        data = payload.get("data")
        pid = data.get("id") if isinstance(data, dict) else None
        if payload.get("type") != "payment" or pid is None or isinstance(pid, bool) or not str(pid).isdigit():
            return None
        return EventoPagamento(transacao_id=str(pid), pedido_id=None)  # o pedido vem da consulta (external_reference)

    async def verificar(self, http, cred, evento) -> Verificacao:
        try:
            r = await http.get(f"{BASE}/v1/payments/{evento.transacao_id}", headers={"Authorization": f"Bearer {cred['access_token']}"})
            r.raise_for_status()
            d = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PagamentoIndisponivel(f"Falha ao consultar pagamento ({type(exc).__name__}).") from exc
        if not isinstance(d, dict):
            raise PagamentoIndisponivel("Resposta inesperada do Mercado Pago.")
        valor = reais_para_centavos(d.get("transaction_amount"))
        ref = d.get("external_reference")
        aprovado = d.get("status") == "approved" and d.get("currency_id") == "BRL"
        return Verificacao(pago=aprovado, pedido_id=ref if isinstance(ref, str) else None,
                           valor_centavos=valor, pago_centavos=valor)
