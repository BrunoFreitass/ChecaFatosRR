// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/AuthContext.ts";
import { AuthCallback } from "./AuthCallback.tsx";

const auth = vi.hoisted(() => ({
  exchangeCodeForSession: vi.fn(),
  getSession: vi.fn(async () => ({ data: { session: null } })),
}));
vi.mock("../lib/supabase.ts", () => ({ supabase: { auth } }));

const READY: AuthContextValue = {
  status: "ready",
  profile: null,
  errorMessage: null,
  signInWithGoogle: async () => undefined,
  signOut: async () => undefined,
  setProfile: () => undefined,
  reloadProfile: () => undefined,
};

function renderCallback(code: string) {
  const path = `/auth/callback?code=${code}`;
  window.history.replaceState({}, "", path);
  const router = createMemoryRouter(
    [
      { path: "/auth/callback", element: <AuthCallback /> },
      { path: "/", element: <p>Página inicial</p> },
    ],
    { initialEntries: [path] },
  );
  render(
    <StrictMode>
      <AuthContext value={READY}>
        <RouterProvider router={router} />
      </AuthContext>
    </StrictMode>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("/auth/callback no StrictMode", () => {
  it("o efeito roda duas vezes, mas o code é trocado uma vez só", async () => {
    auth.exchangeCodeForSession.mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { data: { session: { access_token: "token-falso" } }, error: null };
    });

    renderCallback("codigo-1");

    expect(await screen.findByText("Página inicial")).toBeTruthy();
    expect(auth.exchangeCodeForSession).toHaveBeenCalledTimes(1);
    expect(auth.exchangeCodeForSession).toHaveBeenCalledWith("codigo-1");
  });

  it("mostra o motivo quando falta o code_verifier", async () => {
    auth.exchangeCodeForSession.mockResolvedValue({
      data: { session: null },
      error: {
        name: "AuthPKCECodeVerifierMissingError",
        code: "pkce_code_verifier_not_found",
        status: 400,
      },
    });
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    renderCallback("codigo-2");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("127.0.0.1 e localhost");
    expect(auth.exchangeCodeForSession).toHaveBeenCalledTimes(1);
  });
});
