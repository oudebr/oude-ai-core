import unittest
from types import SimpleNamespace
from uuid import uuid4

from tests.fakes import ORG_A, ORG_B, FakeHttp, FakeResp
import httpx
from app.core.tenant import TenantContext
from app.db.repositories import TenantRepository
from app.services.whatsapp.client import (EvolutionWhatsApp, MetaCloudWhatsApp, WhatsAppIndisponivel, ZApiWhatsApp,
                                          testar_conexao, whatsapp_do_tenant)


def ctx(cred):
    return TenantContext(org_id=ORG_A, credenciais={"whatsapp": cred} if cred else {})


class TestWhatsApp(unittest.IsolatedAsyncioTestCase):
    def test_fabrica_escolhe_o_adaptador_pelas_credenciais_da_org(self):
        http = FakeHttp()
        self.assertIsNone(whatsapp_do_tenant(http, ctx(None)))                         # sem integração => fallback no painel
        self.assertIsInstance(whatsapp_do_tenant(http, ctx({"provider": "meta_cloud", "phone_number_id": "1", "access_token": "t"})), MetaCloudWhatsApp)
        self.assertIsInstance(whatsapp_do_tenant(http, ctx({"provider": "evolution", "base_url": "https://e.com", "instance": "i", "apikey": "k"})), EvolutionWhatsApp)
        self.assertIsInstance(whatsapp_do_tenant(http, ctx({"provider": "zapi", "instance_id": "i", "token": "t"})), ZApiWhatsApp)
        self.assertIsNone(whatsapp_do_tenant(http, ctx({"provider": "evolution", "instance": "i"})))   # incompleto
        self.assertIsNone(whatsapp_do_tenant(http, ctx({"provider": "desconhecido"})))

    async def test_evolution_formato_da_documentacao(self):
        http = FakeHttp(FakeResp({"status": "PENDING"}, 201))
        await EvolutionWhatsApp(http, "https://8.8.8.8", "loja-1", "chave-evo").enviar_texto("+55 (21) 99999-0000", "olá")
        url, headers, corpo = http.chamadas[0]
        self.assertEqual(url, "https://8.8.8.8/message/sendText/loja-1")
        self.assertEqual(headers, {"apikey": "chave-evo"})
        self.assertEqual(corpo, {"number": "5521999990000", "text": "olá"})

    async def test_evolution_bloqueia_servidor_interno_ssrf(self):
        http = FakeHttp()
        for base in ("https://127.0.0.1", "https://169.254.169.254", "https://10.1.2.3:8080"):
            with self.assertRaises(WhatsAppIndisponivel):
                await EvolutionWhatsApp(http, base, "i", "k").enviar_texto("5521999990000", "x")
        self.assertEqual(http.chamadas, [])    # nem chegou a chamar

    async def test_zapi_formato_da_documentacao(self):
        http = FakeHttp(FakeResp({"zaapId": "1", "messageId": "2", "id": "2"}))
        await ZApiWhatsApp(http, "INST1", "TOK1", "cli-token").enviar_texto("5521999990000", "olá")
        url, headers, corpo = http.chamadas[0]
        self.assertEqual(url, "https://api.z-api.io/instances/INST1/token/TOK1/send-text")
        self.assertEqual(headers, {"Client-Token": "cli-token"})
        self.assertEqual(corpo, {"phone": "5521999990000", "message": "olá"})
        http2 = FakeHttp(FakeResp({}))
        await ZApiWhatsApp(http2, "I", "T", None).enviar_texto("5521999990000", "x")
        self.assertEqual(http2.chamadas[0][1], {})      # sem Client-Token configurado, sem header

    async def test_falhas_viram_whatsapp_indisponivel_sem_vazar_credencial(self):
        for adaptador in (MetaCloudWhatsApp(FakeHttp(FakeResp({}, 500)), "123456", "segredo-meta"),
                          ZApiWhatsApp(FakeHttp(httpx.HTTPError("token TOK1 inválido")), "I", "TOK1", None),
                          EvolutionWhatsApp(FakeHttp(FakeResp({}, 401)), "https://8.8.8.8", "i", "segredo-evo")):
            with self.assertRaises(WhatsAppIndisponivel) as cm:
                await adaptador.enviar_texto("5521999990000", "x")
            for segredo in ("segredo-meta", "TOK1", "segredo-evo"):
                self.assertNotIn(segredo, str(cm.exception))
        with self.assertRaises(WhatsAppIndisponivel):
            await ZApiWhatsApp(FakeHttp(), "I", "T", None).enviar_texto("123", "x")   # telefone inválido

    async def test_testar_conexao(self):
        ok, _ = await testar_conexao(None, {"provider": "zapi", "instance_id": "i", "token": "t"})
        self.assertIsNone(ok)    # sem endpoint confirmado: não finge teste
        ok, det = await testar_conexao(None, {"provider": "evolution", "base_url": "https://127.0.0.1", "instance": "i", "apikey": "k"})
        self.assertFalse(ok)     # SSRF bloqueado também no teste


