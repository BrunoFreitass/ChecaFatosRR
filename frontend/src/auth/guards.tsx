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

/** Página do primeiro acesso: exige login, mas ainda SEM perfil. */
export function RequireSessionWithoutProfile({ children }: { children: ReactNode }) {
  const { status, errorMessage } = useAuth();

  if (status === "loading") return <Loading />;
  if (status === "error") return <AuthError message={errorMessage} />;
  if (status === "anonymous") return <Navigate to="/entrar" replace />;
  if (status === "ready") return <Navigate to="/" replace />;
  return children;
}
