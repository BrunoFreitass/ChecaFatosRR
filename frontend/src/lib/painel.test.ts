import { describe, expect, it, vi } from "vitest";

import { painelFixture } from "./painel.fixture.ts";
import { isPainelResponse } from "./painel.ts";

// Sem o módulo real da API: ele exige VITE_API_URL, que o CI não define.
vi.mock("./api.ts", () => ({ apiFetch: vi.fn() }));

describe("painel: validação da resposta", () => {
  it("aceita a resposta do GET /admin/painel", () => {
    expect(isPainelResponse(painelFixture())).toBe(true);
  });

  it("recusa resposta malformada (a tela não quebra)", () => {
    const ok = painelFixture();
    const bad: unknown[] = [
      null,
      [],
      { ...ok, checagens: "x" },
      { ...ok, checagens: [{ ...ok.checagens[0], total: "3" }] },
      { ...ok, tokens: [{ ...ok.tokens[0], entrada: null }] },
      { ...ok, fila: { pendentes: 1 } },
      { ...ok, usuarios: [] },
      { ...ok, gemini: { ...ok.gemini, cota: [{ uso: "checagem", usadas: 1 }] } },
      { ...ok, gemini: { ...ok.gemini, modelos: [{ ...ok.gemini.modelos[0], checagem: "sim" }] } },
      { ...ok, checagens: [{ ...ok.checagens[0], dia: "x" }, ...ok.checagens.slice(1)] }, // dia sem formato
      { ...ok, gemini: { ...ok.gemini, dia_pacifico: "ontem" } },
      { ...ok, denuncias: ok.denuncias.slice(1) }, // listas com dias diferentes
    ];
    for (const value of bad) expect(isPainelResponse(value), JSON.stringify(value)).toBe(false);
  });
});
