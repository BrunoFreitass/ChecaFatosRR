import { lazy, Suspense } from "react";
import { createBrowserRouter } from "react-router";

import { RequireAdmin, RequireProfile, RequireSessionWithoutProfile } from "./auth/guards.tsx";
import { Layout } from "./components/Layout.tsx";
import { Loading } from "./components/Loading.tsx";
import { AdminModeracao } from "./pages/AdminModeracao.tsx";
import { AuthCallback } from "./pages/AuthCallback.tsx";
import { Comunidade } from "./pages/Comunidade.tsx";
import { Entrar } from "./pages/Entrar.tsx";
import { ErrorPage } from "./pages/ErrorPage.tsx";
import { Home } from "./pages/Home.tsx";
import { Privacidade } from "./pages/legal/Privacidade.tsx";
import { Termos } from "./pages/legal/Termos.tsx";
import { MinhaConta } from "./pages/MinhaConta.tsx";
import { NotFound } from "./pages/NotFound.tsx";
import { Onboarding } from "./pages/Onboarding.tsx";
import { Resultado } from "./pages/Resultado.tsx";
import { Apuracao } from "./pages/Apuracao.tsx";
import { Ultimas } from "./pages/Ultimas.tsx";

// Só o admin usa: chunk separado, o bundle de quem lê o site não cresce.
const PainelAdmin = lazy(() => import("./pages/PainelAdmin.tsx"));

export const router = createBrowserRouter([
  {
    element: <Layout />,
    errorElement: <ErrorPage />,
    children: [
      { index: true, element: <Home /> },
      { path: "entrar", element: <Entrar /> },
      { path: "auth/callback", element: <AuthCallback /> },
      {
        path: "bem-vindo",
        element: (
          <RequireSessionWithoutProfile>
            <Onboarding />
          </RequireSessionWithoutProfile>
        ),
      },
      {
        path: "minha-conta",
        element: (
          <RequireProfile>
            <MinhaConta />
          </RequireProfile>
        ),
      },
      {
        path: "minha-conta/painel",
        element: (
          <RequireAdmin>
            <Suspense fallback={<Loading />}>
              <PainelAdmin />
            </Suspense>
          </RequireAdmin>
        ),
      },
      {
        path: "admin/moderacao",
        element: (
          <RequireProfile>
            <AdminModeracao />
          </RequireProfile>
        ),
      },
      { path: "checagem/:slug", element: <Resultado /> },
      { path: "comunidade", element: <Comunidade /> },
      { path: "ultimas", element: <Ultimas /> },
      { path: "apuracao", element: <Apuracao /> },
      { path: "termos", element: <Termos /> },
      { path: "privacidade", element: <Privacidade /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
