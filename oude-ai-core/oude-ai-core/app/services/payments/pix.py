"""
Pix ESTÁTICO (BR Code / "copia e cola"), com a chave Pix do próprio lojista. Não há provedor que avise o
pagamento: a confirmação é MANUAL (dono/admin confirma no CRM depois de ver o Pix na conta). Por isso
`usa_webhook = False` e não existe `verificar`. Formato EMV do BR Code conforme o manual do Banco Central
(campos 00, 01, 26[00,01], 52, 53, 54, 58, 59, 60, 62[05], 63 = CRC16-CCITT). ATENÇÃO: gerado e checado
internamente (estrutura + CRC); ainda NÃO foi validado pagando em app de banco: teste com R$ 0,01 antes de usar.
"""
import re
import unicodedata

from app.services.payments.base import Cobranca, EventoPagamento, Verificacao


def _campo(id_: str, valor: str) -> str:
    return f"{id_}{len(valor):02d}{valor}"


def crc16_ccitt(texto: str) -> str:
    crc = 0xFFFF
    for b in texto.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def _ascii_maiusculo(txt: str, maximo: int) -> str:
    sem = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]", "", sem).upper().strip()[:maximo]


def gerar_brcode(chave: str, nome: str, cidade: str, valor_centavos: int, txid: str) -> str:
    if not 1 <= valor_centavos <= 99_999_999_99:
        raise ValueError("valor fora do intervalo")
    conta = _campo("00", "br.gov.bcb.pix") + _campo("01", chave)
    tx = re.sub(r"[^A-Za-z0-9]", "", txid)[:25] or "***"
    corpo = (
        _campo("00", "01") + _campo("01", "11") + _campo("26", conta) + _campo("52", "0000") + _campo("53", "986")
        + _campo("54", f"{valor_centavos // 100}.{valor_centavos % 100:02d}") + _campo("58", "BR")
        + _campo("59", _ascii_maiusculo(nome, 25) or "LOJA") + _campo("60", _ascii_maiusculo(cidade, 15) or "BRASIL")
        + _campo("62", _campo("05", tx)) + "6304"
    )
    return corpo + crc16_ccitt(corpo)


class PixManual:
    nome = "pix"
    usa_webhook = False

    async def criar_cobranca(self, http, cred, pedido_id, itens, total_centavos, webhook_url) -> Cobranca:
        codigo = gerar_brcode(str(cred["chave_pix"]), str(cred["nome_recebedor"]), str(cred["cidade"]),
                              total_centavos, pedido_id.replace("-", ""))
        return Cobranca(copia_e_cola=codigo, bruto={"tipo": "pix_estatico"},
                        aviso="Pix estático: confirme o pagamento manualmente (POST /pedidos/{id}/confirmar-manual).")

    def extrair_evento(self, payload) -> EventoPagamento | None:
        return None

    async def verificar(self, http, cred, evento) -> Verificacao:  # pragma: no cover - nunca chamado
        raise NotImplementedError("Pix estático não tem confirmação automática.")
