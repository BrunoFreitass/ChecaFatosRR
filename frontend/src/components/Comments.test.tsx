// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/AuthContext.ts";
import type { Comment } from "../lib/comments.ts";
import type { Profile } from "../lib/types.ts";
import { Comments } from "./Comments.tsx";

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

const PROFILE: Profile = {
  apelido: "eleitor_rr",
  status: "active",
  suspended_until: null,
  is_admin: false,
  terms_version: "2026-10-08",
  created_at: "2026-10-09T12:00:00Z",
};

function comment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: "vzn4p8qkrtmw",
    apelido: "vizinho_bv",
    texto: "A seção eleitoral mudou de endereço?",
    criado_em: "2026-10-13T12:00:00Z",
    removido_pelo_autor: false,
    respostas: [],
    ...overrides,
  };
}

function auth(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  return {
    status: "ready",
    profile: PROFILE,
    errorMessage: null,
    signInWithGoogle: vi.fn(),
    signOut: vi.fn(),
    setProfile: vi.fn(),
    reloadProfile: vi.fn(),
    ...overrides,
  };
}

function renderComments(value: AuthContextValue, checagem: string | null = "k7mqp9xwvrzt") {
  render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={["/checagem/k7mqp9xwvrzt"]}>
        <Comments checagem={checagem} title="Comentários" />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

/** GET devolve `items`; os outros métodos chamam `other`. */
function routeApi(items: Comment[], other: (path: string, body: unknown) => unknown = () => undefined) {
  api.apiFetch.mockImplementation(async (path: string, options: { method?: string; body?: unknown } = {}) => {
    if (!options.method || options.method === "GET") return { items, next_cursor: null };
    return other(path, options.body);
  });
}

beforeEach(() => {
  api.apiFetch.mockReset();
});
afterEach(cleanup);

describe("Comments", () => {
  it("sem login: lista os comentários e convida a entrar, sem ações", async () => {
    routeApi([comment()]);
    renderComments(auth({ status: "anonymous", profile: null }));

    expect(await screen.findByText("A seção eleitoral mudou de endereço?")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Entre com sua conta Google" }).getAttribute("href")).toBe(
      "/entrar?volta=%2Fchecagem%2Fk7mqp9xwvrzt",
    );
    expect(screen.queryByRole("button", { name: /Responder/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Denunciar/ })).toBeNull();
    // Leitura pública: sem token.
    expect(api.apiFetch.mock.calls[0]?.[0]).toBe("/comments?checagem=k7mqp9xwvrzt");
    expect(api.apiFetch.mock.calls[0]?.[1]).toMatchObject({ anonymous: true });
  });

  it("o texto do comentário nunca vira HTML", async () => {
    const payload = '<img src=x onerror="alert(1)"> <a href="javascript:alert(1)">clique</a>';
    routeApi([comment({ texto: payload })]);
    renderComments(auth());

    expect(await screen.findByText(payload)).toBeTruthy();
    expect(document.body.querySelector("img")).toBeNull();
    expect(document.body.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("publica um comentário e mostra no topo", async () => {
    const created = comment({ id: "z9y8x7w6v5t4", apelido: PROFILE.apelido, texto: "Mudou sim." });
    const sent: unknown[] = [];
    routeApi([comment()], (_path, body) => {
      sent.push(body);
      return created;
    });
    renderComments(auth());

    fireEvent.change(await screen.findByLabelText("Escreva um comentário"), {
      target: { value: "  Mudou sim.  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Comentar" }));

    await screen.findByText("Mudou sim.");
    expect(sent).toEqual([{ texto: "Mudou sim.", checagem: "k7mqp9xwvrzt" }]);
    const items = screen.getAllByRole("article");
    expect(items[0]?.textContent).toContain("Mudou sim.");
  });

  it("comentário bloqueado mostra o aviso de violação e as advertências", async () => {
    routeApi([], () => {
      throw new ApiError(422, "violacao", "Seu comentário não foi publicado: discurso de ódio.", {
        regra: "discurso de ódio",
        explicacao: "Ataques a grupos não são permitidos.",
        strikes: 2,
        status: "active",
        suspenso_ate: null,
      });
    });
    renderComments(auth());

    fireEvent.change(await screen.findByLabelText("Escreva um comentário"), {
      target: { value: "texto bloqueado" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Comentar" }));

    const alert = (await screen.findByText(/Aviso de violação/)).closest("[role=alert]")!;
    expect(alert.textContent).toContain("Aviso de violação: discurso de ódio");
    expect(alert.textContent).toContain("Ataques a grupos não são permitidos.");
    expect(screen.getByText(/Advertências ativas: 2/)).toBeTruthy();
  });

  it("violação que suspende a conta recarrega o perfil", async () => {
    const value = auth();
    routeApi([], () => {
      throw new ApiError(422, "violacao", "Bloqueado.", {
        regra: "ameaça",
        explicacao: "Ameaças não são permitidas.",
        strikes: 3,
        status: "suspended",
        suspenso_ate: "2026-10-20T12:00:00Z",
      });
    });
    renderComments(value);

    fireEvent.change(await screen.findByLabelText("Escreva um comentário"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Comentar" }));

    expect(await screen.findByText(/Sua conta está suspensa até/)).toBeTruthy();
    expect(value.reloadProfile).toHaveBeenCalled();
  });

  it("o autor apaga o próprio comentário e as respostas ficam", async () => {
    const reply = comment({ id: "r1r2r3r4r5r6", texto: "Resposta que fica." });
    routeApi([comment({ apelido: PROFILE.apelido, respostas: [reply] })]);
    renderComments(auth());

    fireEvent.click(await screen.findByRole("button", { name: "Apagar meu comentário" }));
    await waitFor(() => expect(document.activeElement?.textContent).toBe("Sim, apagar"));
    fireEvent.click(screen.getByRole("button", { name: "Sim, apagar" }));

    expect(await screen.findByText("Comentário removido pelo autor")).toBeTruthy();
    expect(screen.queryByText("A seção eleitoral mudou de endereço?")).toBeNull();
    expect(screen.getByText("Resposta que fica.")).toBeTruthy();
    const call = api.apiFetch.mock.calls.find((c) => (c[1] as { method?: string })?.method === "DELETE");
    expect(call?.[0]).toBe("/comments/vzn4p8qkrtmw");
  });

  it("denuncia o comentário de outra pessoa (o próprio não tem o botão)", async () => {
    routeApi([comment()], () => ({ status: "recebida" }));
    renderComments(auth());

    fireEvent.click(await screen.findByRole("button", { name: "Denunciar comentário de vizinho_bv" }));

    expect(await screen.findByText("Denúncia recebida. Obrigado!")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Apagar/ })).toBeNull();
  });

  it("responde a um comentário principal (respostas não têm Responder)", async () => {
    const reply = comment({ id: "r1r2r3r4r5r6", texto: "Já existe resposta." });
    const created = comment({ id: "n1n2n3n4n5n6", apelido: PROFILE.apelido, texto: "Minha resposta." });
    const sent: unknown[] = [];
    routeApi([comment({ respostas: [reply] })], (_path, body) => {
      sent.push(body);
      return created;
    });
    renderComments(auth());

    const buttons = await screen.findAllByRole("button", { name: /^Responder a/ });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]!);
    fireEvent.change(screen.getByLabelText("Responder a vizinho_bv"), { target: { value: "Minha resposta." } });
    fireEvent.click(screen.getByRole("button", { name: "Responder" }));

    await screen.findByText("Minha resposta.");
    expect(sent).toEqual([{ texto: "Minha resposta.", checagem: "k7mqp9xwvrzt", resposta_a: "vzn4p8qkrtmw" }]);
  });

  it("conta suspensa lê, mas não vê o formulário", async () => {
    routeApi([comment()]);
    renderComments(auth({ profile: { ...PROFILE, status: "suspended", suspended_until: "2026-10-20T12:00:00Z" } }));

    expect(await screen.findByText(/Sua conta está suspensa até/)).toBeTruthy();
    expect(screen.queryByLabelText("Escreva um comentário")).toBeNull();
    expect(screen.queryByRole("button", { name: /Denunciar/ })).toBeNull();
  });

  it("feed da Comunidade: sem checagem na consulta nem no envio", async () => {
    routeApi([]);
    renderComments(auth(), null);

    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(api.apiFetch.mock.calls[0]?.[0]).toBe("/comments");
    expect(await screen.findByText("Ainda não há comentários.")).toBeTruthy();
  });
});

describe("Comments: suspensão pelo próprio comentário", () => {
  it("o aviso de violação continua na tela quando o perfil passa a suspenso", async () => {
    function Harness() {
      const [profile, setProfile] = useState<Profile>(PROFILE);
      const value = auth({
        profile,
        reloadProfile: () => setProfile({ ...PROFILE, status: "suspended", suspended_until: "2026-10-20T12:00:00Z" }),
      });
      return (
        <AuthContext.Provider value={value}>
          <MemoryRouter>
            <Comments checagem={null} title="Comentários" />
          </MemoryRouter>
        </AuthContext.Provider>
      );
    }
    routeApi([], () => {
      throw new ApiError(422, "violacao", "Bloqueado.", {
        regra: "ameaça",
        explicacao: "Ameaças não são permitidas.",
        strikes: 3,
        status: "suspended",
        suspenso_ate: "2026-10-20T12:00:00Z",
      });
    });
    render(<Harness />);

    fireEvent.change(await screen.findByLabelText("Escreva um comentário"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Comentar" }));

    expect(await screen.findByText("Aviso de violação: ameaça")).toBeTruthy();
    expect(screen.getByText("Ameaças não são permitidas.")).toBeTruthy();
    expect(screen.getByText(/Sua conta está suspensa até/)).toBeTruthy();
    expect(screen.queryByLabelText("Escreva um comentário")).toBeNull();
  });

  it("publicar antes de a lista chegar não some com o comentário", async () => {
    let resolveList: (value: unknown) => void = () => undefined;
    const created = comment({ id: "z9y8x7w6v5t4", apelido: PROFILE.apelido, texto: "Cheguei antes." });
    api.apiFetch.mockImplementation((_path: string, options: { method?: string } = {}) =>
      options.method === "POST"
        ? Promise.resolve(created)
        : new Promise((resolve) => {
            resolveList = resolve;
          }),
    );
    renderComments(auth());

    fireEvent.change(screen.getByLabelText("Escreva um comentário"), { target: { value: "Cheguei antes." } });
    fireEvent.click(screen.getByRole("button", { name: "Comentar" }));
    await screen.findByText("Cheguei antes.");
    resolveList({ items: [comment()], next_cursor: null });

    expect(await screen.findByText("A seção eleitoral mudou de endereço?")).toBeTruthy();
    expect(screen.getByText("Cheguei antes.")).toBeTruthy();
  });
});
