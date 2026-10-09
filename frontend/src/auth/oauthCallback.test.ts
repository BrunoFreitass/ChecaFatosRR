import type { AuthError, Session } from "@supabase/supabase-js";
import { describe, expect, it, vi } from "vitest";

import { createCallbackCompleter, type CallbackAuth } from "./oauthCallback.ts";

const SESSION = { access_token: "token-falso" } as Session;
const CALLBACK = "http://localhost:5173/auth/callback";

function authError(name: string, code: string, status: number): AuthError {
  return { name, code, status, message: name } as AuthError;
}

/** Dublê do supabase.auth: o code só pode ser trocado uma vez, como no Supabase real. */
function fakeAuth(options: { verifier?: boolean; online?: boolean } = {}) {
  const { verifier = true, online = true } = options;
  let session: Session | null = null;
  const used = new Set<string>();

  const auth = {
    exchangeCodeForSession: vi.fn(async (code: string) => {
      await new Promise((resolve) => setTimeout(resolve, 5)); // latência da rede
      if (!online) throw new TypeError("Failed to fetch");
      if (!verifier) {
        return {
          data: { session: null },
          error: authError("AuthPKCECodeVerifierMissingError", "pkce_code_verifier_not_found", 400),
        };
      }
      if (used.has(code)) {
        return {
          data: { session: null },
          error: authError("AuthApiError", "flow_state_not_found", 404),
        };
      }
      used.add(code);
      session = SESSION;
      return { data: { session }, error: null };
    }),
    getSession: vi.fn(async () => ({ data: { session } })),
  } satisfies CallbackAuth;
  return auth;
}

describe("conclusão do login PKCE", () => {
  it("troca o code uma única vez com duas execuções simultâneas (StrictMode)", async () => {
    const auth = fakeAuth();
    const complete = createCallbackCompleter(auth);
    const url = new URL(`${CALLBACK}?code=abc`);

    const [first, second] = await Promise.all([complete(url), complete(url)]);

    expect(auth.exchangeCodeForSession).toHaveBeenCalledTimes(1);
    expect(first).toEqual({ ok: true });
    expect(second).toEqual({ ok: true });
  });

  it("troca o code uma única vez quando o componente remonta depois da troca", async () => {
    const auth = fakeAuth();
    const complete = createCallbackCompleter(auth);
    const url = new URL(`${CALLBACK}?code=abc`);

    expect(await complete(url)).toEqual({ ok: true });
    expect(await complete(url)).toEqual({ ok: true });
    expect(auth.exchangeCodeForSession).toHaveBeenCalledTimes(1);
  });

  it("aceita o code já usado por outra execução quando a sessão existe", async () => {
    const auth = fakeAuth();
    const url = new URL(`${CALLBACK}?code=abc`);
    // Duas instâncias independentes (ex.: recarga da página): a 2ª troca falha no servidor.
    await createCallbackCompleter(auth)(url);

    expect(await createCallbackCompleter(auth)(url)).toEqual({ ok: true });
    expect(auth.exchangeCodeForSession).toHaveBeenCalledTimes(2);
  });

  it("explica quando o code_verifier não está neste navegador", async () => {
    const result = await createCallbackCompleter(fakeAuth({ verifier: false }))(
      new URL(`${CALLBACK}?code=abc`),
    );

    expect(result).toMatchObject({ ok: false, reason: "verificador_ausente" });
  });

  it("recusa code já usado quando não há sessão", async () => {
    const auth = fakeAuth();
    await auth.exchangeCodeForSession("abc"); // consumido em outro navegador
    auth.getSession.mockResolvedValue({ data: { session: null } });

    const result = await createCallbackCompleter(auth)(new URL(`${CALLBACK}?code=abc`));

    expect(result).toMatchObject({
      ok: false,
      reason: "troca_recusada",
      detail: "AuthApiError · flow_state_not_found · 404",
    });
  });

  it("separa chave do Supabase inválida (401 Invalid API key) de code expirado", async () => {
    const auth = fakeAuth();
    auth.exchangeCodeForSession.mockResolvedValue({
      data: { session: null },
      error: { name: "AuthApiError", status: 401, message: "Invalid API key" } as AuthError,
    });

    const result = await createCallbackCompleter(auth)(new URL(`${CALLBACK}?code=abc`));

    expect(result).toMatchObject({ ok: false, reason: "configuracao", detail: "AuthApiError · 401" });
  });

  it("separa falha de rede", async () => {
    const result = await createCallbackCompleter(fakeAuth({ online: false }))(
      new URL(`${CALLBACK}?code=abc`),
    );

    expect(result).toMatchObject({ ok: false, reason: "rede" });
  });

  it("não troca nada quando o Google devolve erro", async () => {
    const auth = fakeAuth();
    const result = await createCallbackCompleter(auth)(
      new URL(`${CALLBACK}?error=access_denied&error_code=user_cancelled`),
    );

    expect(result).toEqual({ ok: false, reason: "cancelado", detail: "user_cancelled" });
    expect(auth.exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it("sem code: segue logado se já houver sessão, senão pede para tentar de novo", async () => {
    const auth = fakeAuth();
    const complete = createCallbackCompleter(auth);
    expect(await complete(new URL(CALLBACK))).toMatchObject({ ok: false, reason: "sem_codigo" });

    auth.getSession.mockResolvedValue({ data: { session: SESSION } });
    expect(await complete(new URL(CALLBACK))).toEqual({ ok: true });
  });
});
