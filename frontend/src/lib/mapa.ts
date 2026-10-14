// Mapa do Brasil na Apuração (plano mapa-e-painel-admin): regras de cor, faixas e nomes.
// Cor por número de candidato vinda da API (TSE_MAPA_CORES; 13 e 22 lidos do arquivo oficial do
// TSE). Vermelho e azul com as mesmas 4 faixas e o mesmo contraste; sem cor = cinza neutro.

export interface MapaCandidato {
  numero: string;
  nome: string;
  pct: number | null;
}

export interface MapaResultado {
  candidatos: MapaCandidato[];
  secoes_pct: number | null;
  final: boolean;
  totalizado_em: string | null;
}

export interface MapaResponse {
  cores: Record<string, string>;
  estados: { uf: string; resultado: MapaResultado | null }[];
}

export const UF_NAMES: Record<string, string> = {
  ac: "Acre", al: "Alagoas", am: "Amazonas", ap: "Amapá", ba: "Bahia", ce: "Ceará",
  df: "Distrito Federal", es: "Espírito Santo", go: "Goiás", ma: "Maranhão", mg: "Minas Gerais",
  ms: "Mato Grosso do Sul", mt: "Mato Grosso", pa: "Pará", pb: "Paraíba", pe: "Pernambuco",
  pi: "Piauí", pr: "Paraná", rj: "Rio de Janeiro", rn: "Rio Grande do Norte", ro: "Rondônia",
  rr: "Roraima", rs: "Rio Grande do Sul", sc: "Santa Catarina", se: "Sergipe", sp: "São Paulo",
  to: "Tocantins",
};

/** Sem dados do TSE ainda. */
export const NO_DATA = "#e2e8f0";
/** Líder sem cor configurada (só no ensaio do 1º turno, com vários candidatos). */
export const NEUTRAL_OTHER = "#64748b";

// Tailwind red/blue 300, 400, 600, 800: mesmas faixas de luminosidade nos dois.
const TONES: Record<string, readonly string[]> = {
  vermelho: ["#fca5a5", "#f87171", "#dc2626", "#991b1b"],
  azul: ["#93c5fd", "#60a5fa", "#2563eb", "#1e40af"],
};

/** Faixas de margem (pontos percentuais entre o 1º e o 2º): até 5, 15, 30 e acima de 30. */
export const BAND_LABELS = ["até 5 pontos", "5 a 15", "15 a 30", "mais de 30"] as const;

export function marginBand(margin: number): number {
  if (margin <= 5) return 0;
  if (margin <= 15) return 1;
  if (margin <= 30) return 2;
  return 3;
}

/** Candidatos com percentual, do maior para o menor (não depende da ordem da API). */
export function rankedCandidates(result: MapaResultado | null): MapaCandidato[] {
  return [...(result?.candidatos ?? [])]
    .filter((c) => c.pct !== null)
    .sort((a, b) => (b.pct ?? 0) - (a.pct ?? 0));
}

export function leaderOf(result: MapaResultado | null): MapaCandidato | null {
  return rankedCandidates(result)[0] ?? null;
}

/** Cor configurada para o número; "constructor" e afins não contam (só chaves próprias). */
export function colorOf(numero: string, cores: Record<string, string>): string | undefined {
  return Object.hasOwn(cores, numero) ? cores[numero] : undefined;
}

function tonesOf(color: string | undefined): readonly string[] | undefined {
  return color !== undefined && Object.hasOwn(TONES, color) ? TONES[color] : undefined;
}

export function fillFor(result: MapaResultado | null, cores: Record<string, string>): string {
  const [first, second] = rankedCandidates(result);
  if (!first) return NO_DATA;
  const tones = tonesOf(colorOf(first.numero, cores));
  if (!tones) return NEUTRAL_OTHER;
  const margin = (first.pct ?? 0) - (second?.pct ?? 0);
  return tones[marginBand(margin)] ?? NEUTRAL_OTHER;
}

/** Tom da legenda (faixa intermediária) de uma cor configurada. */
export function toneOf(color: string): string {
  return tonesOf(color)?.[2] ?? NEUTRAL_OTHER;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPctOrNull(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function isCandidato(value: unknown): value is MapaCandidato {
  return (
    isRecord(value) && typeof value.numero === "string" && typeof value.nome === "string" && isPctOrNull(value.pct)
  );
}

function isResultado(value: unknown): value is MapaResultado | null {
  if (value === null) return true;
  return (
    isRecord(value) &&
    Array.isArray(value.candidatos) &&
    value.candidatos.every(isCandidato) &&
    isPctOrNull(value.secoes_pct) &&
    typeof value.final === "boolean" &&
    (value.totalizado_em === null || typeof value.totalizado_em === "string")
  );
}

/** Valida tudo o que a tela lê: resposta malformada não chega ao render. */
export function isMapaResponse(value: unknown): value is MapaResponse {
  return (
    isRecord(value) &&
    isRecord(value.cores) &&
    Object.values(value.cores).every((color) => typeof color === "string") &&
    Array.isArray(value.estados) &&
    value.estados.every((e) => isRecord(e) && typeof e.uf === "string" && isResultado(e.resultado))
  );
}
