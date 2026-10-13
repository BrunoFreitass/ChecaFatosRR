import { Link } from "react-router";

import { Comments } from "../components/Comments.tsx";

/** Feed geral de comentários (sem checagem). */
export function Comunidade() {
  return (
    <div className="space-y-4">
      <title>Comunidade — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold sm:text-3xl">Comunidade</h1>
      <p className="text-slate-700">
        Converse sobre as eleições em Roraima. Comentários com desinformação, ataques ou ameaças são
        bloqueados; veja as regras nos{" "}
        <Link to="/termos" className="font-semibold text-brand-700 underline">
          Termos de Uso
        </Link>
        .
      </p>
      <Comments checagem={null} title="Comentários" />
    </div>
  );
}
