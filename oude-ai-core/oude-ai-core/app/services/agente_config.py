"""
Configuração da tela "Meu Agente de IA": identidade, doutrinação e autonomia.

Tudo que o lojista digita é tratado como DADO: validado (tipo e tamanho), saneado (sem caracteres de controle
nem delimitadores do prompt) e só então gravado. Campos desconhecidos são descartados (lista branca).
A autonomia é aplicada AQUI, no servidor (`exigir` / `ferramentas_permitidas`), nunca pelo navegador.
"""
from typing import Any

from app.services.llm.prompt import TONS, sanear_instrucoes

SEGMENTOS = ("perfumaria", "clinica", "ecommerce", "servicos", "outro")
EMOJIS = ("permitidos", "moderados", "proibidos")
IDIOMAS = ("pt-BR", "en", "es")
MAX_FAQ = 30


class ConfigAgenteInvalida(ValueError):
    pass


# campo -> tamanho máximo (iguais aos CHECKs do SQL 004)
LIMITES_IDENTIDADE = {"nome_loja": 120, "saudacao": 300, "estilo_comunicacao": 600, "objetivos": 800, "transbordo": 800}
LIMITES_DOUTRINA = {
    "historia": 2000, "publico_alvo": 1000, "produtos_servicos": 2000, "pagamentos": 1000, "entrega": 1000,
    "trocas": 1000, "horarios": 600, "promocoes": 1000, "regras_comerciais": 1500,
}
INTERRUPTORES = (
    "responder_duvidas", "recomendar_produtos", "consultar_estoque", "registrar_oportunidades", "criar_pedidos",
    "enviar_whatsapp", "aplicar_descontos", "criar_cobrancas", "encaminhar_funcionario",
)
PERMISSOES_PADRAO: dict[str, Any] = {
    "responder_duvidas": True, "recomendar_produtos": True, "consultar_estoque": True,
    "registrar_oportunidades": False, "criar_pedidos": False, "enviar_whatsapp": False,
    "aplicar_descontos": False, "desconto_max_pct": 0, "criar_cobrancas": False, "encaminhar_funcionario": True,
}
IDENTIDADE_PADRAO: dict[str, Any] = {
    "nome_loja": "", "segmento": "perfumaria", "saudacao": "", "estilo_comunicacao": "", "emojis": "moderados",
    "idioma": "pt-BR", "objetivos": "", "transbordo": "",
}
DOUTRINA_PADRAO: dict[str, Any] = {**{k: "" for k in LIMITES_DOUTRINA}, "faq": []}


def _texto(valor: Any, campo: str, maximo: int) -> str:
    if not isinstance(valor, str):
        raise ConfigAgenteInvalida(f"'{campo}' deve ser texto.")
    limpo = sanear_instrucoes(valor)[:maximo]  # remove controle e delimitadores do prompt
    return limpo


