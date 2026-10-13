import { Link, Outlet } from "react-router";

import { RouteFocus } from "./RouteFocus.tsx";
import { UserMenu } from "./UserMenu.tsx";

const MAIN_ID = "conteudo";
const FOOTER_LINK = "inline-flex min-h-11 items-center underline";

export function Layout() {
  return (
    <div className="flex min-h-dvh flex-col bg-slate-50 text-slate-900">
      <a
        href={`#${MAIN_ID}`}
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:rounded focus:bg-white focus:p-3 focus:font-semibold"
      >
        Pular para o conteúdo
      </a>
      <RouteFocus targetId={MAIN_ID} />

      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-2 px-4">
          <Link
            to="/"
            className="inline-flex min-h-12 items-center text-lg font-bold tracking-tight focus-visible:outline-2 focus-visible:outline-brand-700"
          >
            ChecaFatos<span className="text-brand-700">RR</span>
          </Link>
          <nav aria-label="Principal" className="flex items-center gap-1">
            <Link
              to="/apuracao"
              className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-semibold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-brand-700"
            >
              Apuração
            </Link>
            <Link
              to="/ultimas"
              className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-semibold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-brand-700"
            >
              Últimas
            </Link>
            <Link
              to="/comunidade"
              className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-semibold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-brand-700"
            >
              Comunidade
            </Link>
            <UserMenu />
          </nav>
        </div>
      </header>

      <main id={MAIN_ID} tabIndex={-1} className="mx-auto w-full max-w-5xl flex-1 px-4 py-6 outline-none">
        <Outlet />
      </main>

      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto max-w-5xl space-y-2 px-4 py-4 text-sm text-slate-600">
          <p>Projeto apartidário de combate à desinformação nas eleições · Roraima</p>
          <nav aria-label="Informações legais" className="flex flex-wrap gap-x-4">
            <Link to="/termos" className={FOOTER_LINK}>
              Termos de Uso
            </Link>
            <Link to="/privacidade" className={FOOTER_LINK}>
              Privacidade
            </Link>
          </nav>
        </div>
      </footer>
    </div>
  );
}
