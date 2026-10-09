import { Link } from "react-router";

import { useAuth } from "../auth/AuthContext.ts";

const LINK_CLASS =
  "inline-flex min-h-11 items-center rounded-md px-3 text-sm font-semibold text-brand-700 hover:bg-brand-50 focus-visible:outline-2 focus-visible:outline-brand-700";

export function UserMenu() {
  const { status, profile } = useAuth();

  if (status === "ready" && profile) {
    return (
      <Link to="/minha-conta" className={LINK_CLASS} aria-label={`Minha conta (${profile.apelido})`}>
        {profile.apelido}
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
