import { useLocation } from "react-router";

export function Home() {
  const location = useLocation();
  const accountDeleted = (location.state as { contaExcluida?: boolean } | null)?.contaExcluida;

  return (
    <section className="space-y-4">
      <title>ChecaFatosRR — checagem de notícias</title>
      {accountDeleted && (
        <p role="status" className="rounded-lg bg-green-50 p-4 text-green-900">
          Sua conta foi excluída.
        </p>
      )}
      <h1 className="text-2xl font-bold leading-tight sm:text-3xl">Recebeu um link suspeito?</h1>
      <p className="text-base text-slate-700">
        Cole o link de uma notícia e veja o que as agências de checagem já disseram sobre o assunto,
        com uma explicação simples sobre o conteúdo.
      </p>
      <p className="rounded-lg bg-brand-50 p-4 text-sm text-brand-800">
        Em breve: checagem de links. Estamos preparando o site.
      </p>
    </section>
  );
}
