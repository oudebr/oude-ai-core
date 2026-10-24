from fastapi import Request

from app.config import Settings
from app.db.repositories import TenantRepository


def get_repo(request: Request) -> TenantRepository:
    return request.app.state.repo


def get_http(request: Request):
    return request.app.state.http


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings
