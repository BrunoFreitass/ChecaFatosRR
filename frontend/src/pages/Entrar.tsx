import { useEffect, useState } from "react";
import { Navigate, useSearchParams } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";
import { Loading } from "../components/Loading.tsx";
import { ApiError } from "../lib/api.ts";
import { safeReturnPath } from "../lib/returnPath.ts";

export function Entrar() {
  const { status, signInWithGoogle } = useAuth();
  const [params] = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const returnPath = safeReturnPath(params.get("volta"));

  // Voltar do Google com o botão "voltar" restaura a página do cache: destrava o botão.
  useEffect(() => {
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) setOpening(false);
    };
    window.addEventListener("pageshow", onPageShow);
    return () => window.removeEventListener("pageshow", onPageShow);
  }, []);

  if (status === "loading") return <Loading />;
  if (status === "ready") return <Navigate to={returnPath} replace />;
  if (status === "needs_profile") return <Navigate to="/bem-vindo" replace />;

  async function handleClick() {
    setError(null);
    setOpening(true);
    try {
      await signInWithGoogle(returnPath);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Não foi possível abrir o login.");
      setOpening(false);
    }
  }

  return (
    <section className="mx-auto max-w-md space-y-5">
      <title>Entrar — ChecaFatosRR</title>
      <h1 className="text-2xl font-bold">Entrar</h1>
      <p className="text-slate-700">
        Use sua conta Google. No site aparece só o apelido que você escolher: seu nome e seu
        e-mail nunca são mostrados.
      </p>
      <button
        type="button"
        onClick={handleClick}
        disabled={opening}
        className="inline-flex min-h-12 w-full items-center justify-center rounded-lg bg-brand-700 px-4 font-semibold text-white hover:bg-brand-800 disabled:opacity-60"
      >
        {opening ? "Abrindo o Google…" : "Entrar com Google"}
      </button>
      {error && (
        <p role="alert" className="text-sm text-red-800">
          {error}
        </p>
      )}
      <p className="text-sm text-slate-600">
        Ao entrar você concorda com os <a href="/termos" className="underline">Termos de Uso</a> e
        a <a href="/privacidade" className="underline">Política de Privacidade</a>.
      </p>
    </section>
  );
}
