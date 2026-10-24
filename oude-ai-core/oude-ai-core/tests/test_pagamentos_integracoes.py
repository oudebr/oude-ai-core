import unittest

from tests.fakes import DONO_A, ORG_A, ORG_B, FakeHttp, FakeRepo, FakeResp
import httpx
from app.core.providers import PAGAMENTOS
from app.core.tenant import TenantContext
from app.services.integracoes import SCHEMAS, ConfigInvalida, validar_credenciais
from app.services.payments.base import EventoPagamento, PagamentoIndisponivel, Verificacao
from app.services.payments.confirmacao import ErroTemporario, confirmar_manual, confirmar_pagamento
from app.services.payments.infinitepay import InfinitePay
from app.services.payments.mercadopago import MercadoPago, reais_para_centavos
from app.services.payments.pix import PixManual, crc16_ccitt, gerar_brcode
from app.services.payments.registry import PROVEDORES_PAGAMENTO, provedor_de
from app.services.payments.webhook_auth import webhook_autorizado
from app.services.pedidos import PedidoInvalido, validar_itens
from app.workers.jobs.estoque import alerta_estoque


class FakeProvedor:
    """Provedor de teste no formato InfinitePay (evento já cita o pedido)."""
    nome, usa_webhook = "infinitepay", True

    def __init__(self, ver=None, erro=None, nome="infinitepay"):
        self.ver, self.erro, self.chamadas, self.nome = ver, erro, [], nome

    def extrair_evento(self, payload):
        if not payload.get("order_nsu") or not payload.get("transaction_nsu"):
            return None
        return EventoPagamento(transacao_id=payload["transaction_nsu"], pedido_id=payload["order_nsu"])

    async def verificar(self, http, cred, evento):
        self.chamadas.append((cred, evento.transacao_id))
        if self.erro:
            raise self.erro
        return Verificacao(**{**self.ver, "pedido_id": evento.pedido_id})


OK = {"pago": True, "valor_centavos": 1500, "pago_centavos": 1510}
CRED = {"handle": "lojaA"}


async def pedido(repo, org=ORG_A, total=1500, provedor="infinitepay"):
    return await repo.criar_pedido(org, "c", [{"descricao": "x", "quantidade": 1, "preco_centavos": total}], total, provedor)


def webhook(ped, nsu="tx1"):
    return {"order_nsu": ped["id"], "transaction_nsu": nsu}


async def confirmar(repo, prov, ped_org=ORG_A, payload=None, cred=CRED):
    return await confirmar_pagamento(repo, prov, None, cred, ped_org, payload)


