"""Limite de uso por organização (janela deslizante, em memória). Protege o custo de IA do lojista e
o serviço contra abuso. Por processo: com N réplicas o teto efetivo é N vezes maior."""
import time
from collections import defaultdict, deque


class LimitadorPorOrg:
    def __init__(self, maximo: int, janela_s: float = 60.0) -> None:
        self.maximo, self.janela = maximo, janela_s
        self._eventos: dict[str, deque[float]] = defaultdict(deque)

    def permitir(self, chave: str, agora: float | None = None) -> bool:
        agora = time.monotonic() if agora is None else agora
        fila = self._eventos[chave]
        while fila and fila[0] <= agora - self.janela:
            fila.popleft()
        if len(fila) >= self.maximo:
            return False
        fila.append(agora)
        return True
