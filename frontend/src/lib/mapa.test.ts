import { describe, expect, it } from "vitest";

import { fillFor, isMapaResponse, leaderOf, marginBand, NEUTRAL_OTHER, NO_DATA, UF_NAMES } from "./mapa.ts";

const CORES = { "13": "vermelho", "22": "azul" } as const;

function resultado(a: number, b: number, lider = "13") {
  const outro = lider === "13" ? "22" : "13";
  return {
    candidatos: [
      { numero: lider, nome: lider === "13" ? "LULA" : "FLAVIO BOLSONARO", pct: a },
      { numero: outro, nome: outro === "13" ? "LULA" : "FLAVIO BOLSONARO", pct: b },
    ],
    secoes_pct: 50,
    final: false,
    totalizado_em: null,
  };
}

describe("mapa: cores e faixas", () => {
  it("tem os 27 estados com nome", () => {
    expect(Object.keys(UF_NAMES)).toHaveLength(27);
    expect(UF_NAMES.rr).toBe("Roraima");
  });

  it("faixas de margem: até 5, 15, 30 e acima de 30 pontos", () => {
    expect(marginBand(2)).toBe(0);
    expect(marginBand(10)).toBe(1);
    expect(marginBand(20)).toBe(2);
    expect(marginBand(45)).toBe(3);
  });

  it("estado sem dados fica cinza", () => {
    expect(fillFor(null, CORES)).toBe(NO_DATA);
  });

  it("vermelho e azul têm as mesmas 4 faixas, mais forte com margem maior", () => {
    const fracoVermelho = fillFor(resultado(51, 49, "13"), CORES);
    const forteVermelho = fillFor(resultado(80, 20, "13"), CORES);
    const fracoAzul = fillFor(resultado(51, 49, "22"), CORES);
    expect(fracoVermelho).not.toBe(forteVermelho);
    expect(fracoVermelho).not.toBe(fracoAzul);
  });

  it("líder sem cor configurada fica em cinza neutro (ensaio do 1º turno)", () => {
    const outro = {
      ...resultado(40, 30),
      candidatos: [
        { numero: "70", nome: "ESCRITOR AUGUSTO CURY", pct: 40 },
        { numero: "13", nome: "LULA", pct: 30 },
      ],
    };
    expect(leaderOf(outro)?.numero).toBe("70");
    expect(fillFor(outro, CORES)).toBe(NEUTRAL_OTHER);
  });

  it("valida a resposta da API", () => {
    expect(isMapaResponse({ cores: CORES, estados: [{ uf: "ac", resultado: null }] })).toBe(true);
    expect(isMapaResponse({ estados: "x" })).toBe(false);
  });
});
