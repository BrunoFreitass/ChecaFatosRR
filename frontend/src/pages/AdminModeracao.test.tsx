// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/AuthContext.ts";
import type { QueueItem } from "../lib/comments.ts";
import type { Profile } from "../lib/types.ts";
import { AdminModeracao } from "./AdminModeracao.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api.ts", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly code: string,
      message: string,
      readonly details: Record<string, unknown> = {},
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
  terms_version: "2026-10-08",
  created_at: "2026-10-09T12:00:00Z",
};

const REVIEW: QueueItem = {
  id: 7,
  tipo: "comment_review",
  motivo: "hidden_by_reports",
  categoria: null,
  confianca: null,
  criado_em: "2026-10-13T12:00:00Z",
  apelido: "vizinho_bv",
  comentario: "vzn4p8qkrtmw",
  status_comentario: "hidden_reports",
  texto: "<b>Comentário denunciado</b>",
};

const BLOCKED: QueueItem = {
  id: 8,
  tipo: "strike_confirmation",
  motivo: "ai_low_confidence",
  categoria: "crime",
  confianca: 0.6,
  criado_em: "2026-10-13T12:05:00Z",
  apelido: "eleitor_rr",
  comentario: null,
  status_comentario: null,
  texto: "Texto bloqueado pela IA.",
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
        <AdminModeracao />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

function card(text: string): HTMLElement {
  return screen.getByText(text).closest("li")!;
}

beforeEach(() => {
  api.apiFetch.mockReset();
});
afterEach(cleanup);

describe("AdminModeracao", () => {
  it("quem não é admin não vê a fila nem chama a API", () => {
    renderPage({ ...ADMIN, is_admin: false });
    expect(screen.getByText(/Só a administração/)).toBeTruthy();
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it("mostra as ações de cada tipo de item, com o texto como texto", async () => {
    api.apiFetch.mockResolvedValue({ items: [REVIEW, BLOCKED] });
    renderPage();

    await screen.findByText("<b>Comentário denunciado</b>");
    expect(document.body.querySelector("b")).toBeNull();
    const review = within(card("<b>Comentário denunciado</b>"));
    expect(review.getByText("Oculto por denúncias")).toBeTruthy();
    expect(review.getByRole("button", { name: "Aprovar: item 7 de vizinho_bv" })).toBeTruthy();
    expect(review.getByRole("button", { name: "Remover: item 7 de vizinho_bv" })).toBeTruthy();
    expect(review.queryByRole("button", { name: /Descartar/ })).toBeNull();

    const blocked = within(card("Texto bloqueado pela IA."));
    expect(blocked.getByText(/crime · confiança 60%/)).toBeTruthy();
    expect(blocked.getByRole("button", { name: /^Publicar/ })).toBeTruthy();
    expect(blocked.getByRole("button", { name: /^Confirmar strike/ })).toBeTruthy();
    expect(blocked.getByRole("button", { name: /^Descartar/ })).toBeTruthy();
  });

  it("aprovar tira o item da fila", async () => {
    api.apiFetch.mockImplementation(async (_path: string, options: { method?: string } = {}) =>
      options.method === "POST" ? { status: "ok" } : { items: [REVIEW] },
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /^Aprovar/ }));

    expect(await screen.findByText("A fila está vazia.")).toBeTruthy();
    expect(document.activeElement?.textContent).toBe("Fila de moderação");
    expect(api.apiFetch).toHaveBeenCalledWith("/admin/moderacao/fila/7", {
      method: "POST",
      body: { acao: "aprovar" },
    });
  });

  it("banir pede confirmação", async () => {
    api.apiFetch.mockImplementation(async (_path: string, options: { method?: string } = {}) =>
      options.method === "POST" ? { status: "ok" } : { items: [REVIEW] },
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /^Banir vizinho_bv/ }));
    expect(api.apiFetch).toHaveBeenCalledTimes(1); // só a leitura da fila
    fireEvent.click(screen.getByRole("button", { name: "Sim, banir vizinho_bv" }));

    expect(await screen.findByText("A fila está vazia.")).toBeTruthy();
    expect(api.apiFetch).toHaveBeenLastCalledWith("/admin/moderacao/fila/7", {
      method: "POST",
      body: { acao: "banir" },
    });
  });

  it("item já resolvido (409) mostra o aviso e mantém a fila", async () => {
    api.apiFetch.mockImplementation(async (_path: string, options: { method?: string } = {}) => {
      if (options.method === "POST") {
        throw new ApiError(409, "acao_nao_concluida", "Não foi possível concluir. Recarregue a fila.");
      }
      return { items: [BLOCKED] };
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /^Publicar/ }));

    expect(await screen.findByText("Não foi possível concluir. Recarregue a fila.")).toBeTruthy();
    expect(screen.getByText("Texto bloqueado pela IA.")).toBeTruthy();
  });
});
