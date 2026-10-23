"""Configuração central (variáveis de ambiente). Falha na subida se faltar algo crítico."""
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_service_role_key: SecretStr
    # Segredo que o BACKEND do CRM usa para assinar os tokens (ver app/core/auth.py).
    crm_shared_secret: SecretStr = Field(min_length=32)
    crm_shared_secret_previous: SecretStr | None = None
    credentials_master_key: SecretStr
    credentials_master_key_previous: SecretStr | None = None
    # Legado/operacional: se definido, habilita /v1/internal/* (ex.: Render Cron Job). Não dá acesso a dados de org.
    internal_api_token: SecretStr | None = None
    # URL pública deste serviço (usada para montar o webhook de pagamento).
    public_base_url: str = ""

    environment: str = "production"
    log_level: str = "INFO"
    run_scheduler: bool = True
    max_concurrent_tenants: int = Field(default=20, ge=1, le=500)
    http_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    token_ttl_max_seconds: int = Field(default=120, ge=10, le=600)
    chat_requests_per_minute: int = Field(default=20, ge=1, le=600)

    def segredos_crm(self) -> list[str]:
        out = [self.crm_shared_secret.get_secret_value()]
        if self.crm_shared_secret_previous:
            out.append(self.crm_shared_secret_previous.get_secret_value())
        return out

    def chaves_mestras(self) -> list[str]:
        out = [self.credentials_master_key.get_secret_value()]
        if self.credentials_master_key_previous:
            out.append(self.credentials_master_key_previous.get_secret_value())
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
