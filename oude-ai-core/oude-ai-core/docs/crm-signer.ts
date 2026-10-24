// Backend do OÚDE CRM (TanStack Start / Node): gera o token que autoriza UMA chamada ao oude-ai-core.
// Antes de chamar: confirme que o usuário logado pertence à organização (org_membros). O papel NÃO vai no token;
// o oude-ai-core consulta o papel real no banco.
import { createHmac, randomUUID } from "node:crypto";

const b64 = (v: string | Buffer) => Buffer.from(v).toString("base64url");

export function tokenParaAgente(userId: string, orgId: string, segredo: string): string {
  const iat = Math.floor(Date.now() / 1000);
  const cab = b64(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const corpo = b64(
    JSON.stringify({ iss: "oude-crm", aud: "oude-ai-core", sub: userId, org: orgId, jti: randomUUID(), iat, exp: iat + 60 }),
  );
  const sig = createHmac("sha256", segredo).update(`${cab}.${corpo}`).digest("base64url");
  return `${cab}.${corpo}.${sig}`;
}

// Uso: fetch(`${AI_CORE_URL}/v1/tenants/${orgId}/agente`, { headers: { Authorization: `Bearer ${tokenParaAgente(user.id, orgId, process.env.CRM_SHARED_SECRET!)}` } })
