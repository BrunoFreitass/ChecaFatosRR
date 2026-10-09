import type { AuthError, Session } from "@supabase/supabase-js";

/** Por que o retorno do Google não virou sessão. Cada motivo tem uma mensagem própria. */
export type CallbackFailure =
  | "cancelado"
  | "sem_codigo"
  | "verificador_ausente"
  | "troca_recusada"
  | "configuracao"
  | "rede";

export type CallbackResult =
  | { ok: true }
  | { ok: false; reason: CallbackFailure; detail: string | null };

/** O pedaço do `supabase.auth` usado aqui (facilita o teste com um dublê). */
export interface CallbackAuth {
  exchangeCodeForSession(
    code: string,
  ): Promise<{ data: { session: Session | null }; error: AuthError | null }>;
  getSession(): Promise<{ data: { session: Session | null } }>;
}

const OK: CallbackResult = { ok: true };

function failure(reason: CallbackFailure, detail: string | null = null): CallbackResult {
  return { ok: false, reason, detail };
}

function describe(error: AuthError | null): string | null {
  if (!error) return null;
  // Só nome, código e status: a mensagem do servidor nunca carrega token.
  return [error.name, error.code, error.status].filter(Boolean).join(" · ") || null;
}

async function exchange(auth: CallbackAuth, code: string): Promise<CallbackResult> {
  let error: AuthError | null;
  try {
    const result = await auth.exchangeCodeForSession(code);
    if (!result.error && result.data.session) return OK;
    error = result.error;
  } catch {
    return failure("rede");
  }

  // O code só vale uma vez. Se outra execução (outra aba, recarga da página) já o trocou,
  // a sessão salva por ela continua válida: não é erro.
  const { data } = await auth.getSession();
  if (data.session) return OK;

  if (error?.code === "pkce_code_verifier_not_found") {
    return failure("verificador_ausente", describe(error));
  }
  if (error?.name === "AuthRetryableFetchError" || error?.status === 0) {
    return failure("rede", describe(error));
  }
  // 401/403 na troca = o Supabase recusou o próprio site (ex.: "Invalid API key" com a
  // chave publicável errada no .env), não o usuário.
  if (error?.status === 401 || error?.status === 403) {
    return failure("configuracao", describe(error));
  }
  return failure("troca_recusada", describe(error));
}

/**
 * Cria a função que conclui o login PKCE. Chamadas repetidas com o mesmo `code` (o efeito
 * do React roda duas vezes no StrictMode; o componente pode remontar) recebem a MESMA
 * promessa: a troca no Supabase acontece uma única vez.
 */
export function createCallbackCompleter(auth: CallbackAuth) {
  const inflight = new Map<string, Promise<CallbackResult>>();

  return async function complete(url: URL): Promise<CallbackResult> {
    // O Google/Supabase devolve erros na query (?error=) ou no fragmento (#error=).
    const hash = new URLSearchParams(url.hash.slice(1));
    const params = url.searchParams;
    if (params.get("error") || params.get("error_description") || hash.get("error")) {
      return failure("cancelado", params.get("error_code") ?? hash.get("error_code"));
    }

    const code = params.get("code");
    if (!code) {
      // Recarga depois de um login concluído (o code já saiu da URL).
      const { data } = await auth.getSession();
      return data.session ? OK : failure("sem_codigo");
    }

    let pending = inflight.get(code);
    if (!pending) {
      pending = exchange(auth, code);
      inflight.set(code, pending);
    }
    return pending;
  };
}

export const CALLBACK_MESSAGES: Record<CallbackFailure, string> = {
  cancelado: "O login com o Google foi cancelado ou recusado.",
  sem_codigo: "O login expirou ou não foi concluído. Tente de novo.",
  verificador_ausente:
    "Este navegador não tem o início deste login. Isso acontece quando o login começa num " +
    "endereço do site e volta em outro (por exemplo, 127.0.0.1 e localhost), ou numa aba " +
    "anônima diferente. Abra o site sempre pelo mesmo endereço e tente de novo.",
  troca_recusada: "O código de login expirou ou já foi usado. Tente de novo.",
  configuracao:
    "O login está indisponível por um problema de configuração do site. Tente mais tarde; " +
    "se continuar, avise o suporte.",
  rede: "Sem conexão com o servidor de login. Verifique sua internet e tente de novo.",
};
