"""TODO (próxima etapa): aniversários/datas comemorativas, nutrição de leads não convertidos e pós-venda.
Usará `services.llm.client.responder` para redigir e `reservar_execucao` para não repetir mensagem."""
import logging

log = logging.getLogger(__name__)


async def engajamento(*_args, **_kwargs) -> int:
    log.info("job engajamento: ainda não implementado")
    return 0
