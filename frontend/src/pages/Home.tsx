import { Link, useLocation } from "react-router";

import { CheckForm } from "../components/CheckForm.tsx";
import { TseBanner } from "../components/TseBanner.tsx";

export function Home() {
  const location = useLocation();
  const accountDeleted = (location.state as { contaExcluida?: boolean } | null)?.contaExcluida;

  return (
    <section className="space-y-5">
      <title>ChecaFatosRR — checagem de notícias</title>
      <TseBanner />
      {accountDeleted && (
        <p role="status" className="rounded-lg bg-green-50 p-4 text-green-900">
          Sua conta foi excluída.
        </p>
      )}
      <h1 className="text-2xl font-bold leading-tight sm:text-3xl">
        Recebeu uma notícia ou mensagem suspeita?
      </h1>
      <p className="text-base text-slate-700">
        Cole o link da notícia ou o texto da mensagem e veja o que as agências de checagem já
        disseram, com uma explicação simples sobre o conteúdo.
      </p>
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
        <CheckForm />
      </div>
      <p className="text-slate-700">
        <Link to="/ultimas" className="font-semibold text-brand-700 underline">
          Ver as últimas checagens
        </Link>
      </p>
    </section>
  );
}
