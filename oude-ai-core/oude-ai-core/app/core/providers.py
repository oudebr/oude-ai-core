"""Catálogo único de integrações que o LOJISTA pode conectar (cada uma salva cifrada em org_integrations)."""

IA = ("llm",)
WHATSAPP = ("whatsapp",)          # dentro dele: provider = meta_cloud | evolution | zapi
FORNECEDOR = ("supplier",)
PAGAMENTOS = ("infinitepay", "mercadopago", "pix")   # um registro por meio de pagamento
INTEGRACOES = IA + WHATSAPP + FORNECEDOR + PAGAMENTOS
