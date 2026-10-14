// Só para testes (Vitest e e2e): resposta fictícia do GET /admin/painel. O app não importa este
// arquivo, então ele não vai para o bundle.
import type { PainelResponse } from "./painel.types.ts";

const DIAS = ["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"];

export function painelFixture(): PainelResponse {
  return {
    gerado_em: "2026-10-14T16:30:00Z",
    checagens: DIAS.map((dia, i) => ({
      dia,
      total: 10 + i,
      verdadeiro: 1,
      enganoso: 2,
      falso: 3 + i,
      fora_de_contexto: 1,
      nao_verificavel: 3,
      so_agencias: i === 6 ? 2 : 0,
    })),
    tokens: DIAS.map((dia, i) => ({
      dia,
      checagens: i === 6 ? 12 : 0,
      entrada: i === 6 ? 25_200 : 0,
      saida: i === 6 ? 4_560 : 0,
      raciocinio: 0,
    })),
    pedidos: DIAS.map((dia, i) => ({ dia, total: 20 + i, do_cache: 5 })),
    comentarios: DIAS.map((dia, i) => ({ dia, total: 4 + i, ocultos: i === 6 ? 1 : 0 })),
    denuncias: DIAS.map((dia, i) => ({ dia, total: i === 6 ? 3 : 0 })),
    usuarios_novos: DIAS.map((dia, i) => ({ dia, total: 2 + i })),
    fila: { pendentes: 4, revisao: 3, confirmacao_strike: 1 },
    usuarios: { total: 312, suspensos: 2, banidos: 1 },
    gemini: {
      dia_pacifico: "2026-10-14",
      cota: [
        { uso: "checagem", usadas: 36, limite: 1000, restante: 964 },
        { uso: "moderacao", usadas: 120, limite: 2000, restante: 1880 },
      ],
      modelos: [
        { modelo: "gemini-3.8-flash", usadas: 18, limite: 20, restante: 2, checagem: true, moderacao: false },
        { modelo: "gemini-3.1-flash-lite", usadas: 140, limite: 500, restante: 360, checagem: true, moderacao: true },
        { modelo: "gemma-3-27b-it", usadas: 0, limite: 14400, restante: 14400, checagem: false, moderacao: true },
      ],
    },
  };
}
