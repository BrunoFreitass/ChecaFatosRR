import { useEffect, useState } from "react";
import { Link, Navigate } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import {
  CALLBACK_MESSAGES,
  createCallbackCompleter,
  type CallbackResult,
} from "../auth/oauthCallback.ts";
import { Loading } from "../components/Loading.tsx";
import { readReturnPath } from "../lib/returnPath.ts";
import { supabase } from "../lib/supabase.ts";

// No nível do módulo: sobrevive à dupla execução do efeito e à remontagem do componente.
const completeOAuthCallback = createCallbackCompleter(supabase.auth);

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

/** Destino do Google após o login: troca o `?code=` pela sessão (PKCE) e carrega o perfil. */
export function AuthCallback() {
  const { status, errorMessage } = useAuth();
  const [result, setResult] = useState<CallbackResult | null>(null);

  useEffect(() => {
    let active = true;
    void completeOAuthCallback(new URL(window.location.href)).then((outcome) => {
      if (!outcome.ok && import.meta.env.DEV) {
        console.error("[login] falhou:", outcome.reason, outcome.detail ?? "");
      }
      if (active) setResult(outcome);
    });
    return () => {
      active = false;
    };
  }, []);

  if (result === null) return <Loading label="Entrando…" />;
  if (!result.ok) return <LoginFailed message={CALLBACK_MESSAGES[result.reason]} />;

  // Sessão criada: o AuthProvider busca o perfil em GET /me.
  if (status === "loading") return <Loading label="Entrando…" />;
  if (status === "needs_profile") return <Navigate to="/bem-vindo" replace />;
  if (status === "ready") return <Navigate to={readReturnPath()} replace />;
  if (status === "error") return <LoginFailed message={errorMessage ?? "Tente de novo."} />;
  return (
    <LoginFailed message={errorMessage ?? "O login expirou ou não foi concluído. Tente de novo."} />
  );
}
