"""Núcleo: criptografia das credenciais e subida da aplicação (pytest).

Substitui a versão antiga deste arquivo, que usava a assinatura de 2 argumentos de `cifrar_credenciais` e a
autenticação por `X-Internal-Token` (as rotas de lojista agora exigem `Authorization: Bearer <token assinado>`).
O teste de subida usa variáveis de ambiente FALSAS (nada de Supabase real) e não liga o scheduler.
"""
import time
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import CredenciaisInvalidas, cifrar_credenciais, decifrar_credenciais

CHAVE = Fernet.generate_key().decode()
SEGREDO_CRM = "segredo-crm-de-teste-com-mais-de-32-caracteres"


def test_crypto_roundtrip_e_recusa_texto_puro():
    org = uuid4()
    arm = cifrar_credenciais({"api_key": "sk-x"}, [CHAVE], org)          # chaves = lista; org_id obrigatório
    assert "sk-x" not in str(arm)
    assert decifrar_credenciais(arm, [CHAVE], org) == {"api_key": "sk-x"}
    with pytest.raises(CredenciaisInvalidas):
        decifrar_credenciais({"api_key": "sk-x"}, [CHAVE], org)          # texto puro é recusado
    with pytest.raises(CredenciaisInvalidas):
        decifrar_credenciais(arm, [CHAVE], uuid4())                      # outra organização é recusada
    with pytest.raises(CredenciaisInvalidas):
        decifrar_credenciais(arm, [Fernet.generate_key().decode()], org)  # chave errada


@pytest.fixture
def cliente(monkeypatch):
    """App com ambiente falso: sem rede, sem Supabase real, sem scheduler. As variáveis do ambiente têm
    prioridade sobre o `.env`, então as chaves reais do seu .env não são usadas aqui."""
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("SUPABASE_URL", "https://exemplo-teste.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiJ9.e30.x")
    monkeypatch.setenv("CRM_SHARED_SECRET", SEGREDO_CRM)
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", CHAVE)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://core-teste.onrender.com")
    monkeypatch.setenv("RUN_SCHEDULER", "false")
    get_settings.cache_clear()
    from main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _token(segredo: str, org) -> str:
    from app.core.auth import assinar_token

    agora = time.time()
    return assinar_token({"iss": "oude-crm", "aud": "oude-ai-core", "sub": str(uuid4()), "org": str(org),
                          "jti": str(uuid4()), "iat": agora, "exp": agora + 60}, segredo)


def test_app_sobe_e_protege_rotas(cliente):
    org = uuid4()
    assert cliente.get("/health").json() == {"status": "ok"}
    assert cliente.get("/nao-existe").status_code == 404                                   # rota inexistente => 404
    rota = f"/v1/tenants/{org}/agente"
    assert cliente.get(rota).status_code == 401                                            # sem token
    assert cliente.get(rota, headers={"X-Internal-Token": "qualquer"}).status_code == 401  # o token estático NÃO abre rotas de lojista
    assert cliente.get(rota, headers={"Authorization": f"Bearer {_token('segredo-errado-' * 3, org)}"}).status_code == 401
    assert cliente.post("/v1/tenants/nao-uuid/chat", json={"mensagem": "oi"}).status_code == 422
    # webhook: segredo/organização desconhecidos respondem 404 (sem revelar quais organizações existem)
    # (a consulta ao Supabase só ocorre depois da checagem de formato; aqui o corpo é inválido de propósito)