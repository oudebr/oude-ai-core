"""
Criptografia das credenciais dos lojistas (chaves de IA, WhatsApp, pagamentos, fornecedor).

Formato em `org_integrations.encrypted_credentials` (jsonb):  {"v": 1, "ct": "<token Fernet>"}
- Rotação: MultiFernet. A 1ª chave cifra; todas decifram. Para trocar a chave mestra, coloque a nova em
  CREDENTIALS_MASTER_KEY e a antiga em CREDENTIALS_MASTER_KEY_PREVIOUS, rode `reencriptar` e remova a antiga.
- Vínculo com a organização: o conteúdo cifrado carrega o org_id. Um blob copiado de uma organização para
  outra NÃO é aceito (CredenciaisInvalidas).
CLI:  python -m app.core.crypto gerar-chave | cifrar <org_id> '<json>'
"""
import json
import sys
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CredenciaisInvalidas(Exception):
    """Credencial ausente, em texto puro, de outra organização ou cifrada com chave desconhecida."""


def _mf(chaves: list[str]) -> MultiFernet:
    if not chaves:
        raise CredenciaisInvalidas("Nenhuma chave mestra configurada.")
    return MultiFernet([Fernet(c.encode()) for c in chaves])


def cifrar_credenciais(dados: dict[str, Any], chaves: list[str], org_id: UUID) -> dict[str, Any]:
    conteudo = {**dados, "_org": str(org_id)}
    return {"v": 1, "ct": _mf(chaves).encrypt(json.dumps(conteudo).encode()).decode()}


def decifrar_credenciais(armazenado: Any, chaves: list[str], org_id: UUID) -> dict[str, Any]:
    if not isinstance(armazenado, dict) or armazenado.get("v") != 1 or not isinstance(armazenado.get("ct"), str):
        raise CredenciaisInvalidas("Formato inesperado (texto puro é recusado de propósito).")
    try:
        dados = json.loads(_mf(chaves).decrypt(armazenado["ct"].encode()))
    except (InvalidToken, ValueError) as exc:
        raise CredenciaisInvalidas("Não foi possível decifrar (chave mestra diferente?).") from exc
    if not isinstance(dados, dict) or dados.get("_org") != str(org_id):
        raise CredenciaisInvalidas("Credencial não pertence a esta organização.")
    dados.pop("_org", None)
    return dados


def reencriptar(armazenado: dict[str, Any], chaves: list[str]) -> dict[str, Any]:
    """Recifra com a chave mais nova (após rotação). Mantém o vínculo com a organização."""
    return {"v": 1, "ct": _mf(chaves).rotate(armazenado["ct"].encode()).decode()}


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "gerar-chave":
        print(Fernet.generate_key().decode())
    elif len(sys.argv) >= 4 and sys.argv[1] == "cifrar":
        import os

        print(json.dumps(cifrar_credenciais(json.loads(sys.argv[3]), [os.environ["CREDENTIALS_MASTER_KEY"]], UUID(sys.argv[2]))))
    else:
        print("uso: python -m app.core.crypto gerar-chave | cifrar <org_id> '<json>'")
