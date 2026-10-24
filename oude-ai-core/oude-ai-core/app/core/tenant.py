"""Contexto do lojista (tenant) em uma execução. Tudo que o agente faz recebe um TenantContext."""
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class TenantContext:
    org_id: UUID
    # provider -> credenciais JÁ decifradas ("llm", "whatsapp", "supplier"). Nunca logar/serializar.
    credenciais: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def tem(self, provider: str) -> bool:
        return provider in self.credenciais

    def cred(self, provider: str) -> dict[str, Any]:
        return self.credenciais[provider]
