"""Testes de ROTAS (FastAPI). Exigem `pip install -r requirements.txt`; sem fastapi, são PULADOS (não passam em silêncio:
o unittest os reporta como skipped). Usam FakeRepo, sem rede e sem Supabase."""
import time
import unittest
from types import SimpleNamespace

from tests.fakes import DONO_A, MEMBRO_A, ORG_A, ORG_B, FakeHttp, FakeRepo, completion

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    HAS = True
except ImportError:
    HAS = False

SEG = "segredo-atual-com-mais-de-32-caracteres!!"


@unittest.skipUnless(HAS, "fastapi não instalado")
class TestRotas(unittest.TestCase):
    def setUp(self):
        from cryptography.fernet import Fernet
        from app.api.routes import tenants, webhooks
        from app.core.auth import CacheJti
        from app.core.ratelimit import LimitadorPorOrg

        self.chave = Fernet.generate_key().decode()
        self.repo = FakeRepo()
        self.repo.membros = {(ORG_A, DONO_A): "dono", (ORG_A, MEMBRO_A): "membro"}
        s = SimpleNamespace(segredos_crm=lambda: [SEG], chaves_mestras=lambda: [self.chave], token_ttl_max_seconds=120,
                            public_base_url="https://core.exemplo.com", max_concurrent_tenants=2)
        app = FastAPI()
        app.state.repo, app.state.settings, app.state.cache_jti = self.repo, s, CacheJti()
        app.state.http, app.state.limitador = FakeHttp(), LimitadorPorOrg(2)
        app.include_router(tenants.router); app.include_router(webhooks.router)
        self.app, self.c = app, TestClient(app)

    def hdr(self, user=DONO_A, org=ORG_A):
        from app.core.auth import assinar_token
        agora = time.time()
        t = assinar_token({"iss": "oude-crm", "aud": "oude-ai-core", "sub": str(user), "org": str(org),
                           "jti": f"j{time.time_ns()}", "iat": agora, "exp": agora + 60}, SEG)
        return {"Authorization": f"Bearer {t}"}

    def test_sem_token_401_e_org_errada_403(self):
        self.assertEqual(self.c.get(f"/v1/tenants/{ORG_A}/agente").status_code, 401)
        self.assertEqual(self.c.get(f"/v1/tenants/{ORG_B}/agente", headers=self.hdr()).status_code, 403)

    def test_membro_nao_edita_agente_nem_integracoes(self):
        self.assertEqual(self.c.put(f"/v1/tenants/{ORG_A}/agente", json={"tom": "formal"}, headers=self.hdr(MEMBRO_A)).status_code, 403)
        self.assertEqual(self.c.get(f"/v1/tenants/{ORG_A}/integracoes", headers=self.hdr(MEMBRO_A)).status_code, 403)

    def test_fluxo_da_tela_salvar_ativar_conversar_pausar(self):
        base = f"/v1/tenants/{ORG_A}"
        r = self.c.put(f"{base}/agente", json={"instrucoes": "Ofereça decant.", "tom": "consultivo"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post(f"{base}/agente/ativar", headers=self.hdr()).status_code, 409)  # sem chave de IA
        r = self.c.put(f"{base}/integracoes/llm", json={"provider": "openai", "model": "m", "api_key": "sk-12345678"}, headers=self.hdr())
        self.assertEqual(r.status_code, 200); self.assertNotIn("sk-12345678", r.text)
        self.assertEqual(self.c.post(f"{base}/agente/ativar", headers=self.hdr()).json(), {"ativo": True})
        # a chave foi gravada CIFRADA: o repositório (banco) não contém o texto puro
        self.assertNotIn("sk-12345678", str(self.repo.salvos["llm"]))
        self.assertEqual(self.c.get(f"{base}/agente", headers=self.hdr()).json()["instrucoes"], "Ofereça decant.")
        self.assertEqual(self.c.post(f"{base}/agente/pausar", headers=self.hdr()).json(), {"ativo": False})
        self.assertEqual(self.c.post(f"{base}/chat", json={"mensagem": "oi"}, headers=self.hdr()).status_code, 409)  # pausado

    def test_webhook_com_segredo_errado_404(self):
        self.assertEqual(self.c.post(f"/webhooks/infinitepay/{ORG_A}/segredo-errado", json={}).status_code, 404)
        self.assertEqual(self.c.post(f"/webhooks/infinitepay/{ORG_B}/qualquer", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
