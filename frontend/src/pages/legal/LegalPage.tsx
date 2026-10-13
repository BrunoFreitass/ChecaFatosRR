import type { ReactNode } from "react";

import { LEGAL } from "../../legal.ts";
import { formatLegalVersion, useLegalVersion } from "../../lib/legalVersion.ts";

export function ContactEmail() {
  if (!LEGAL.contactEmail) {
    return <strong>[e-mail de contato a definir antes da publicação]</strong>;
  }
  return (
    <a href={`mailto:${LEGAL.contactEmail}`} className="font-semibold underline">
      {LEGAL.contactEmail}
    </a>
  );
}

export function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  const { legal } = useLegalVersion();
  return (
    <article className="mx-auto max-w-2xl space-y-4 leading-relaxed [&_h2]:mt-6 [&_h2]:text-lg [&_h2]:font-semibold [&_li]:ml-5 [&_li]:list-disc">
      <title>{`${title} — ChecaFatosRR`}</title>
      <h1 className="text-2xl font-bold">{title}</h1>
      <p role="status" className="text-sm text-slate-600">
        {legal.status === "ready" && `Versão de ${formatLegalVersion(legal.version)}`}
        {legal.status === "loading" && "Carregando a versão…"}
        {legal.status === "error" && "Versão vigente indisponível no momento"}
        {LEGAL.preliminary && " · versão preliminar, sujeita a ajustes"}
      </p>
      {children}
    </article>
  );
}