def validar_identidade(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for campo, maximo in LIMITES_IDENTIDADE.items():
        if campo in d:
            out[campo] = _texto(d[campo], campo, maximo)
    for campo, validos in (("segmento", SEGMENTOS), ("emojis", EMOJIS), ("idioma", IDIOMAS)):
        if campo in d:
            if d[campo] not in validos:
                raise ConfigAgenteInvalida(f"'{campo}' deve ser um de {list(validos)}.")
            out[campo] = d[campo]
    return out


def validar_doutrina(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for campo, maximo in LIMITES_DOUTRINA.items():
        if campo in d:
            out[campo] = _texto(d[campo], campo, maximo)
    if "faq" in d:
        faq = d["faq"]
        if not isinstance(faq, list) or len(faq) > MAX_FAQ:
            raise ConfigAgenteInvalida(f"'faq' deve ser uma lista com até {MAX_FAQ} itens.")
        itens = []
        for i in faq:
            if not isinstance(i, dict):
                raise ConfigAgenteInvalida("Cada item do FAQ deve ter 'pergunta' e 'resposta'.")
            p, r = _texto(i.get("pergunta", ""), "pergunta", 200), _texto(i.get("resposta", ""), "resposta", 600)
            if p and r:  # item pela metade é descartado
                itens.append({"pergunta": p, "resposta": r})
        out["faq"] = itens
    return out


def validar_autonomia(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for campo in INTERRUPTORES:
        if campo in d:
            if not isinstance(d[campo], bool):
                raise ConfigAgenteInvalida(f"'{campo}' deve ser verdadeiro ou falso.")
            out[campo] = d[campo]
    if "desconto_max_pct" in d:
        v = d["desconto_max_pct"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 50:
            raise ConfigAgenteInvalida("'desconto_max_pct' deve ser um número de 0 a 50.")
        out["desconto_max_pct"] = round(float(v), 2)
    # coerência: sem permissão para descontos, o limite é zero (não fica um número "armado" escondido)
    if out.get("aplicar_descontos") is False:
        out["desconto_max_pct"] = 0
    return out


def validar_agente(d: dict[str, Any]) -> dict[str, Any]:
    """nome_agente, tom e instruções livres (ficam em ai_agent_config)."""
    out: dict[str, Any] = {}
    if "nome_agente" in d:
        n = d["nome_agente"]
        if not isinstance(n, str) or not 1 <= len(n.strip()) <= 60:
            raise ConfigAgenteInvalida("'nome_agente' deve ter de 1 a 60 caracteres.")
        out["nome_agente"] = n.strip()
    if "tom" in d:
        if d["tom"] not in TONS:
            raise ConfigAgenteInvalida(f"'tom' deve ser um de {list(TONS)}.")
        out["tom"] = d["tom"]
    if "instrucoes" in d:
        if not isinstance(d["instrucoes"], str):
            raise ConfigAgenteInvalida("'instrucoes' deve ser texto.")
        out["instrucoes"] = sanear_instrucoes(d["instrucoes"])
    return out


def com_padroes(linha: dict[str, Any] | None, padrao: dict[str, Any]) -> dict[str, Any]:
    """Completa uma linha do banco (ou None) com os padrões, devolvendo só as chaves conhecidas."""
    linha = linha or {}
    return {k: linha.get(k, v) for k, v in padrao.items()}


def exigir(perm: dict[str, Any], interruptor: str) -> None:
    """Barreira server-side. Chame ANTES de executar qualquer ferramenta que altere algo ou envie algo."""
    if interruptor not in INTERRUPTORES:
        raise PermissaoNegada("Permissão desconhecida.")  # falha fechada
    if not perm.get(interruptor, False):
        raise PermissaoNegada(f"O lojista não autorizou o agente a: {interruptor}.")


class PermissaoNegada(Exception):
    pass


# ferramenta -> interruptor que a autoriza
FERRAMENTA_PERMISSAO = {"consultar_catalogo": "consultar_estoque", "encaminhar_funcionario": "encaminhar_funcionario"}


def ferramentas_permitidas(perm: dict[str, Any]) -> set[str]:
    return {f for f, i in FERRAMENTA_PERMISSAO.items() if perm.get(i, False)}


# ---------------------------------------------------------------- status do agente
def calcular_status(cfg: dict[str, Any] | None, tem_llm: bool, identidade: dict[str, Any], integracoes: list[dict[str, Any]]) -> dict[str, Any]:
    """Estado REAL, calculado no servidor. Códigos: incompleto | pronto | ativo | pausado | erro_integracao."""
    cfg = cfg or {}
    falhas = [i["provider"] for i in integracoes if i.get("ultimo_teste_ok") is False]
    pendencias = []
    if not tem_llm:
        pendencias.append("Conecte a chave de IA (aba Integrações).")
    if not (identidade.get("nome_loja") or "").strip():
        pendencias.append("Informe o nome da loja (aba Personalidade).")
    if cfg.get("ativo"):
        # só as integrações essenciais derrubam o agente: IA sempre; as demais só avisam
        if "llm" in falhas:
            return {"codigo": "erro_integracao", "rotulo": "Erro de integração", "pendencias": [
                "O último teste da chave de IA falhou. Corrija e teste de novo."], "falhas": falhas}
        return {"codigo": "ativo", "rotulo": "Ativo", "pendencias": [], "falhas": falhas}
    if pendencias:
        return {"codigo": "incompleto", "rotulo": "Configuração incompleta", "pendencias": pendencias, "falhas": falhas}
    if cfg.get("pausado_em"):
        return {"codigo": "pausado", "rotulo": "Pausado", "pendencias": [], "falhas": falhas}
    return {"codigo": "pronto", "rotulo": "Pronto para testar", "pendencias": [], "falhas": falhas}


def bloqueios_de_ativacao(tem_llm: bool, integracoes: list[dict[str, Any]]) -> list[str]:
    """Motivos que IMPEDEM ativar. Vazio = pode ativar."""
    b = []
    if not tem_llm:
        b.append("Conecte a chave de IA antes de ativar o agente.")
    if any(i["provider"] == "llm" and i.get("ultimo_teste_ok") is False for i in integracoes):
        b.append("O último teste da chave de IA falhou. Teste a conexão novamente antes de ativar.")
    return b