class TestPagamentos(unittest.IsolatedAsyncioTestCase):
    async def test_confirma_pagamento_valido_com_a_credencial_da_org(self):
        repo = FakeRepo(); p = await pedido(repo); prov = FakeProvedor(OK)
        r = await confirmar(repo, prov, payload=webhook(p))
        self.assertEqual(r.status, "pago")
        self.assertEqual(repo.pedidos[(ORG_A, p["id"])]["status"], "pago")
        self.assertEqual(prov.chamadas[0][0], CRED)   # verificou com a credencial da organização

    async def test_webhook_sozinho_nao_basta_provedor_diz_nao_pago(self):
        repo = FakeRepo(); p = await pedido(repo)
        r = await confirmar(repo, FakeProvedor({"pago": False}), payload=webhook(p))
        self.assertEqual(r.status, "nao_pago")
        self.assertEqual(repo.pedidos[(ORG_A, p["id"])]["status"], "aguardando_pagamento")

    async def test_webhook_duplicado_e_idempotente(self):
        repo = FakeRepo(); p = await pedido(repo); prov = FakeProvedor(OK)
        a = await confirmar(repo, prov, payload=webhook(p))
        b = await confirmar(repo, prov, payload=webhook(p))
        c = await confirmar(repo, prov, payload=webhook(p, "tx-outro"))
        self.assertEqual((a.status, b.status, c.status), ("pago", "duplicado", "duplicado"))
        self.assertEqual(len(prov.chamadas), 1)
        self.assertEqual(len([x for x in repo.auditoria if x[1] == "pedido_pago"]), 1)

    async def test_chamadas_concorrentes_marcam_pago_uma_vez(self):
        import asyncio
        repo = FakeRepo(); p = await pedido(repo); prov = FakeProvedor(OK)
        rs = await asyncio.gather(*[confirmar(repo, prov, payload=webhook(p)) for _ in range(5)])
        self.assertEqual(sorted(r.status for r in rs).count("pago"), 1)

    async def test_mesmo_evento_em_voo_nao_consulta_o_provedor_duas_vezes(self):
        import asyncio

        class Lento(FakeProvedor):
            async def verificar(self, *a):
                await asyncio.sleep(0.01)
                return await super().verificar(*a)

        repo = FakeRepo(); p = await pedido(repo); prov = Lento(OK)
        await asyncio.gather(*[confirmar(repo, prov, payload=webhook(p)) for _ in range(5)])
        self.assertEqual(len(prov.chamadas), 1)
        self.assertEqual(repo.pedidos[(ORG_A, p["id"])]["status"], "pago")

    async def test_valor_divergente_nao_marca_pago(self):
        repo = FakeRepo(); p = await pedido(repo, total=9999)
        r = await confirmar(repo, FakeProvedor(OK), payload=webhook(p))
        self.assertEqual(r.status, "divergente")
        self.assertEqual(repo.pedidos[(ORG_A, p["id"])]["status"], "aguardando_pagamento")
        self.assertTrue(any(a[1] == "pagamento_divergente" for a in repo.auditoria))

    async def test_pago_menor_que_valor_ou_ausente(self):
        repo = FakeRepo(); p = await pedido(repo)
        for i, ver in enumerate(({**OK, "pago_centavos": 1000}, {**OK, "pago_centavos": None}, {**OK, "valor_centavos": None})):
            r = await confirmar(repo, FakeProvedor(ver), payload=webhook(p, f"t{i}"))
            self.assertEqual(r.status, "divergente", ver)

    async def test_falha_do_provedor_gera_erro_temporario_e_permite_retry(self):
        repo = FakeRepo(); p = await pedido(repo)
        with self.assertRaises(ErroTemporario):
            await confirmar(repo, FakeProvedor(erro=httpx.HTTPError("fora do ar")), payload=webhook(p))
        self.assertNotIn((ORG_A, "infinitepay:tx1"), repo.eventos)
        self.assertEqual((await confirmar(repo, FakeProvedor(OK), payload=webhook(p))).status, "pago")

    async def test_pedido_de_outra_org_e_invisivel(self):
        repo = FakeRepo(); pb = await pedido(repo, ORG_B); prov = FakeProvedor(OK)
        r = await confirmar(repo, prov, ORG_A, webhook(pb))
        self.assertEqual(r.status, "ignorado")
        self.assertEqual(repo.pedidos[(ORG_B, pb["id"])]["status"], "aguardando_pagamento")
        self.assertEqual(prov.chamadas, [])

    async def test_pedido_de_outro_provedor_nao_e_confirmado_por_este(self):
        repo = FakeRepo(); p = await pedido(repo, provedor="mercadopago")   # pedido do Mercado Pago
        prov = FakeProvedor(OK)                                            # webhook chega pela InfinitePay
        r = await confirmar(repo, prov, payload=webhook(p))
        self.assertEqual(r.status, "ignorado"); self.assertEqual(prov.chamadas, [])

    async def test_evento_nao_reconhecido_ou_pedido_inexistente(self):
        repo = FakeRepo(); prov = FakeProvedor(OK)
        self.assertEqual((await confirmar(repo, prov, payload={"x": 1})).status, "ignorado")
        self.assertEqual((await confirmar(repo, prov, payload={"order_nsu": "nao-existe", "transaction_nsu": "t"})).status, "ignorado")

    async def test_pedido_do_provedor_diferente_do_evento_diverge(self):
        class Trocado(FakeProvedor):
            async def verificar(self, http, cred, evento):
                return Verificacao(pago=True, pedido_id="outro-pedido", valor_centavos=1500, pago_centavos=1500)

        repo = FakeRepo(); p = await pedido(repo)
        self.assertEqual((await confirmar(repo, Trocado(OK), payload=webhook(p))).status, "divergente")

    async def test_confirmacao_manual_so_pix_e_idempotente(self):
        repo = FakeRepo(); pix = await pedido(repo, provedor="pix"); mp = await pedido(repo, provedor="mercadopago")
        self.assertEqual((await confirmar_manual(repo, ORG_A, DONO_A, pix["id"])).status, "pago")
        self.assertEqual((await confirmar_manual(repo, ORG_A, DONO_A, pix["id"])).status, "duplicado")
        self.assertEqual((await confirmar_manual(repo, ORG_A, DONO_A, mp["id"])).status, "ignorado")   # não é Pix
        self.assertEqual(repo.pedidos[(ORG_A, mp["id"])]["status"], "aguardando_pagamento")
        pb = await pedido(repo, ORG_B, provedor="pix")
        self.assertEqual((await confirmar_manual(repo, ORG_A, DONO_A, pb["id"])).status, "ignorado")   # outra org


