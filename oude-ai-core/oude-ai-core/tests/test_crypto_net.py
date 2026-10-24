import unittest
from uuid import uuid4

from cryptography.fernet import Fernet

from app.core.crypto import CredenciaisInvalidas, cifrar_credenciais, decifrar_credenciais, reencriptar
from app.core.net import UrlNaoPermitida, validar_url_externa
from app.core.ratelimit import LimitadorPorOrg

K1, K2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()


class TestCrypto(unittest.TestCase):
    def test_roundtrip_e_texto_puro_recusado(self):
        org = uuid4()
        arm = cifrar_credenciais({"api_key": "sk-x"}, [K1], org)
        self.assertNotIn("sk-x", str(arm))
        self.assertEqual(decifrar_credenciais(arm, [K1], org), {"api_key": "sk-x"})
        with self.assertRaises(CredenciaisInvalidas):
            decifrar_credenciais({"api_key": "sk-x"}, [K1], org)

    def test_blob_copiado_para_outra_org_e_recusado(self):
        a, b = uuid4(), uuid4()
        arm = cifrar_credenciais({"api_key": "sk-x"}, [K1], a)
        with self.assertRaises(CredenciaisInvalidas):
            decifrar_credenciais(arm, [K1], b)

    def test_chave_errada(self):
        org = uuid4()
        with self.assertRaises(CredenciaisInvalidas):
            decifrar_credenciais(cifrar_credenciais({"a": 1}, [K1], org), [K2], org)

    def test_rotacao(self):
        org = uuid4()
        antigo = cifrar_credenciais({"a": 1}, [K1], org)
        self.assertEqual(decifrar_credenciais(antigo, [K2, K1], org), {"a": 1})  # nova + anterior
        novo = reencriptar(antigo, [K2, K1])
        self.assertEqual(decifrar_credenciais(novo, [K2], org), {"a": 1})        # só a nova já basta


class TestNet(unittest.IsolatedAsyncioTestCase):
    async def test_ssrf(self):
        for u in ["http://exemplo.com", "https://127.0.0.1/x", "https://169.254.169.254/latest", "https://10.0.0.5",
                  "https://user:pw@exemplo.com", "https://[::1]/", "ftp://x.com", "https://localhost/"]:
            with self.assertRaises(UrlNaoPermitida, msg=u):
                await validar_url_externa(u)


class TestRate(unittest.TestCase):
    def test_limite_por_org(self):
        lim = LimitadorPorOrg(2, 60)
        self.assertTrue(lim.permitir("a", 0)); self.assertTrue(lim.permitir("a", 1))
        self.assertFalse(lim.permitir("a", 2))
        self.assertTrue(lim.permitir("b", 2))      # outra org não é afetada
        self.assertTrue(lim.permitir("a", 61))     # janela passou


if __name__ == "__main__":
    unittest.main()
