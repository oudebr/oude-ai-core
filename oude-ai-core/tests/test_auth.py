import time
import unittest

from tests.fakes import DONO_A, DONO_B, MEMBRO_A, ORG_A, ORG_B, FakeRepo
from app.core.auth import (CacheJti, NaoAutenticado, SemPermissao, assinar_token, autorizar_requisicao, verificar_token)

SEG = "segredo-atual-com-mais-de-32-caracteres!!"
ANTIGO = "segredo-antigo-com-mais-de-32-caracteres!"


def claims(user=DONO_A, org=ORG_A, **kw):
    agora = time.time()
    base = {"iss": "oude-crm", "aud": "oude-ai-core", "sub": str(user), "org": str(org), "jti": f"j{agora}{id(kw)}",
            "iat": agora, "exp": agora + 60}
    base.update(kw)
    return base


class TestAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.repo.membros = {(ORG_A, DONO_A): "dono", (ORG_A, MEMBRO_A): "membro", (ORG_B, DONO_B): "dono"}
        self.cache = CacheJti()

    async def aut(self, token, org=ORG_A, escopo="agente.escrever", metodo="PUT", segredos=None):
        return await autorizar_requisicao(self.repo, token, org, escopo, metodo, segredos or [SEG], self.cache)

    async def test_token_valido(self):
        p = await self.aut(assinar_token(claims(), SEG))
        self.assertEqual((p.user_id, p.org_id, p.papel), (DONO_A, ORG_A, "dono"))

    async def test_sem_token_ou_malformado(self):
        for t in (None, "", "abc", "a.b.c"):
            with self.assertRaises(NaoAutenticado):
                await self.aut(t)

    async def test_assinatura_adulterada_e_segredo_errado(self):
        t = assinar_token(claims(), SEG)
        corpo = t.split(".")
        with self.assertRaises(NaoAutenticado):
            await self.aut(f"{corpo[0]}.{corpo[1]}x.{corpo[2]}")
        with self.assertRaises(NaoAutenticado):
            await self.aut(assinar_token(claims(), "outro-segredo-completamente-diferente-123"))

    async def test_alg_none_recusado(self):
        import base64, json
        b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        with self.assertRaises(NaoAutenticado):
            await self.aut(f"{b({'alg': 'none'})}.{b(claims())}.")

    async def test_expirado_e_ttl_longo_e_audience(self):
        agora = time.time()
        with self.assertRaises(NaoAutenticado):
            await self.aut(assinar_token(claims(iat=agora - 500, exp=agora - 400), SEG))
        with self.assertRaises(NaoAutenticado):
            await self.aut(assinar_token(claims(exp=agora + 3600), SEG))
        with self.assertRaises(NaoAutenticado):
            await self.aut(assinar_token(claims(aud="outro"), SEG))

    async def test_rotacao_aceita_segredo_anterior(self):
        t = assinar_token(claims(), ANTIGO)
        p = await self.aut(t, segredos=[SEG, ANTIGO])
        self.assertEqual(p.papel, "dono")
        with self.assertRaises(NaoAutenticado):
            await self.aut(assinar_token(claims(), ANTIGO), segredos=[SEG])

    async def test_token_de_uma_org_nao_vale_para_outra(self):
        t = assinar_token(claims(DONO_A, ORG_A), SEG)
        with self.assertRaises(SemPermissao):
            await self.aut(t, org=ORG_B)

    async def test_membro_de_duas_orgs_token_de_uma_nao_abre_a_outra(self):
        self.repo.membros[(ORG_B, DONO_A)] = "dono"   # DONO_A pertence às DUAS orgs: só a trava do token protege
        t = assinar_token(claims(DONO_A, ORG_A), SEG)
        with self.assertRaises(SemPermissao):
            await self.aut(t, org=ORG_B)

    async def test_usuario_de_outra_org_nao_acessa(self):
        t = assinar_token(claims(DONO_B, ORG_A), SEG)  # token "legítimo", mas DONO_B não é membro da ORG_A
        with self.assertRaises(SemPermissao):
            await self.aut(t)

    async def test_papel_vem_do_banco_nao_do_token(self):
        t = assinar_token(claims(MEMBRO_A, ORG_A, role="dono", papel="dono"), SEG)
        with self.assertRaises(SemPermissao):
            await self.aut(t, escopo="integracoes.escrever")
        p = await self.aut(assinar_token(claims(MEMBRO_A, ORG_A), SEG), escopo="chat", metodo="POST")
        self.assertEqual(p.papel, "membro")

    async def test_matriz_de_permissoes_do_membro(self):
        for escopo, ok in [("chat", True), ("agente.ler", True), ("pedidos.ler", True), ("agente.escrever", False),
                           ("integracoes.ler", False), ("integracoes.testar", False), ("jobs.rodar", False),
                           ("pedidos.escrever", False)]:
            t = assinar_token(claims(MEMBRO_A, ORG_A), SEG)
            if ok:
                await self.aut(t, escopo=escopo, metodo="GET")
            else:
                with self.assertRaises(SemPermissao, msg=escopo):
                    await self.aut(t, escopo=escopo, metodo="GET")

    async def test_escopo_desconhecido_falha_fechado(self):
        with self.assertRaises(SemPermissao):
            await self.aut(assinar_token(claims(), SEG), escopo="admin.tudo")

    async def test_replay_bloqueado_em_escrita_mas_nao_em_leitura(self):
        t = assinar_token(claims(), SEG)
        await self.aut(t, escopo="integracoes.escrever", metodo="PUT")
        with self.assertRaises(NaoAutenticado):
            await self.aut(t, escopo="integracoes.escrever", metodo="PUT")
        t2 = assinar_token(claims(), SEG)
        await self.aut(t2, escopo="agente.ler", metodo="GET")
        await self.aut(t2, escopo="agente.ler", metodo="GET")

    def test_verificar_token_direto(self):
        self.assertEqual(verificar_token(assinar_token(claims(), SEG), [SEG])["iss"], "oude-crm")


if __name__ == "__main__":
    unittest.main()
