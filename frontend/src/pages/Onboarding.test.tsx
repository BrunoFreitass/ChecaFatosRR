// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/AuthContext.ts";
import { ApiError } from "../lib/api.ts";
import { Onboarding } from "./Onboarding.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api.ts", async () => {
  const actual = await vi.importActual<typeof import("../lib/api.ts")>("../lib/api.ts");
  return { ApiError: actual.ApiError, apiFetch: api.apiFetch };
});

const NEEDS_PROFILE: AuthContextValue = {
  status: "needs_profile",
  profile: null,
  errorMessage: null,
  signInWithGoogle: async () => undefined,
  signOut: async () => undefined,
  setProfile: () => undefined,
  reloadProfile: () => undefined,
};

type Options = { method?: string; body?: unknown; signal?: AbortSignal };

function renderPage() {
  return render(
    <AuthContext value={NEEDS_PROFILE}>
      <MemoryRouter>
        <Onboarding />
      </MemoryRouter>
    </AuthContext>,
  );
}

function fillForm() {
  fireEvent.change(screen.getByLabelText("Apelido"), { target: { value: "bruno_rr" } });
  fireEvent.click(screen.getByRole("checkbox"));
}

const submit = () => screen.getByRole("button", { name: "Continuar" }) as HTMLButtonElement;

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  api.apiFetch.mockReset();
});

describe("Onboarding: versão dos termos", () => {
  it("o botão fica desabilitado até a versão chegar do servidor e o aceite usa essa versão", async () => {
    let releaseVersion: (value: { version: string }) => void = () => undefined;
    api.apiFetch.mockImplementation((path: string, options: Options = {}) => {
      if (path === "/legal/version") {
        return new Promise((resolve) => {
          releaseVersion = resolve;
        });
      }
      if (path.startsWith("/apelido/disponivel")) return Promise.resolve({ disponivel: true });
      if (path === "/me" && options.method === "POST") return new Promise(() => undefined);
      return Promise.reject(new Error(`inesperado: ${path}`));
    });
    renderPage();
    fillForm();
    await vi.advanceTimersByTimeAsync(500);
    expect(await screen.findByText("Disponível!")).toBeTruthy();

    expect(submit().disabled).toBe(true); // apelido ok e termos marcados, mas sem versão

    releaseVersion({ version: "2026-10-13" });
    await vi.waitFor(() => expect(submit().disabled).toBe(false));
    fireEvent.click(submit());

    expect(api.apiFetch).toHaveBeenCalledWith("/me", {
      method: "POST",
      body: { apelido: "bruno_rr", aceito_termos: true, terms_version: "2026-10-13" },
    });
  });

  it("falha ao ler a versão: avisa, não envia com versão inventada e permite tentar de novo", async () => {
    let calls = 0;
    api.apiFetch.mockImplementation((path: string) => {
      if (path === "/legal/version") {
        calls += 1;
        return calls === 1 ? Promise.reject(new Error("rede")) : Promise.resolve({ version: "2026-10-13" });
      }
      if (path.startsWith("/apelido/disponivel")) return Promise.resolve({ disponivel: true });
      return Promise.reject(new Error(`inesperado: ${path}`));
    });
    renderPage();
    fillForm();
    await vi.advanceTimersByTimeAsync(500);

    expect(await screen.findByText(/Não foi possível carregar a versão dos Termos/)).toBeTruthy();
    expect(submit().disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Tentar de novo" }));
    await vi.waitFor(() => expect(submit().disabled).toBe(false));
    expect(calls).toBe(2);
  });

  it("termos atualizados no meio do cadastro: desmarca o aceite e busca a versão nova", async () => {
    const versions = ["2026-10-13", "2026-10-20"];
    let calls = 0;
    api.apiFetch.mockImplementation((path: string, options: Options = {}) => {
      if (path === "/legal/version") return Promise.resolve({ version: versions[Math.min(calls++, 1)] });
      if (path.startsWith("/apelido/disponivel")) return Promise.resolve({ disponivel: true });
      if (path === "/me" && options.method === "POST") {
        return Promise.reject(
          new ApiError(422, "termos_desatualizados", "Os Termos de Uso mudaram.", {}),
        );
      }
      return Promise.reject(new Error(`inesperado: ${path}`));
    });
    renderPage();
    fillForm();
    await vi.advanceTimersByTimeAsync(500);
    await vi.waitFor(() => expect(submit().disabled).toBe(false));

    fireEvent.click(submit());

    expect(await screen.findByText(/Os Termos de Uso mudaram/)).toBeTruthy();
    expect(screen.getByText(/marque de novo o aceite/)).toBeTruthy();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
    expect(calls).toBe(2);
  });

  it("enquanto a versão carrega, o botão desabilitado explica o motivo", () => {
    api.apiFetch.mockImplementation(() => new Promise(() => undefined));
    renderPage();

    const hint = screen.getByText("Carregando os Termos de Uso…");
    expect(submit().getAttribute("aria-describedby")).toBe(hint.id);
  });

  it("a leitura da versão é anônima e cancelada ao sair da página", () => {
    api.apiFetch.mockImplementation(() => new Promise(() => undefined));
    const { unmount } = renderPage();

    const [path, options] = api.apiFetch.mock.calls.find(([p]) => p === "/legal/version") ?? [];
    expect(path).toBe("/legal/version");
    expect(options.anonymous).toBe(true);
    expect(options.signal.aborted).toBe(false);

    unmount();
    expect(options.signal.aborted).toBe(true);
  });
});
