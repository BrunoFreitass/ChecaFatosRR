/**
 * O que a pessoa digitou antes de ser mandada para o login. Fica no sessionStorage (some ao
 * fechar a aba) e nunca na URL: o texto pode ser longo e ter dados pessoais.
 */
const KEY = "checafatosrr:rascunho";

export type CheckMode = "link" | "texto";

export interface PendingCheck {
  modo: CheckMode;
  valor: string;
}

export function savePendingCheck(pending: PendingCheck): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(pending));
  } catch {
    // Armazenamento bloqueado: a pessoa cola de novo depois do login.
  }
}

export function clearPendingCheck(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // Armazenamento bloqueado: nada a apagar.
  }
}

/** Só lê (sem apagar): pode rodar mais de uma vez no StrictMode. Apague com clearPendingCheck. */
export function readPendingCheck(): PendingCheck | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      "modo" in parsed &&
      "valor" in parsed &&
      (parsed.modo === "link" || parsed.modo === "texto") &&
      typeof parsed.valor === "string"
    ) {
      return { modo: parsed.modo, valor: parsed.valor };
    }
  } catch {
    // JSON inválido ou armazenamento bloqueado.
  }
  return null;
}
