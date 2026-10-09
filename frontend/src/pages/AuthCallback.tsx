import { Link, Navigate, useSearchParams } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import { Loading } from "../components/Loading.tsx";
import { readReturnPath } from "../lib/returnPath.ts";

function LoginFailed({ message }: { message: string }) {
  return (
    <section className="mx-auto max-w-md space-y-4">
      <title>Não foi possível entrar — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold">Não foi possível entrar</h1>
      <p role="alert" className="text-slate-700">
        {message}
      </p>
      <Link to="/entrar" className="inline-flex min-h-11 items-center font-semibold text-brand-700 underline">
        Tentar de novo
      </Link>
    </section>
  );
}

/** Destino do Google após o login. O supabase-js troca o código pela sessão sozinho (PKCE). */
export function AuthCallback() {
  const { status, errorMessage } = useAuth();
  const [params] = useSearchParams();

  // O Google/Supabase devolve erros na query (?error=) ou no fragmento (#error=).
  const hash = new URLSearchParams(window.location.hash.slice(1));
  if (params.get("error") || params.get("error_description") || hash.get("error")) {
    return <LoginFailed message="O login com o Google foi cancelado ou recusado." />;
  }
  if (status === "loading") return <Loading label="Entrando…" />;
  if (status === "needs_profile") return <Navigate to="/bem-vindo" replace />;
  if (status === "ready") return <Navigate to={readReturnPath()} replace />;
  if (status === "error") return <LoginFailed message={errorMessage ?? "Tente de novo."} />;
  return <LoginFailed message="O login expirou ou não foi concluído. Tente de novo." />;
}
