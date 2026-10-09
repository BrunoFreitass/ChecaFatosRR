import { createContext, useContext } from "react";

import type { Profile } from "../lib/types.ts";

export type AuthStatus = "loading" | "anonymous" | "needs_profile" | "ready" | "error";

export interface AuthContextValue {
  status: AuthStatus;
  profile: Profile | null;
  /** Versão vigente dos Termos, informada pela API quando falta o perfil. */
  termsVersion: string | null;
  errorMessage: string | null;
  signInWithGoogle: (returnPath: string) => Promise<void>;
  signOut: () => Promise<void>;
  setProfile: (profile: Profile) => void;
  reloadProfile: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth precisa estar dentro de <AuthProvider>");
  return value;
}
