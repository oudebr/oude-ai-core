"""
Contrato de um meio de pagamento. Cada organização (lojista) conecta os SEUS provedores; as credenciais vêm
cifradas de org_integrations. Regra de segurança comum a todos: o conteúdo do webhook NUNCA confirma
pagamento; só `verificar` (consulta servidor-a-servidor ao provedor com a credencial do lojista) confirma.
"""
from dataclasses import dataclass, field
from typing import Any, Protocol


class PagamentoIndisponivel(Exception):
    """Falha de comunicação/formato com o provedor (temporária do ponto de vista do webhook)."""


@dataclass(frozen=True)
class Cobranca:
    url: str | None = None                # link de pagamento (quando o provedor devolve e o campo é confirmado)
    copia_e_cola: str | None = None       # Pix copia-e-cola
    bruto: dict[str, Any] = field(default_factory=dict)   # resposta bruta do provedor (auditoria)
    aviso: str | None = None


@dataclass(frozen=True)
class EventoPagamento:
    transacao_id: str                     # chave de idempotência dentro do provedor
    pedido_id: str | None                 # quando o webhook já cita o pedido (InfinitePay); None no Mercado Pago
    dados: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verificacao:
    pago: bool
    pedido_id: str | None = None
    valor_centavos: int | None = None
    pago_centavos: int | None = None


class ProvedorPagamento(Protocol):
    nome: str
    usa_webhook: bool          # False => confirmação manual (Pix estático)

    async def criar_cobranca(self, http: Any, cred: dict[str, Any], pedido_id: str, itens: list[dict[str, Any]],
                             total_centavos: int, webhook_url: str) -> Cobranca: ...

    def extrair_evento(self, payload: dict[str, Any]) -> EventoPagamento | None: ...

    async def verificar(self, http: Any, cred: dict[str, Any], evento: EventoPagamento) -> Verificacao: ...
