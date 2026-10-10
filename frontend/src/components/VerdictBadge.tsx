import { AWAITING_CHECK_LABEL, verdictLabel } from "../lib/format.ts";
import type { Verdict } from "../lib/types.ts";

// Cor + ícone + texto: o veredito nunca depende só da cor (daltonismo, tela em preto e branco).
const STYLE: Record<Verdict, { icon: string; className: string }> = {
  verdadeiro: { icon: "✓", className: "bg-green-100 text-green-900 ring-green-700/30" },
  enganoso: { icon: "!", className: "bg-amber-100 text-amber-900 ring-amber-700/30" },
  falso: { icon: "✕", className: "bg-red-100 text-red-900 ring-red-700/30" },
  fora_de_contexto: { icon: "↺", className: "bg-orange-100 text-orange-900 ring-orange-700/30" },
  nao_verificavel: { icon: "?", className: "bg-slate-200 text-slate-900 ring-slate-700/30" },
};
// "Ainda sem checagem": cinza claro e reticências, sem nenhum ar de alerta.
const AWAITING = { icon: "…", className: "bg-slate-100 text-slate-700 ring-slate-400/40" };

export function VerdictBadge({
  verdict,
  size = "md",
  awaitingCheck = false,
}: {
  verdict: Verdict;
  size?: "md" | "lg";
  /** Não verificável sem checagem de agência: mostra o neutro "Ainda sem checagem". */
  awaitingCheck?: boolean;
}) {
  const neutral = awaitingCheck && verdict === "nao_verificavel";
  // Veredito desconhecido (API mais nova que o site em cache): mostra como "Não verificável".
  const { icon, className } = neutral ? AWAITING : (STYLE[verdict] ?? STYLE.nao_verificavel);
  const sizing = size === "lg" ? "gap-2 px-4 py-2 text-lg" : "gap-1.5 px-2.5 py-1 text-sm";
  return (
    <span
      className={`inline-flex items-center rounded-full font-bold ring-1 ring-inset ${sizing} ${className}`}
    >
      <span aria-hidden="true">{icon}</span>
      <span>{neutral ? AWAITING_CHECK_LABEL : verdictLabel(verdict)}</span>
    </span>
  );
}
