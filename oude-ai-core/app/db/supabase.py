"""Cliente Supabase assíncrono (service role). Um único cliente por processo, criado no startup."""
from supabase import AsyncClient, acreate_client

from app.config import Settings


async def criar_cliente(settings: Settings) -> AsyncClient:
    return await acreate_client(settings.supabase_url, settings.supabase_service_role_key.get_secret_value())
