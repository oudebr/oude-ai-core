"""Registro dos meios de pagamento. Para adicionar um: criar a classe (contrato em base.py), registrar aqui,
declarar o provider em app/core/providers.py e o formulário em app/services/integracoes.py."""
from app.services.payments.base import ProvedorPagamento
from app.services.payments.infinitepay import InfinitePay
from app.services.payments.mercadopago import MercadoPago
from app.services.payments.pix import PixManual

PROVEDORES_PAGAMENTO: dict[str, ProvedorPagamento] = {p.nome: p for p in (InfinitePay(), MercadoPago(), PixManual())}


def provedor_de(nome: str) -> ProvedorPagamento | None:
    return PROVEDORES_PAGAMENTO.get(nome)