class TestProvedoresReais(unittest.IsolatedAsyncioTestCase):
    async def test_mercadopago_eventos_e_verificacao(self):
        mp = MercadoPago()
        self.assertIsNone(mp.extrair_evento({"type": "merchant_order", "data": {"id": "1"}}))
        self.assertIsNone(mp.extrair_evento({"type": "payment", "data": {"id": "abc"}}))
        ev = mp.extrair_evento({"type": "payment", "data": {"id": "999"}})
        self.assertEqual((ev.transacao_id, ev.pedido_id), ("999", None))
        http = FakeHttpGet(FakeResp({"status": "approved", "transaction_amount": 15.5, "external_reference": "PED1", "currency_id": "BRL"}))
        v = await mp.verificar(http, {"access_token": "APP_USR-token-do-lojista"}, ev)
        self.assertEqual((v.pago, v.pedido_id, v.valor_centavos), (True, "PED1", 1550))
        self.assertEqual(http.chamadas[0][0], "https://api.mercadopago.com/v1/payments/999")
        self.assertEqual(http.chamadas[0][1]["Authorization"], "Bearer APP_USR-token-do-lojista")   # credencial da org
        for resp in ({"status": "pending", "transaction_amount": 10, "currency_id": "BRL"},
                     {"status": "approved", "transaction_amount": 10, "currency_id": "USD"},
                     {"status": "rejected", "transaction_amount": 10, "currency_id": "BRL"}):
            self.assertFalse((await mp.verificar(FakeHttpGet(FakeResp(resp)), {"access_token": "x" * 12}, ev)).pago)
        with self.assertRaises(PagamentoIndisponivel):
            await mp.verificar(FakeHttpGet(FakeResp({}, 500)), {"access_token": "x" * 12}, ev)

    def test_reais_para_centavos_exato(self):
        self.assertEqual(reais_para_centavos(10), 1000); self.assertEqual(reais_para_centavos(0.1 + 0.2), None)
        self.assertEqual(reais_para_centavos("19.90"), 1990); self.assertIsNone(reais_para_centavos(10.005))
        self.assertIsNone(reais_para_centavos(True)); self.assertIsNone(reais_para_centavos(None))

    async def test_mercadopago_cria_preferencia_com_pedido_como_referencia(self):
        http = FakeHttp(FakeResp({"id": "p1", "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=p1"}))
        c = await MercadoPago().criar_cobranca(http, {"access_token": "APP_USR-x1234567"}, "PED1",
                                               [{"descricao": "Oud", "quantidade": 2, "preco_centavos": 15050}], 30100, "https://core/wh")
        url, headers, corpo = http.chamadas[0]
        self.assertEqual(url, "https://api.mercadopago.com/checkout/preferences")
        self.assertEqual(corpo["external_reference"], "PED1"); self.assertEqual(corpo["notification_url"], "https://core/wh")
        self.assertEqual(corpo["items"][0], {"title": "Oud", "quantity": 2, "unit_price": 150.5, "currency_id": "BRL"})
        self.assertTrue(c.url.startswith("https://"))

    async def test_infinitepay_adaptador(self):
        ip = InfinitePay()
        self.assertIsNone(ip.extrair_evento({"order_nsu": "x"}))
        ev = ip.extrair_evento({"order_nsu": "P1", "transaction_nsu": "T1", "invoice_slug": "S1"})
        http = FakeHttp(FakeResp({"success": True, "paid": True, "amount": 1500, "paid_amount": 1510}))
        v = await ip.verificar(http, {"handle": "lojaA"}, ev)
        self.assertEqual((v.pago, v.valor_centavos, v.pago_centavos, v.pedido_id), (True, 1500, 1510, "P1"))
        self.assertEqual(http.chamadas[0][2], {"handle": "lojaA", "order_nsu": "P1", "transaction_nsu": "T1", "slug": "S1"})
        http2 = FakeHttp(FakeResp({"success": True, "paid": True, "amount": "1500", "paid_amount": True}))
        v2 = await ip.verificar(http2, {"handle": "lojaA"}, ev)
        self.assertEqual((v2.valor_centavos, v2.pago_centavos), (None, None))   # tipos estranhos não passam

    def test_pix_brcode_crc_e_estrutura(self):
        self.assertEqual(crc16_ccitt("123456789"), "29B1")     # vetor padrão CRC-16/CCITT-FALSE
        code = gerar_brcode("loja@exemplo.com", "Perfumaria Á Ç", "São Paulo", 15050, "abcdef-123")
        corpo, crc = code[:-4], code[-4:]
        self.assertEqual(crc16_ccitt(corpo), crc)
        self.assertIn("0014br.gov.bcb.pix0116loja@exemplo.com", code)
        self.assertIn("5406150.50", code); self.assertIn("5802BR", code); self.assertIn("5303986", code)
        self.assertIn("PERFUMARIA A C", code); self.assertIn("SAO PAULO", code)
        with self.assertRaises(ValueError):
            gerar_brcode("k", "n", "c", 0, "t")

    async def test_pix_nao_tem_webhook(self):
        p = PixManual()
        self.assertFalse(p.usa_webhook); self.assertIsNone(p.extrair_evento({"qualquer": "coisa"}))
        c = await p.criar_cobranca(None, {"chave_pix": "11999990000", "nome_recebedor": "Loja", "cidade": "Rio"}, "abc-def", [], 1000, "")
        self.assertTrue(c.copia_e_cola.startswith("000201")); self.assertIn("manualmente", c.aviso)

    def test_autenticacao_do_webhook(self):
        mp, pix, cred = provedor_de("mercadopago"), provedor_de("pix"), {"webhook_secret": "segredo-da-org-a"}
        self.assertTrue(webhook_autorizado(mp, cred, "segredo-da-org-a"))
        self.assertFalse(webhook_autorizado(mp, cred, "segredo-errado"))
        self.assertFalse(webhook_autorizado(mp, cred, ""))
        self.assertFalse(webhook_autorizado(mp, None, "segredo-da-org-a"))                  # org sem a integração
        self.assertFalse(webhook_autorizado(mp, {"webhook_secret": ""}, ""))                # segredo vazio nunca vale
        self.assertFalse(webhook_autorizado(None, cred, "segredo-da-org-a"))                # provedor inexistente
        self.assertFalse(webhook_autorizado(pix, cred, "segredo-da-org-a"))                 # Pix não tem webhook

    def test_registro_tem_os_tres_meios(self):
        self.assertEqual(set(PROVEDORES_PAGAMENTO), set(PAGAMENTOS))


