"""
Autenticação das chamadas do BACKEND do OÚDE CRM para este serviço (token assinado, HS256, stdlib).

O backend do CRM autentica o lojista (sessão Supabase), confere que ele pertence à organização e então
assina um token CURTO com: sub (id do usuário), org (id da organização), jti, iat, exp.
Este serviço NÃO confia no papel enviado: o papel (dono/admin/membro) é lido de `org_membros` no banco.

Segredos: CRM_SHARED_SECRET (atual) e CRM_SHARED_SECRET_PREVIOUS (aceito só para verificar, p/ rotação).
"""
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from uuid import UUID

ISSUER = "oude-crm"
AUDIENCE = "oude-ai-core"
LEEWAY = 30  # segundos de tolerância de relógio

# escopo -> papéis permitidos (papel real, vindo do banco)
PERMISSOES: dict[str, frozenset[str]] = {
    "chat": frozenset({"dono", "admin", "membro"}),
    "agente.ler": frozenset({"dono", "admin", "membro"}),
    "agente.escrever": frozenset({"dono", "admin"}),
    "integracoes.ler": frozenset({"dono", "admin"}),
    "integracoes.escrever": frozenset({"dono", "admin"}),
    "integracoes.testar": frozenset({"dono", "admin"}),
    "jobs.rodar": frozenset({"dono", "admin"}),
    "pedidos.ler": frozenset({"dono", "admin", "membro"}),
    "pedidos.escrever": frozenset({"dono", "admin"}),
}


class NaoAutenticado(Exception):
    """401: token ausente/inválido/expirado."""


class SemPermissao(Exception):
    """403: autenticado, mas sem acesso a esta organização/operação."""


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    org_id: UUID
    papel: str


def _b64(dado: bytes) -> str:
    return base64.urlsafe_b64encode(dado).rstrip(b"=").decode()


def _unb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def assinar_token(claims: dict, segredo: str) -> str:
    """Usado nos testes e como referência para o backend do CRM (mesmo formato)."""
    cab = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    corpo = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(segredo.encode(), f"{cab}.{corpo}".encode(), hashlib.sha256).digest()
    return f"{cab}.{corpo}.{_b64(sig)}"


def verificar_token(token: str, segredos: list[str], ttl_max: int = 120, agora: float | None = None) -> dict:
    """Valida assinatura (segredo atual ou anterior), alg, issuer, audience, validade e TTL máximo."""
    agora = time.time() if agora is None else agora
    try:
        cab_b64, corpo_b64, sig_b64 = token.split(".")
        cab = json.loads(_unb64(cab_b64))
        claims = json.loads(_unb64(corpo_b64))
        sig = _unb64(sig_b64)
    except (ValueError, TypeError) as exc:
        raise NaoAutenticado("Token malformado.") from exc
    if cab.get("alg") != "HS256":  # recusa "none" e qualquer outro algoritmo
        raise NaoAutenticado("Algoritmo não aceito.")
    assinado = f"{cab_b64}.{corpo_b64}".encode()
    if not any(
        hmac.compare_digest(sig, hmac.new(s.encode(), assinado, hashlib.sha256).digest()) for s in segredos if s
    ):
        raise NaoAutenticado("Assinatura inválida.")
    if not isinstance(claims, dict) or claims.get("iss") != ISSUER or claims.get("aud") != AUDIENCE:
        raise NaoAutenticado("Emissor/destinatário inválido.")
    try:
        iat, exp = float(claims["iat"]), float(claims["exp"])
        UUID(str(claims["sub"])), UUID(str(claims["org"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise NaoAutenticado("Claims incompletas.") from exc
    if exp < agora - LEEWAY or iat > agora + LEEWAY:
        raise NaoAutenticado("Token expirado.")
    if exp - iat > ttl_max:
        raise NaoAutenticado("Validade do token acima do permitido.")
    if not claims.get("jti"):
        raise NaoAutenticado("Token sem identificador.")
    return claims


class CacheJti:
    """Anti-replay em memória (por processo). Em várias réplicas não é global: por isso as operações
    de escrita também são idempotentes por desenho (ver pedidos/pagamentos)."""

    def __init__(self) -> None:
        self._vistos: dict[str, float] = {}

    def usar(self, jti: str, expira_em: float, agora: float | None = None) -> bool:
        agora = time.time() if agora is None else agora
        for k in [k for k, v in self._vistos.items() if v < agora]:
            del self._vistos[k]
        if jti in self._vistos:
            return False
        self._vistos[jti] = expira_em + LEEWAY
        return True


async def autorizar_requisicao(
    repo,
    token: str | None,
    org_id_url: UUID,
    escopo: str,
    metodo: str,
    segredos: list[str],
    cache_jti: CacheJti,
    ttl_max: int = 120,
    agora: float | None = None,
) -> Principal:
    if not token:
        raise NaoAutenticado("Token ausente.")
    if escopo not in PERMISSOES:
        raise SemPermissao("Escopo desconhecido.")  # falha fechada
    claims = verificar_token(token, segredos, ttl_max, agora)
    if UUID(str(claims["org"])) != org_id_url:  # o token vale para UMA organização
        raise SemPermissao("Token não vale para esta organização.")
    if metodo.upper() not in ("GET", "HEAD") and not cache_jti.usar(str(claims["jti"]), float(claims["exp"]), agora):
        raise NaoAutenticado("Token já utilizado.")
    user_id = UUID(str(claims["sub"]))
    papel = await repo.papel_do_usuario(org_id_url, user_id)  # papel REAL, do banco
    if papel is None or papel not in PERMISSOES[escopo]:
        raise SemPermissao("Sem permissão para esta operação.")
    return Principal(user_id=user_id, org_id=org_id_url, papel=papel)
