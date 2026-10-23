"""Validação de itens de pedido (pura). Valores em CENTAVOS inteiros, nunca float."""
from typing import Any


class PedidoInvalido(ValueError):
    pass


def validar_itens(itens: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(itens, list) or not 1 <= len(itens) <= 30:
        raise PedidoInvalido("Informe de 1 a 30 itens.")
    limpos, total = [], 0
    for i in itens:
        if not isinstance(i, dict):
            raise PedidoInvalido("Item inválido.")
        desc, qtd, preco = i.get("descricao"), i.get("quantidade"), i.get("preco_centavos")
        if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 120:
            raise PedidoInvalido("Descrição do item inválida.")
        if not isinstance(qtd, int) or isinstance(qtd, bool) or not 1 <= qtd <= 999:
            raise PedidoInvalido("Quantidade inválida (1 a 999).")
        if not isinstance(preco, int) or isinstance(preco, bool) or not 1 <= preco <= 10_000_000:
            raise PedidoInvalido("Preço deve ser inteiro em centavos (1 a 10.000.000).")
        limpos.append({"descricao": desc.strip(), "quantidade": qtd, "preco_centavos": preco})
        total += qtd * preco
    if total > 100_000_000:
        raise PedidoInvalido("Total acima do limite.")
    return limpos, total
