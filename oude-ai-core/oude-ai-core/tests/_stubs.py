"""Permite rodar a suíte SEM instalar httpx/supabase (ex.: ambiente sem acesso ao PyPI).
Com as dependências reais instaladas, estes stubs não são usados."""
import sys
import types


def instalar() -> None:
    try:
        import httpx  # noqa: F401
    except ImportError:
        m = types.ModuleType("httpx")

        class HTTPError(Exception):
            pass

        class AsyncClient:  # só para anotações de tipo
            pass

        m.HTTPError, m.AsyncClient = HTTPError, AsyncClient
        sys.modules["httpx"] = m
    try:
        import supabase  # noqa: F401
    except ImportError:
        m = types.ModuleType("supabase")
        m.AsyncClient = type("AsyncClient", (), {})
        sys.modules["supabase"] = m


instalar()
