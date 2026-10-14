import { Link } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";

const LINK_CLASS =
  "inline-flex min-h-11 items-center rounded-md px-2 text-sm sm:px-3 font-semibold text-brand-700 hover:bg-brand-50 focus-visible:outline-2 focus-visible:outline-brand-700";

export function UserMenu() {
  const { status, profile } = useAuth();

  if (status === "ready" && profile) {
    return (
      // Apelido longo (até 20 caracteres) cortado com "…" no menu; o nome acessível tem o inteiro.
      <Link
        to="/minha-conta"
        className={`${LINK_CLASS} max-w-40`}
        aria-label={`Minha conta (${profile.apelido})`}
      >
        <span className="min-w-0 truncate">{profile.apelido}</span>
      </Link>
    );
  }
  if (status === "needs_profile") {
    return (
      <Link to="/bem-vindo" className={LINK_CLASS}>
        Escolher apelido
      </Link>
    );
  }
  if (status === "anonymous") {
    return (
      <Link to="/entrar" className={LINK_CLASS}>
        Entrar
      </Link>
    );
  }
  return null;
}
