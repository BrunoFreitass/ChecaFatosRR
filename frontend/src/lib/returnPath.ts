const KEY = "checafatosrr:volta";

function hasControlCharacter(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

/** Só caminhos internos ("/x"), nunca "//dominio" ou URLs absolutas (open redirect). */
export function safeReturnPath(value: string | null | undefined): string {
  if (
    !value ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.startsWith("/\\") ||
    hasControlCharacter(value)
  ) {
    return "/";
  }
  return value;
}

export function saveReturnPath(path: string): void {
  try {
    sessionStorage.setItem(KEY, safeReturnPath(path));
  } catch {
    // Navegação privada / armazenamento bloqueado: volta para o início.
  }
}

/** Só lê (sem apagar): é chamada durante a renderização, que pode rodar mais de uma vez. */
export function readReturnPath(): string {
  try {
    return safeReturnPath(sessionStorage.getItem(KEY));
  } catch {
    return "/";
  }
}
