import { describe, expect, it } from "vitest";

import {
  fillFor,
  isMapaResponse,
  leaderOf,
  marginBand,
  NEUTRAL_OTHER,
  NO_DATA,
  rankedCandidates,
  standingOf,
  TIE,
  UF_NAMES,
} from "./mapa.ts";

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
    expect(isMapaResponse({ cores: CORES, estados: [{ uf: "rr", resultado: resultado(60, 40) }] })).toBe(true);
    expect(isMapaResponse({ estados: "x" })).toBe(false);
  });

  it("recusa resposta malformada em vez de quebrar a página (revisão 14/10)", () => {
    const ok = resultado(60, 40);
    const bad = [
      { cores: [], estados: [] }, // cores como lista
      { cores: { "13": 1 }, estados: [] }, // cor que não é texto
      { cores: CORES, estados: [{ uf: "rr" }] }, // resultado ausente
      { cores: CORES, estados: [{ uf: "rr", resultado: { ...ok, candidatos: "x" } }] },
      { cores: CORES, estados: [{ uf: "rr", resultado: { ...ok, candidatos: [{ numero: "13", nome: "A", pct: "60" }] } }] },
      { cores: CORES, estados: [{ uf: "rr", resultado: { ...ok, candidatos: [{ numero: 13, nome: "A", pct: 60 }] } }] },
      { cores: CORES, estados: [{ uf: "rr", resultado: { ...ok, secoes_pct: "50" } }] },
    ];
    for (const value of bad) expect(isMapaResponse(value), JSON.stringify(value)).toBe(false);
  });

  it("ordena os candidatos por percentual, sem depender da ordem da API", () => {
    expect(rankedCandidates(resultado(30, 70)).map((c) => c.pct)).toEqual([70, 30]);
  });

  it("nada apurado (tudo 0%, seções 0) fica sem dados, nunca na cor de quem veio primeiro", () => {
    const zerado = { ...resultado(0, 0), secoes_pct: 0 };
    expect(fillFor(zerado, CORES)).toBe(NO_DATA);
    expect(leaderOf(zerado)).toBeNull();
    expect(fillFor({ ...resultado(60, 40), secoes_pct: null }, CORES)).toBe(NO_DATA);
  });

  it("só um candidato com percentual não vira vitória folgada", () => {
    const um = { ...resultado(60, 40), candidatos: [{ numero: "13", nome: "LULA", pct: 60 }] };
    expect(fillFor(um, CORES)).toBe(NO_DATA);
  });

  it("empate exato fica cinza de empate, sem líder", () => {
    const empate = resultado(50, 50);
    expect(fillFor(empate, CORES)).toBe(TIE);
    expect(leaderOf(empate)).toBeNull();
    expect(standingOf(empate).kind).toBe("tie");
  });

  it("número com nome de propriedade do objeto não vira cor", () => {
    const outro = {
      ...resultado(60, 40),
      candidatos: [
        { numero: "constructor", nome: "X", pct: 60 },
        { numero: "13", nome: "LULA", pct: 40 },
      ],
    };
    expect(fillFor(outro, CORES)).toBe(NEUTRAL_OTHER);
  });
});
