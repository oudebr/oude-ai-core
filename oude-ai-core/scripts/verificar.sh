#!/usr/bin/env bash
# Procedimento reproduzível: instala, testa e (se as dependências estiverem instaladas) sobe a API e testa os endpoints.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1) Dependências"
python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt && DEPS=1 || { echo "!! pip install falhou (sem acesso ao PyPI?). Rodando só a suíte sem rede."; DEPS=0; }

echo "== 2) Testes unitários (stdlib; usam stubs se httpx/supabase não existirem)"
if python -c "import pytest" 2>/dev/null; then python -m pytest -q 2>&1 | tail -8
else python -m unittest discover -s tests -t . -p "test_[!c]*.py" 2>&1 | tail -5; echo "(sem pytest: test_core.py foi pulado)"; fi

if [ "$DEPS" = "1" ]; then
  echo "== 3) Testes de rotas (exigem fastapi)"
  python -m unittest tests.test_api -v 2>&1 | tail -5

  echo "== 4) Subindo a API com ambiente de teste e consultando /health"
  export SUPABASE_URL=https://exemplo.supabase.co SUPABASE_SERVICE_ROLE_KEY=chave-de-teste
  export CRM_SHARED_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(40))")
  export CREDENTIALS_MASTER_KEY=$(python -m app.core.crypto gerar-chave)
  export RUN_SCHEDULER=false PUBLIC_BASE_URL=https://exemplo.onrender.com
  uvicorn main:app --port 8765 & PID=$!
  trap 'kill $PID 2>/dev/null || true' EXIT
  for i in $(seq 1 20); do curl -fs localhost:8765/health && break || sleep 0.5; done; echo
  echo "sem token => esperado 401:"; curl -s -o /dev/null -w "%{http_code}\n" localhost:8765/v1/tenants/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/agente
fi