class QueryGravador:
    """Cliente Supabase falso que GRAVA a cadeia de filtros, para provar o isolamento no repositório REAL."""

    def __init__(self, dados=None):
        self.chamadas, self.dados = [], dados or []

    def table(self, nome):
        self.chamadas = [("table", (nome,))]
        return self

    def __getattr__(self, nome):
        def f(*a, **k):
            self.chamadas.append((nome, a))
            return self
        return f

    async def execute(self):
        return SimpleNamespace(data=self.dados)

    def filtros(self):
        return {a for n, a in self.chamadas if n == "eq"}


class TestRepositorioIsolamento(unittest.IsolatedAsyncioTestCase):
    async def chamar(self, metodo, *args, dados=None, coluna="org_id"):
        db = QueryGravador(dados)
        repo = TenantRepository(db)
        res = await getattr(repo, metodo)(*args)
        self.assertIn((coluna, str(ORG_A)), db.filtros(), f"{metodo} sem filtro de organização")
        return res, db

    async def test_toda_consulta_por_org_filtra_pela_org(self):
        u = uuid4()
        casos = [
            ("integracoes", (ORG_A,), "org_id"), ("get_config", (ORG_A,), "org_id"), ("historico", (ORG_A, "c"), "org_id"),
            ("get_pedido", (ORG_A, "p"), "org_id"), ("remover_integracao", (ORG_A, "llm"), "org_id"),
            ("salvar_checkout", (ORG_A, "p", {}), "org_id"), ("liberar_evento_pagamento", (ORG_A, "k"), "org_id"),
            ("marcar_pedido_pago", (ORG_A, "p", "t", 1), "org_id"), ("liberar_execucao", (ORG_A, "k"), "org_id"),
            ("papel_do_usuario", (ORG_A, u), "org_id"),
            # catálogo e estoque: tabela `produtos`, onde a coluna de dono é user_id
            ("buscar_catalogo", (ORG_A, "oud"), "user_id"), ("estoque_baixo", (ORG_A,), "user_id"),
        ]
        for metodo, args, coluna in casos:
            await self.chamar(metodo, *args, coluna=coluna)

    async def test_catalogo_e_estoque_usam_produtos_e_nao_inventory(self):
        _, db = await self.chamar("buscar_catalogo", ORG_A, "oud", coluna="user_id")
        self.assertEqual(db.chamadas[0], ("table", ("produtos",)))
        _, db = await self.chamar("estoque_baixo", ORG_A, coluna="user_id")
        self.assertEqual(db.chamadas[0], ("table", ("produtos",)))

    async def test_busca_sanitiza_termo_para_nao_quebrar_o_filtro(self):
        _, db = await self.chamar("buscar_catalogo", ORG_A, "oud),user_id.eq.outro(*%", coluna="user_id")
        filtro = [a[0] for n, a in db.chamadas if n == "or_"][0]
        self.assertNotIn("user_id.eq", filtro)           # injeção de filtro PostgREST neutralizada
        self.assertEqual(filtro.count("("), 0); self.assertEqual(filtro.count(")"), 0)

    async def test_estoque_baixo_compara_estoque_com_minimo(self):
        dados = [{"id": 1, "nome": "a", "estoque": 1, "estoque_minimo": 3}, {"id": 2, "nome": "b", "estoque": 9, "estoque_minimo": 3},
                 {"id": 3, "nome": "c", "estoque": 3, "estoque_minimo": 3}]
        res, _ = await self.chamar("estoque_baixo", ORG_A, dados=dados, coluna="user_id")
        self.assertEqual([i["id"] for i in res], [1, 3])

    async def test_gravacoes_carregam_a_org(self):
        db = QueryGravador([{"id": "p"}])
        repo = TenantRepository(db)
        await repo.criar_pedido(ORG_B, "c", [], 100, "pix")
        self.assertEqual(db.chamadas[1][0], "insert"); self.assertEqual(db.chamadas[1][1][0]["org_id"], str(ORG_B))
        self.assertEqual(db.chamadas[1][1][0]["provedor"], "pix")
        await repo.salvar_integracao(ORG_B, "llm", {"v": 1, "ct": "x"})
        self.assertEqual(db.chamadas[1][1][0]["org_id"], str(ORG_B))
        with self.assertRaises(ValueError):
            await repo.salvar_integracao(ORG_B, "payments", {})   # provider fora do catálogo é recusado


if __name__ == "__main__":
    unittest.main()
