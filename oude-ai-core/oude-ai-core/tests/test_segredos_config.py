import re
import unittest
from pathlib import Path

from tests.fakes import ORG_A
from app.core.tenant import TenantContext

RAIZ = Path(__file__).resolve().parent.parent
SEGREDOS = ("SUPABASE_SERVICE_ROLE_KEY", "CRM_SHARED_SECRET", "CREDENTIALS_MASTER_KEY")


class TestSegredos(unittest.TestCase):
    def test_render_nao_tem_valor_fixo_para_segredos(self):
        linhas = (RAIZ / "render.yaml").read_text().splitlines()
        for chave in SEGREDOS + ("SUPABASE_URL", "PUBLIC_BASE_URL"):
            i = next(n for n, l in enumerate(linhas) if l.strip() == f"- key: {chave}")
            self.assertEqual(linhas[i + 1].split("#")[0].strip(), "sync: false", f"{chave} deve ser preenchida no painel da Render")

    def test_codigo_nao_tem_segredo_hardcoded(self):
        padroes = [r"eyJhbGci[\w-]{20,}", r"sk-[A-Za-z0-9]{20,}", r"sb_secret_\w+", r"-----BEGIN [A-Z ]*PRIVATE KEY"]
        for arq in list((RAIZ / "app").rglob("*.py")) + [RAIZ / "main.py", RAIZ / ".env.example", RAIZ / "render.yaml"]:
            texto = arq.read_text()
            for p in padroes:
                self.assertIsNone(re.search(p, texto), f"{arq.name} parece conter segredo ({p})")

    def test_variaveis_globais_so_sao_lidas_em_config_py(self):
        # o resto do código nunca toca os.environ/getenv (nada de segredo de servidor vai para serviços/prompts)
        for arq in (RAIZ / "app").rglob("*.py"):
            if arq.name in ("config.py", "crypto.py"):   # crypto.py: só o CLI de apoio lê CREDENTIALS_MASTER_KEY
                continue
            self.assertNotIn("os.environ", arq.read_text(), arq.name)
            self.assertNotIn("getenv", arq.read_text(), arq.name)

    def test_servicos_de_ia_nao_recebem_settings(self):
        # a IA só recebe TenantContext (credenciais DA ORG); funções de LLM/agente não têm parâmetro `settings`
        for arq in ("app/services/agent.py", "app/services/llm/client.py", "app/services/llm/prompt.py"):
            self.assertNotIn("settings", (RAIZ / arq).read_text().lower().replace("# settings", ""), arq)

    def test_contexto_do_tenant_nao_imprime_credenciais(self):
        t = TenantContext(org_id=ORG_A, credenciais={"llm": {"api_key": "sk-secreta-123456"}})
        self.assertNotIn("sk-secreta", repr(t))

    def test_env_example_lista_as_cinco_variaveis_globais(self):
        txt = (RAIZ / ".env.example").read_text()
        for v in SEGREDOS + ("SUPABASE_URL", "PUBLIC_BASE_URL"):
            self.assertRegex(txt, rf"(?m)^{v}=")


if __name__ == "__main__":
    unittest.main()
