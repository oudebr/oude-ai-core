import json
from types import SimpleNamespace
from uuid import UUID

from tests import _stubs  # noqa: F401
import httpx


class FakeRepo:
    """Repositório em memória com a MESMA regra de isolamento do real: tudo filtrado por org_id."""

    def __init__(self):
        self.membros = {}      # (org, user) -> papel
        self.configs = {}      # org -> dict
        self.produtos = []     # linhas da tabela `produtos` (user_id == org_id)
        self.pedidos = {}      # (org, id) -> dict
        self.eventos = set()   # (org, transaction_nsu)
        self.msgs = []         # (org, contato, role, content)
        self.auditoria = []
        self.execucoes = set()
        self.notificacoes = []
        self.integr = {}       # org -> [linhas]
        self.estoque = {}      # org -> itens baixos

    async def papel_do_usuario(self, org, user):
        return self.membros.get((org, user))

    async def get_config(self, org):
        return self.configs.get(org)

    async def salvar_config(self, org, campos, user):
        self.configs.setdefault(org, {}).update(campos)

    async def listar_orgs_ativas(self):
        for org, c in list(self.configs.items()):
            if c.get("ativo"):
                yield org

    async def integracoes(self, org):
        return self.integr.get(org, [])

    async def salvar_integracao(self, org, provider, armazenado):
        self.salvos = getattr(self, "salvos", {})
        self.salvos[provider] = armazenado
        linhas = [l for l in self.integr.get(org, []) if l["provider"] != provider]
        self.integr[org] = linhas + [{"provider": provider, "encrypted_credentials": armazenado}]

    async def remover_integracao(self, org, provider):
        self.integr[org] = [l for l in self.integr.get(org, []) if l["provider"] != provider]

    async def historico(self, org, contato, limite=12):
        return [{"role": r, "content": c} for o, ct, r, c in self.msgs if o == org and ct == contato][-limite:]

    async def adicionar_mensagem(self, org, contato, role, content):
        self.msgs.append((org, contato, role, content))

    async def auditar(self, org, user, acao, detalhe=""):
        self.auditoria.append((org, acao, detalhe))

    async def buscar_catalogo(self, org, termo, limite=8):
        t = termo.lower()
        return [i for i in self.produtos if i["user_id"] == str(org) and (t in i["nome"].lower() or not t)][:limite]

    async def criar_pedido(self, org, contato, itens, total, provedor):
        pid = f"00000000-0000-0000-0000-{len(self.pedidos) + 1:012d}"
        ped = {"id": pid, "org_id": str(org), "itens": itens, "total_centavos": total, "status": "aguardando_pagamento",
               "provedor": provedor}
        self.pedidos[(org, pid)] = ped
        return ped

    async def get_pedido(self, org, pid):
        return self.pedidos.get((org, pid))

    async def reservar_evento_pagamento(self, org, nsu, payload):
        if (org, nsu) in self.eventos:
            return False
        self.eventos.add((org, nsu))
        return True

    async def liberar_evento_pagamento(self, org, nsu):
        self.eventos.discard((org, nsu))

    async def marcar_pedido_pago(self, org, pid, nsu, pago):
        ped = self.pedidos.get((org, pid))
        if not ped or ped["status"] != "aguardando_pagamento":
            return False
        ped.update(status="pago", transaction_nsu=nsu, paid_amount_centavos=pago)
        return True

    async def reservar_execucao(self, org, chave):
        if (org, chave) in self.execucoes:
            return False
        self.execucoes.add((org, chave))
        return True

    async def liberar_execucao(self, org, chave):
        self.execucoes.discard((org, chave))

    async def criar_notificacao(self, org, tipo, titulo, mensagem):
        self.notificacoes.append((org, tipo, mensagem))

    async def estoque_baixo(self, org):
        return self.estoque.get(org, [])


class FakeResp:
    def __init__(self, dados, status=200):
        self._d, self.status_code = dados, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


class FakeHttp:
    """Respostas roteirizadas; guarda cada chamada para os testes inspecionarem o que foi ENVIADO."""

    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []

    async def post(self, url, headers=None, json=None):
        self.chamadas.append((url, headers, json))
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def completion(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return FakeResp({"choices": [{"message": msg}]})


def tool_call(nome, args, cid="call_1"):
    return {"id": cid, "type": "function", "function": {"name": nome, "arguments": json.dumps(args)}}


def settings(chaves):
    return SimpleNamespace(chaves_mestras=lambda: chaves, max_concurrent_tenants=5)


ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DONO_A = UUID("a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1")
MEMBRO_A = UUID("a2a2a2a2-a2a2-a2a2-a2a2-a2a2a2a2a2a2")
DONO_B = UUID("b1b1b1b1-b1b1-b1b1-b1b1-b1b1b1b1b1b1")
