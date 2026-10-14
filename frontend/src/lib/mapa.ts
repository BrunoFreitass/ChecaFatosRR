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

function ranked(result: MapaResultado | null): MapaCandidato[] {
  return [...(result?.candidatos ?? [])]
    .filter((c) => c.pct !== null)
    .sort((a, b) => (b.pct ?? 0) - (a.pct ?? 0));
}

export function leaderOf(result: MapaResultado | null): MapaCandidato | null {
  return ranked(result)[0] ?? null;
}

export function fillFor(result: MapaResultado | null, cores: Record<string, string>): string {
  const [first, second] = ranked(result);
  if (!first) return NO_DATA;
  const tones = TONES[cores[first.numero] ?? ""];
  if (!tones) return NEUTRAL_OTHER;
  const margin = (first.pct ?? 0) - (second?.pct ?? 0);
  return tones[marginBand(margin)] ?? NEUTRAL_OTHER;
}

/** Tom da legenda (faixa intermediária) de uma cor configurada. */
export function toneOf(color: string): string {
  return TONES[color]?.[2] ?? NEUTRAL_OTHER;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isMapaResponse(value: unknown): value is MapaResponse {
  return (
    isRecord(value) &&
    isRecord(value.cores) &&
    Array.isArray(value.estados) &&
    value.estados.every((e) => isRecord(e) && typeof e.uf === "string")
  );
}
