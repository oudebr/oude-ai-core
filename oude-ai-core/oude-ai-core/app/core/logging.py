"""Log estruturado simples. Regra: NUNCA logar chaves, tokens ou telefones completos."""
import logging
import sys


def configurar_logs(nivel: str = "INFO") -> None:
    logging.basicConfig(
        level=nivel.upper(),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Bibliotecas HTTP imprimem URLs completas em DEBUG; mantém em WARNING para não vazar nada.
    for nome in ("httpx", "httpcore", "hpack"):
        logging.getLogger(nome).setLevel(logging.WARNING)


def mascarar_telefone(fone: str) -> str:
    d = "".join(c for c in fone if c.isdigit())
    return f"***{d[-4:]}" if len(d) >= 4 else "***"
