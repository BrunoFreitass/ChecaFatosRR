import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  getSession: vi.fn(async () => ({ data: { session: { access_token: "token-de-teste" } } })),
}));
vi.mock("./supabase.ts", () => ({ supabase: { auth } }));
vi.mock("../env.ts", () => ({ env: { apiUrl: "https://api.example.com" } }));

const { apiFetch } = await import("./api.ts");

const fetchMock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(
  async () => new Response("{}", { status: 200 }),
);

beforeEach(() => {
  fetchMock.mockClear();
  auth.getSession.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function sentHeaders(): Record<string, string> {
  const init = fetchMock.mock.calls[0]?.[1];
  return (init?.headers ?? {}) as Record<string, string>;
}

describe("apiFetch", () => {
  it("manda o token da sessão por padrão", async () => {
    await apiFetch("/me");
    expect(sentHeaders().Authorization).toBe("Bearer token-de-teste");
  });

  it("consulta pública (anonymous) não manda o token nem lê a sessão", async () => {
    // Sem Authorization: requisição simples (sem preflight) e cacheável pelo navegador.
    await apiFetch("/tse/resultado", { anonymous: true });
    expect(sentHeaders().Authorization).toBeUndefined();
    expect(auth.getSession).not.toHaveBeenCalled();
  });
});
