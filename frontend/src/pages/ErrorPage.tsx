export function ErrorPage() {
  return (
    <main className="mx-auto max-w-xl space-y-4 px-4 py-10 text-slate-900">
      <title>Erro — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold">Algo deu errado</h1>
      <p className="text-slate-700">
        Não foi possível mostrar esta página. Tente recarregar ou volte para o início.
      </p>
      <a href="/" className="inline-flex min-h-11 items-center font-semibold text-brand-700 underline">
        Voltar para o início
      </a>
    </main>
  );
}