class FakeHttpGet(FakeHttp):
    async def get(self, url, headers=None, params=None):
        self.chamadas.append((url, headers, params))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestValidacoes(unittest.TestCase):
    def test_pedido(self):
        itens, total = validar_itens([{"descricao": "Oud", "quantidade": 2, "preco_centavos": 15000}])
        self.assertEqual(total, 30000)
        for ruim in (None, [], "x", [{"descricao": "", "quantidade": 1, "preco_centavos": 1}],
                     [{"descricao": "a", "quantidade": 0, "preco_centavos": 1}],
                     [{"descricao": "a", "quantidade": 1, "preco_centavos": 10.5}],
                     [{"descricao": "a", "quantidade": True, "preco_centavos": 1}],
                     [{"descricao": "a", "quantidade": 1, "preco_centavos": -5}]):
            with self.assertRaises(PedidoInvalido):
                validar_itens(ruim)

    def test_integracoes(self):
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("llm", {"provider": "desconhecido", "model": "m", "api_key": "12345678"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("supplier", {"catalog_url": "http://inseguro.com/x"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("supplier", {"catalog_url": "https://u:p@f.com/x"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("whatsapp", {"phone_number_id": "abc", "access_token": "x" * 20, "notify_to": "5521999990000"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("inexistente", {})
        limpo = validar_credenciais("llm", {"provider": "GROQ", "model": "m", "api_key": "12345678", "extra": "descartado"})
        self.assertEqual(set(limpo), {"provider", "model", "api_key"})
        for prov, corpo in (("infinitepay", {"handle": "$minhaloja"}), ("mercadopago", {"access_token": "APP_USR-1234567890"})):
            p1 = validar_credenciais(prov, corpo)
            p2 = validar_credenciais(prov, corpo, existente=p1)
            self.assertEqual(p1["webhook_secret"], p2["webhook_secret"])   # re-salvar não invalida o webhook já enviado
        self.assertEqual(validar_credenciais("infinitepay", {"handle": "$minhaloja"})["handle"], "minhaloja")
        pix = validar_credenciais("pix", {"chave_pix": "loja@x.com", "nome_recebedor": "Loja", "cidade": "Rio", "x": 1})
        self.assertEqual(set(pix), {"chave_pix", "nome_recebedor", "cidade"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("pix", {"chave_pix": "a b", "nome_recebedor": "L", "cidade": "R"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("mercadopago", {"access_token": "curto"})
        with self.assertRaises(ConfigInvalida):
            validar_credenciais("payments", {"handle": "x"})   # nome antigo não existe mais

    def test_whatsapp_provedores(self):
        base = {"notify_to": "(21) 99999-0000"}
        ev = validar_credenciais("whatsapp", {**base, "provider": "evolution", "base_url": "https://evo.exemplo.com/", "instance": "loja-1", "apikey": "abcd1234"})
        self.assertEqual((ev["base_url"], ev["notify_to"]), ("https://evo.exemplo.com", "21999990000"))
        z = validar_credenciais("whatsapp", {**base, "provider": "zapi", "instance_id": "3A1B", "token": "T0K3N", "client_token": "ct12345"})
        self.assertEqual(z["client_token"], "ct12345")
        for ruim in ({"provider": "evolution", "base_url": "http://evo.com", "instance": "i", "apikey": "abcd"},
                     {"provider": "evolution", "base_url": "https://evo.com", "instance": "../x", "apikey": "abcd"},
                     {"provider": "zapi", "instance_id": "a/b", "token": "t"},
                     {"provider": "zapi", "instance_id": "ok", "token": "t?x=1"},
                     {"provider": "telegram"}):
            with self.assertRaises(ConfigInvalida, msg=ruim):
                validar_credenciais("whatsapp", {**base, **ruim})

    def test_todo_provedor_tem_formulario_sem_vazar_segredo(self):
        self.assertTrue(all(SCHEMAS[x]["campos"] for x in SCHEMAS))
        self.assertTrue(any(c["secreto"] for c in SCHEMAS["llm"]["campos"]))


META = {"provider": "meta_cloud", "phone_number_id": "123456", "access_token": "tok-tok-tok-1", "notify_to": "5521999990000"}


class TestEstoque(unittest.IsolatedAsyncioTestCase):
    def item(self):
        return {"id": "i1", "nome": "Oud", "estoque": 1, "estoque_minimo": 5}

    async def test_sem_whatsapp_vai_para_o_painel_e_nao_duplica(self):
        repo = FakeRepo(); repo.estoque[ORG_A] = [self.item()]
        t = TenantContext(org_id=ORG_A)
        self.assertEqual(await alerta_estoque(repo, FakeHttp(), t), 1)
        self.assertEqual(await alerta_estoque(repo, FakeHttp(), t), 0)    # segunda chamada no mesmo dia: dedupe
        self.assertEqual(len(repo.notificacoes), 1)

    async def test_falha_do_whatsapp_libera_para_retry_e_avisa_no_painel(self):
        repo = FakeRepo(); repo.estoque[ORG_A] = [self.item()]
        t = TenantContext(org_id=ORG_A, credenciais={"whatsapp": META})
        n = await alerta_estoque(repo, FakeHttp(FakeResp({}, 500)), t)
        self.assertEqual(n, 0)
        self.assertEqual(len(repo.notificacoes), 1)       # fallback no painel
        self.assertEqual(repo.execucoes, set())           # reserva liberada => tenta de novo na próxima rodada
        n2 = await alerta_estoque(repo, FakeHttp(FakeResp({})), t)
        self.assertEqual(n2, 1)

    async def test_whatsapp_ok_envia_ao_responsavel_com_token_do_lojista(self):
        repo = FakeRepo(); repo.estoque[ORG_A] = [self.item()]
        http = FakeHttp(FakeResp({}))
        t = TenantContext(org_id=ORG_A, credenciais={"whatsapp": META})
        await alerta_estoque(repo, http, t)
        url, headers, corpo = http.chamadas[0]
        self.assertIn("/123456/messages", url)
        self.assertEqual(headers["Authorization"], "Bearer tok-tok-tok-1")
        self.assertEqual(corpo["to"], "5521999990000")
        self.assertEqual(repo.notificacoes, [])


if __name__ == "__main__":
    unittest.main()
