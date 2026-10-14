// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/AuthContext.ts";
import { RequireAdmin } from "../auth/guards.tsx";
import { painelFixture } from "../lib/painel.fixture.ts";
import type { Profile } from "../lib/types.ts";
import PainelAdmin from "./PainelAdmin.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api.ts", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly code: string,
      message: string,
    ) {
      super(message);
    }
  },
  apiFetch: api.apiFetch,
}));
const { ApiError } = await import("../lib/api.ts");

const ADMIN: Profile = {
  apelido: "admin_rr",
  status: "active",
  suspended_until: null,
  is_admin: true,
  terms_version: "2026-10-14",
  created_at: "2026-10-09T12:00:00Z",
};

function auth(profile: Profile): AuthContextValue {
  return {
    status: "ready",
    profile,
    errorMessage: null,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
    setProfile: vi.fn(),
    reloadProfile: vi.fn(),
  };
}

function renderPage(profile: Profile = ADMIN) {
  render(
    <AuthContext.Provider value={auth(profile)}>
      <MemoryRouter>
        <RequireAdmin>
          <PainelAdmin />
        </RequireAdmin>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

function table(name: RegExp): HTMLElement {
  return screen.getByRole("table", { name });
}

beforeEach(() => {
  api.apiFetch.mockReset();
});
afterEach(cleanup);

describe("PainelAdmin", () => {
  it("quem não é admin vê o aviso e a API nem é chamada", () => {
    renderPage({ ...ADMIN, is_admin: false });

    expect(screen.getByText("Só a administração do site pode ver esta página.")).toBeTruthy();
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it("busca o painel com o login do admin (não anônimo) e sinal de cancelamento", async () => {
    api.apiFetch.mockResolvedValue(painelFixture());
    renderPage();
    await screen.findByRole("table", { name: /Cota do Gemini hoje/ });

    expect(api.apiFetch).toHaveBeenCalledWith("/admin/painel", { signal: expect.any(AbortSignal) });
  });

  it("cota do Gemini de hoje com o restante, por uso e por modelo", async () => {
    api.apiFetch.mockResolvedValue(painelFixture());
    renderPage();
    const cota = await screen.findByRole("table", { name: /Cota do Gemini hoje/ });

    expect(within(cota).getByRole("row", { name: /Checagem/ }).textContent).toMatch(/36.*1\.000.*964/);
    expect(within(cota).getByRole("row", { name: /Moderação/ }).textContent).toMatch(/120.*2\.000.*1\.880/);
    const modelos = table(/Modelos do Gemini hoje/);
    expect(within(modelos).getByRole("row", { name: /gemini-3\.8-flash/ }).textContent).toMatch(/18.*20.*2/);
    expect(within(modelos).getByRole("row", { name: /gemini-3\.1-flash-lite/ }).textContent).toMatch(
      /checagem e moderação/,
    );
  });

  it("checagens, atividade e tokens dos últimos 7 dias, hoje primeiro", async () => {
    api.apiFetch.mockResolvedValue(painelFixture());
    renderPage();
    const checagens = await screen.findByRole("table", { name: /Checagens por dia/ });

    const rows = within(checagens).getAllByRole("row");
    expect(rows).toHaveLength(8); // cabeçalho + 7 dias
    expect(rows[1]?.textContent).toMatch(/^14\/10/);
    expect(rows[1]?.textContent).toMatch(/16/); // total de hoje
    expect(within(table(/Atividade por dia/)).getAllByRole("row")).toHaveLength(8);
    const tokens = table(/Tokens por dia/);
    expect(within(tokens).getAllByRole("row")[1]?.textContent).toMatch(/25\.200.*4\.560/);
  });

  it("fila pendente com link para a moderação e usuários só em totais", async () => {
    api.apiFetch.mockResolvedValue(painelFixture());
    renderPage();
    const fila = await screen.findByRole("link", { name: /Fila de moderação/ });

    expect(fila.getAttribute("href")).toBe("/admin/moderacao");
    expect(fila.textContent).toMatch(/4 pendentes/);
    expect(screen.getByText(/312 usuários/)).toBeTruthy();
    expect(screen.getByText(/2 suspensos/)).toBeTruthy();
    expect(screen.getByText(/1 banido/)).toBeTruthy();
    expect(screen.getByText(/nada por pessoa/i)).toBeTruthy();
  });

  it("link para as visitas no Cloudflare Web Analytics, em nova aba", async () => {
    api.apiFetch.mockResolvedValue(painelFixture());
    renderPage();

    const link = await screen.findByRole("link", { name: /Cloudflare Web Analytics/ });
    expect(link.getAttribute("href")).toBe("https://dash.cloudflare.com/?to=/:account/web-analytics");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("erro da API aparece como alerta", async () => {
    api.apiFetch.mockImplementation(async () => {
      throw new ApiError(403, "forbidden", "Você não tem permissão para isso.");
    });
    renderPage();

    expect((await screen.findByRole("alert")).textContent).toMatch(/Você não tem permissão/);
  });

  it("resposta fora do formato não quebra a tela", async () => {
    api.apiFetch.mockResolvedValue({ checagens: "x" });
    renderPage();

    expect((await screen.findByRole("alert")).textContent).toMatch(/Não foi possível carregar o painel/);
  });

  it("Recarregar busca de novo", async () => {
    api.apiFetch.mockResolvedValue(painelFixture());
    renderPage();
    await screen.findByRole("table", { name: /Cota do Gemini hoje/ });

    fireEvent.click(screen.getByRole("button", { name: "Recarregar" }));

    await vi.waitFor(() => expect(api.apiFetch).toHaveBeenCalledTimes(2));
  });
});
