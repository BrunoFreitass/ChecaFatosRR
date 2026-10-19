// Menu de acessibilidade (exceção ao congelamento, 16/10/2026): preferências do visitante,
// guardadas só no navegador (localStorage) e aplicadas como classes no <html>. O CSS está em
// index.css. ESPELHO: public/a11y-init.js aplica as mesmas classes antes da 1ª pintura (teste
// em a11y.test.ts garante que os dois concordam).

export const A11Y_KEY = "checafatos:a11y";

/** Tamanho da fonte em passos: -1 (menor), 0 (padrão) e +1 a +3 (maior). */
export const FONT_MIN = -1;
export const FONT_MAX = 3;

export interface A11yPrefs {
  fonte: number;
  contraste: boolean;
  inverter: boolean;
  cinza: boolean;
  links: boolean;
  espaco: boolean;
  pausar: boolean;
  cursor: boolean;
}

export const DEFAULT_PREFS: A11yPrefs = {
  fonte: 0,
  contraste: false,
  inverter: false,
  cinza: false,
  links: false,
  espaco: false,
  pausar: false,
  cursor: false,
};

const TOGGLES = ["contraste", "inverter", "cinza", "links", "espaco", "pausar", "cursor"] as const;

const FONT_CLASS: Record<number, string> = {
  [-1]: "a11y-fonte-menos",
  1: "a11y-fonte-1",
  2: "a11y-fonte-2",
  3: "a11y-fonte-3",
};

export function classesFor(prefs: A11yPrefs): string[] {
  // Contraste e inverter se excluem (juntos, a página fica branca): vale o contraste.
  const on = (key: (typeof TOGGLES)[number]) => prefs[key] && !(key === "inverter" && prefs.contraste);
  const classes = TOGGLES.filter(on).map((key) => `a11y-${key}`);
  const font = FONT_CLASS[prefs.fonte];
  return font ? [font, ...classes] : classes;
}

/** Troca só as classes a11y-* do <html> (as outras ficam). */
export function applyPrefs(prefs: A11yPrefs, root: HTMLElement = document.documentElement): void {
  for (const name of [...root.classList]) {
    if (name.startsWith("a11y-")) root.classList.remove(name);
  }
  root.classList.add(...classesFor(prefs));
}

/** Lê do navegador; valor estragado ou bloqueado (modo privado) volta ao padrão, campo a campo. */
export function loadPrefs(): A11yPrefs {
  let raw: unknown;
  try {
    raw = JSON.parse(localStorage.getItem(A11Y_KEY) ?? "null");
  } catch {
    return { ...DEFAULT_PREFS };
  }
  if (typeof raw !== "object" || raw === null) return { ...DEFAULT_PREFS };
  const data = raw as Record<string, unknown>;
  const prefs = { ...DEFAULT_PREFS };
  const font = data.fonte;
  if (typeof font === "number" && Number.isInteger(font) && font >= FONT_MIN && font <= FONT_MAX) {
    prefs.fonte = font;
  }
  for (const key of TOGGLES) {
    const value = data[key];
    if (typeof value === "boolean") prefs[key] = value;
  }
  // Salvos juntos por uma versão anterior: fica só o contraste (juntos, a página fica branca).
  if (prefs.contraste) prefs.inverter = false;
  return prefs;
}

export function savePrefs(prefs: A11yPrefs): void {
  try {
    localStorage.setItem(A11Y_KEY, JSON.stringify(prefs));
  } catch {
    // Sem armazenamento (modo privado, cota): vale só nesta visita.
  }
}

/** Animações paradas pelo menu ou pelo sistema (prefers-reduced-motion). */
export function motionReduced(): boolean {
  if (document.documentElement.classList.contains("a11y-pausar")) return true;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}
