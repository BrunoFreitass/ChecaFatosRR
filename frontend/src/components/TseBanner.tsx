import { useEffect, useState } from "react";
import { Link } from "react-router";

import { apiFetch } from "../lib/api.ts";
import type { TseStatus } from "../lib/tse.ts";

/**
 * Banner neutro da noite da apuração. A janela (16h de Boa Vista de 25/10 até 26/10) vem do
 * servidor (/tse/status, configurável no painel): nada de deploy no dia.
 */
export function TseBanner() {
  const [status, setStatus] = useState<TseStatus | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch<TseStatus>("/tse/status", { anonymous: true, signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setStatus(data);
      })
      .catch(() => undefined); // o banner é um extra: sem ele, a home segue normal
    return () => controller.abort();
  }, []);

  if (!status?.banner || !status.apuracao) return null;
  return (
    <p className="rounded-lg border border-slate-300 bg-white p-4">
      <Link to="/apuracao" className="font-semibold text-brand-700 underline">
        {status.texto}
      </Link>
    </p>
  );
}
