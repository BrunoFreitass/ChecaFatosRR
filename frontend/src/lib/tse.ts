/** Apuração oficial do TSE (etapa 5): formato de GET /tse/resultado e /tse/status. */

export type TseScope = "br" | "rr";

export interface TseCandidate {
  numero: string;
  nome: string;
  partido: string | null;
  vice: string | null;
  votos: number;
  pct: number | null;
  /** Situação escrita pelo TSE ("Eleito", "Matematicamente eleito"...). */
  situacao: string;
  /** Só verdadeiro quando o TSE marca o candidato como eleito. */
  eleito: boolean;
}

export interface TseResult {
  escopo: string;
  eleicao: string;
  turno: number | null;
  cargo: string;
  final: boolean;
  /** Horário da totalização no TSE (ISO, com fuso). */
  totalizado_em: string | null;
  secoes_pct: number | null;
  eleitores: number | null;
  validos: number | null;
  brancos: number | null;
  brancos_pct: number | null;
  nulos: number | null;
  nulos_pct: number | null;
  abstencao: number | null;
  abstencao_pct: number | null;
  candidatos: TseCandidate[];
}

export interface TseResponse {
  fonte: string;
  link_oficial: string;
  /** resultado null = o TSE ainda não divulgou números desta eleição. */
  escopos: { escopo: string; resultado: TseResult | null }[];
}

export interface TseStatus {
  banner: boolean;
  texto: string;
  apuracao: boolean;
}

export const SCOPE_LABELS: Record<TseScope, string> = { br: "Brasil", rr: "Roraima" };
export const OFFICIAL_SITE = "https://resultados.tse.jus.br/";
/** Consulta do navegador (aprovado em 11/10/2026): a cada 60 s, só com a aba visível. */
export const POLL_MS = 60_000;

const NUMBER = new Intl.NumberFormat("pt-BR");
const PERCENT = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** 56104503 -> "56.104.503". */
export function formatVotes(value: number | null): string {
  return value === null ? "—" : NUMBER.format(value);
}

/** 45.3 -> "45,30%". */
export function formatPct(value: number | null): string {
  return value === null ? "—" : `${PERCENT.format(value)}%`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCandidate(value: unknown): value is TseCandidate {
  return (
    isRecord(value) &&
    typeof value.numero === "string" &&
    typeof value.nome === "string" &&
    typeof value.votos === "number" &&
    typeof value.eleito === "boolean"
  );
}

function isResult(value: unknown): value is TseResult {
  return isRecord(value) && Array.isArray(value.candidatos) && value.candidatos.every(isCandidate);
}

/** Resposta 200 com outra forma (proxy, página de erro) não pode derrubar a página. */
export function isTseResponse(value: unknown): value is TseResponse {
  return (
    isRecord(value) &&
    Array.isArray(value.escopos) &&
    value.escopos.every(
      (item) =>
        isRecord(item) &&
        typeof item.escopo === "string" &&
        (item.resultado === null || isResult(item.resultado)),
    )
  );
}
