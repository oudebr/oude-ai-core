"""TODO (próxima etapa): comparar catálogo do fornecedor com `inventory`, atualizar preços de custo
e sugerir pedidos de compra. O cliente seguro já existe em `services.supplier.client`."""
import logging

log = logging.getLogger(__name__)


async def sincronizar_fornecedor(*_args, **_kwargs) -> int:
    log.info("job fornecedor: ainda não implementado")
    return 0
