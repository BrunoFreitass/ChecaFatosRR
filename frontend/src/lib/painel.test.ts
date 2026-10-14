import { describe, expect, it } from "vitest";

import { painelFixture } from "./painel.fixture.ts";
import { isPainelResponse } from "./painel.ts";

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
    ];
    for (const value of bad) expect(isPainelResponse(value), JSON.stringify(value)).toBe(false);
  });
});
