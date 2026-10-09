import type { ReactNode } from "react";

import { LEGAL } from "../../legal.ts";

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
  return (
    <article className="mx-auto max-w-2xl space-y-4 leading-relaxed [&_h2]:mt-6 [&_h2]:text-lg [&_h2]:font-semibold [&_li]:ml-5 [&_li]:list-disc">
      <title>{`${title} — ChecaFatosRR`}</title>
      <h1 className="text-2xl font-bold">{title}</h1>
      <p className="text-sm text-slate-600">
        Versão de {LEGAL.version}
        {LEGAL.preliminary && " · versão preliminar, sujeita a ajustes"}
      </p>
      {children}
    </article>
  );
}
