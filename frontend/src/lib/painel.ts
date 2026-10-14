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

const DAY = /^\d{4}-\d{2}-\d{2}$/;

function isDay(value: unknown): value is string {
  return typeof value === "string" && DAY.test(value);
}

function isDaily(value: unknown, keys: readonly string[]): value is { dia: string }[] {
  return Array.isArray(value) && value.every((row) => isRecord(row) && isDay(row.dia) && hasNumbers(row, keys));
}

/** Todas as listas por dia cobrem os mesmos dias (a tela cruza as tabelas pelo dia). */
function sameDays(lists: { dia: string }[][]): boolean {
  const [first, ...rest] = lists.map((rows) => rows.map((r) => r.dia).sort().join(","));
  return rest.every((days) => days === first);
}

const CHECAGENS = ["total", "verdadeiro", "enganoso", "falso", "fora_de_contexto", "nao_verificavel", "so_agencias"];
const USE = ["usadas", "limite", "restante"];

/** Valida tudo o que a tela lê: resposta fora do formato vira mensagem de erro, não tela quebrada. */
export function isPainelResponse(value: unknown): value is PainelResponse {
  if (!isRecord(value) || typeof value.gerado_em !== "string") return false;
  const { fila, usuarios, gemini, checagens, tokens, pedidos, comentarios, denuncias, usuarios_novos } = value;
  return (
    isDaily(checagens, CHECAGENS) &&
    isDaily(tokens, ["checagens", "entrada", "saida", "raciocinio"]) &&
    isDaily(pedidos, ["total", "do_cache"]) &&
    isDaily(comentarios, ["total", "ocultos"]) &&
    isDaily(denuncias, ["total"]) &&
    isDaily(usuarios_novos, ["total"]) &&
    sameDays([checagens, tokens, pedidos, comentarios, denuncias, usuarios_novos]) &&
    isRecord(fila) &&
    hasNumbers(fila, ["pendentes", "revisao", "confirmacao_strike"]) &&
    isRecord(usuarios) &&
    hasNumbers(usuarios, ["total", "suspensos", "banidos"]) &&
    isRecord(gemini) &&
    isDay(gemini.dia_pacifico) &&
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
