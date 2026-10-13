import { useEffect, useState } from "react";

import { apiFetch } from "./api.ts";

/** Versão vigente dos Termos e da Política (fonte única: CURRENT_TERMS_VERSION no backend). */
export type LegalVersion =
  | { status: "loading"; version: null }
  | { status: "ready"; version: string }
  | { status: "error"; version: null };

/** Lê GET /legal/version (público). Sem fallback: sem resposta, não há versão. */
export function useLegalVersion(): { legal: LegalVersion; retry: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState<{ attempt: number; legal: LegalVersion } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch<{ version: string }>("/legal/version", { anonymous: true, signal: controller.signal })
      .then(({ version }) => {
        if (!controller.signal.aborted) setLoaded({ attempt, legal: { status: "ready", version } });
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoaded({ attempt, legal: { status: "error", version: null } });
      });
    return () => controller.abort();
  }, [attempt]);

  // Resposta de uma tentativa anterior não vale: enquanto a nova não chega, é "loading".
  const legal: LegalVersion =
    loaded?.attempt === attempt ? loaded.legal : { status: "loading", version: null };
  return { legal, retry: () => setAttempt((n) => n + 1) };
}

/** "2026-10-13" → "13/10/2026" (sem fuso: é uma data de calendário). */
export function formatLegalVersion(version: string): string {
  const [year, month, day] = version.split("-");
  return year && month && day ? `${day}/${month}/${year}` : version;
}
