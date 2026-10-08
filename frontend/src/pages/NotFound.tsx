import { Link } from "react-router";

export function NotFound() {
  return (
    <section className="space-y-4">
      <title>Página não encontrada — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold">Página não encontrada</h1>
      <p className="text-slate-700">O endereço que você abriu não existe ou foi removido.</p>
      <Link
        to="/"
        className="inline-flex min-h-11 items-center font-semibold text-brand-700 underline"
      >
        Voltar para o início
      </Link>
    </section>
  );
}
