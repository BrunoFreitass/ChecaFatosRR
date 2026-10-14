import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import { Loading } from "../components/Loading.tsx";
import { useAuth } from "./AuthContext.ts";

function AuthError({ message }: { message: string | null }) {
  const { reloadProfile } = useAuth();
  return (
    <div role="alert" className="space-y-3 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
      <p>{message ?? "Não foi possível carregar sua conta."}</p>
      <button
        type="button"
        onClick={reloadProfile}
        className="min-h-11 rounded-md bg-red-800 px-4 font-semibold text-white"
      >
        Tentar de novo
      </button>
    </div>
  );
}

/** Página que exige login E apelido escolhido. */
export function RequireProfile({ children }: { children: ReactNode }) {
  const { status, errorMessage } = useAuth();
  const location = useLocation();

  if (status === "loading") return <Loading />;
  if (status === "error") return <AuthError message={errorMessage} />;
  if (status === "anonymous") {
    const volta = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/entrar?volta=${volta}`} replace />;
  }
  if (status === "needs_profile") return <Navigate to="/bem-vindo" replace />;
  return children;
}

/**
 * Página só do admin: exige perfil e is_admin. Quem não é admin vê um aviso e a página nem
 * monta (nada chama a API). O backend confere de novo (require_admin, 403): isto é só a tela.
 */
export function RequireAdmin({ children }: { children: ReactNode }) {
  return (
    <RequireProfile>
      <AdminOnly>{children}</AdminOnly>
    </RequireProfile>
  );
}

function AdminOnly({ children }: { children: ReactNode }) {
  const { profile } = useAuth();
  if (!profile?.is_admin) {
    return (
      <section className="space-y-2">
        <title>Acesso restrito — ChecaFatosRR</title>
        <h1 className="text-2xl font-bold">Acesso restrito</h1>
        <p className="text-slate-700">Só a administração do site pode ver esta página.</p>
      </section>
    );
  }
  return children;
}

/** Página do primeiro acesso: exige login, mas ainda SEM perfil. */
export function RequireSessionWithoutProfile({ children }: { children: ReactNode }) {
  const { status, errorMessage } = useAuth();

  if (status === "loading") return <Loading />;
  if (status === "error") return <AuthError message={errorMessage} />;
  if (status === "anonymous") return <Navigate to="/entrar" replace />;
  if (status === "ready") return <Navigate to="/" replace />;
  return children;
}
