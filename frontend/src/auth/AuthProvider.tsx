import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, apiFetch } from "../lib/api.ts";
import { saveReturnPath } from "../lib/returnPath.ts";
import { supabase } from "../lib/supabase.ts";
import type { Profile } from "../lib/types.ts";
import { AuthContext, type AuthContextValue, type AuthStatus } from "./AuthContext.ts";

interface State {
  status: AuthStatus;
  profile: Profile | null;
  termsVersion: string | null;
  errorMessage: string | null;
}

/** Último resultado de GET /me, sempre associado ao usuário a que pertence. */
interface Loaded {
  userId: string;
  state: State;
}

const LOADING: State = { status: "loading", profile: null, termsVersion: null, errorMessage: null };
const ANONYMOUS: State = { status: "anonymous", profile: null, termsVersion: null, errorMessage: null };

function ready(profile: Profile): State {
  return { status: "ready", profile, termsVersion: null, errorMessage: null };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  // undefined = ainda lendo a sessão salva; null = sem sessão.
  const [userId, setUserId] = useState<string | null | undefined>(undefined);
  // Incrementar dispara uma nova busca do perfil (o estado anterior continua visível).
  const [round, setRound] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    // Só guarda o id: chamar outras funções do supabase dentro do callback pode travar.
    const { data } = supabase.auth.onAuthStateChange((_event, session) => {
      setUserId(session?.user.id ?? null);
    });
    return () => data.subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (!userId) return;
    const controller = new AbortController();
    const settle = (state: State) => {
      if (!controller.signal.aborted) setLoaded({ userId, state });
    };
    // A chave userId descarta respostas de uma sessão anterior (troca rápida de conta).
    apiFetch<Profile>("/me", { signal: controller.signal })
      .then((profile) => settle(ready(profile)))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.code === "profile_not_found") {
          const version = error.details.terms_version;
          settle({
            status: "needs_profile",
            profile: null,
            termsVersion: typeof version === "string" ? version : null,
            errorMessage: null,
          });
          return;
        }
        if (error instanceof ApiError && error.status === 401) {
          // Sessão recusada pelo servidor: trata como anônimo mesmo se o signOut falhar.
          settle(ANONYMOUS);
          void supabase.auth.signOut({ scope: "local" }).catch(() => undefined);
          return;
        }
        settle({
          status: "error",
          profile: null,
          termsVersion: null,
          errorMessage:
            error instanceof ApiError ? error.message : "Não foi possível carregar sua conta.",
        });
      });
    return () => controller.abort();
  }, [userId, round]);

  let state: State = LOADING;
  if (userId === null) state = ANONYMOUS;
  else if (userId && loaded?.userId === userId) state = loaded.state;

  const signInWithGoogle = useCallback(async (returnPath: string) => {
    saveReturnPath(returnPath);
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback` },
    });
    if (error) throw new ApiError(0, "oauth", "Não foi possível abrir o login do Google.");
  }, []);

  const signOut = useCallback(async () => {
    try {
      await supabase.auth.signOut();
    } catch {
      // Sem rede: ao menos limpa a sessão deste navegador.
      await supabase.auth.signOut({ scope: "local" }).catch(() => undefined);
    }
  }, []);

  const setProfile = useCallback(
    (profile: Profile) => {
      if (userId) setLoaded({ userId, state: ready(profile) });
    },
    [userId],
  );

  const reloadProfile = useCallback(() => setRound((value) => value + 1), []);

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, signInWithGoogle, signOut, setProfile, reloadProfile }),
    [state, signInWithGoogle, signOut, setProfile, reloadProfile],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}
