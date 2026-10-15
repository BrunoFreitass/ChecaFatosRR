// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  A11Y_KEY,
  applyPrefs,
  classesFor,
  DEFAULT_PREFS,
  FONT_MAX,
  FONT_MIN,
  loadPrefs,
  savePrefs,
  type A11yPrefs,
} from "./a11y.ts";
// Texto do script de antes da 1ª pintura (o mesmo arquivo que o index.html carrega).
import initScript from "../../public/a11y-init.js?raw";

const ALL_ON: A11yPrefs = {
  fonte: 3,
  contraste: true,
  inverter: true,
  cinza: true,
  links: true,
  espaco: true,
  pausar: true,
  cursor: true,
};

beforeEach(() => {
  localStorage.clear();
  document.documentElement.className = "";
});
afterEach(() => vi.restoreAllMocks());

describe("preferências de acessibilidade", () => {
  it("padrão não liga nada", () => {
    expect(classesFor(DEFAULT_PREFS)).toEqual([]);
  });

  it("cada opção vira uma classe no <html>", () => {
    expect(classesFor(ALL_ON).sort()).toEqual(
      [
        "a11y-fonte-3",
        "a11y-contraste",
        "a11y-inverter",
        "a11y-cinza",
        "a11y-links",
        "a11y-espaco",
        "a11y-pausar",
        "a11y-cursor",
      ].sort(),
    );
    expect(classesFor({ ...DEFAULT_PREFS, fonte: -1 })).toEqual(["a11y-fonte-menos"]);
  });

  it("aplicar troca as classes sem mexer nas outras do <html>", () => {
    document.documentElement.classList.add("outra");
    applyPrefs(ALL_ON);
    applyPrefs({ ...DEFAULT_PREFS, links: true });

    expect([...document.documentElement.classList].sort()).toEqual(["a11y-links", "outra"]);
  });

  it("salva e lê de volta", () => {
    savePrefs({ ...DEFAULT_PREFS, fonte: 2, contraste: true });

    expect(loadPrefs()).toEqual({ ...DEFAULT_PREFS, fonte: 2, contraste: true });
  });

  it("valor estragado ou fora do intervalo volta ao padrão, campo a campo", () => {
    localStorage.setItem(A11Y_KEY, "{lixo");
    expect(loadPrefs()).toEqual(DEFAULT_PREFS);

    localStorage.setItem(A11Y_KEY, JSON.stringify({ fonte: 99, contraste: "sim", links: true }));
    expect(loadPrefs()).toEqual({ ...DEFAULT_PREFS, links: true });
    expect(FONT_MIN).toBe(-1);
    expect(FONT_MAX).toBe(3);
  });

  it("localStorage bloqueado (modo privado) não quebra: lê o padrão e não salva", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("bloqueado");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("bloqueado");
    });

    expect(loadPrefs()).toEqual(DEFAULT_PREFS);
    expect(() => savePrefs(ALL_ON)).not.toThrow();
  });

  it("o script de antes da 1ª pintura (public/a11y-init.js) aplica as mesmas classes", () => {
    savePrefs(ALL_ON);

    new Function(initScript)();

    expect([...document.documentElement.classList].sort()).toEqual(classesFor(ALL_ON).sort());
  });

  it("o script de antes da 1ª pintura e o loadPrefs concordam em valores variados", () => {
    const cases: unknown[] = [
      { fonte: -1 }, { fonte: 0 }, { fonte: 1 }, { fonte: 2 }, { fonte: 1.5 }, { fonte: 4 }, { fonte: -2 },
      { fonte: "2" }, { contraste: "true" }, { contraste: 1 }, { links: true, fonte: 2 }, [], null, 7, "x",
      { cursor: true, pausar: false, espaco: true },
    ];
    for (const value of cases) {
      localStorage.setItem(A11Y_KEY, JSON.stringify(value));
      document.documentElement.className = "";
      new Function(initScript)();
      const fromInit = [...document.documentElement.classList].sort();
      expect(fromInit, JSON.stringify(value)).toEqual(classesFor(loadPrefs()).sort());
    }
  });

  it("o script de antes da 1ª pintura ignora valor estragado", () => {
    localStorage.setItem(A11Y_KEY, "{lixo");

    expect(() => new Function(initScript)()).not.toThrow();
    expect(document.documentElement.className).toBe("");
  });
});
