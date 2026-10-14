// Painel do admin (plano mapa-e-painel-admin §B): GET /admin/painel, só números agregados.
import { apiFetch } from "./api.ts";

export type { ChecagensDia, PainelResponse, TokensDia } from "./painel.types.ts";
import type { PainelResponse } from "./painel.types.ts";

/** Painel de visitas do Cloudflare (o painel só leva até lá; os dados não passam pela API). */
export const WEB_ANALYTICS_URL = "https://dash.cloudflare.com/?to=/:account/web-analytics";

export function fetchPainel(signal?: AbortSignal) {
  return apiFetch<unknown>("/admin/painel", { signal });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasNumbers(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.every((k) => typeof value[k] === "number" && Number.isFinite(value[k]));
}

function isDaily(value: unknown, keys: readonly string[]): boolean {
  return (
    Array.isArray(value) &&
    value.every((row) => isRecord(row) && typeof row.dia === "string" && hasNumbers(row, keys))
  );
}

const CHECAGENS = ["total", "verdadeiro", "enganoso", "falso", "fora_de_contexto", "nao_verificavel", "so_agencias"];
const USE = ["usadas", "limite", "restante"];

/** Valida tudo o que a tela lê: resposta fora do formato vira mensagem de erro, não tela quebrada. */
export function isPainelResponse(value: unknown): value is PainelResponse {
  if (!isRecord(value) || typeof value.gerado_em !== "string") return false;
  const { fila, usuarios, gemini } = value;
  return (
    isDaily(value.checagens, CHECAGENS) &&
    isDaily(value.tokens, ["checagens", "entrada", "saida", "raciocinio"]) &&
    isDaily(value.pedidos, ["total", "do_cache"]) &&
    isDaily(value.comentarios, ["total", "ocultos"]) &&
    isDaily(value.denuncias, ["total"]) &&
    isDaily(value.usuarios_novos, ["total"]) &&
    isRecord(fila) &&
    hasNumbers(fila, ["pendentes", "revisao", "confirmacao_strike"]) &&
    isRecord(usuarios) &&
    hasNumbers(usuarios, ["total", "suspensos", "banidos"]) &&
    isRecord(gemini) &&
    typeof gemini.dia_pacifico === "string" &&
    Array.isArray(gemini.cota) &&
    gemini.cota.every((q) => isRecord(q) && typeof q.uso === "string" && hasNumbers(q, USE)) &&
    Array.isArray(gemini.modelos) &&
    gemini.modelos.every(
      (m) =>
        isRecord(m) &&
        typeof m.modelo === "string" &&
        typeof m.checagem === "boolean" &&
        typeof m.moderacao === "boolean" &&
        hasNumbers(m, USE),
    )
  );
}
