import { createClient } from "@supabase/supabase-js";

import { env } from "../env.ts";

/** Usado SÓ para o login. Os dados passam sempre pela nossa API (lib/api.ts). */
export const supabase = createClient(env.supabaseUrl, env.supabasePublishableKey, {
  auth: {
    flowType: "pkce",
    persistSession: true,
    autoRefreshToken: true,
    // A troca do ?code= é feita só em /auth/callback (auth/oauthCallback.ts), uma única vez
    // e com o motivo real da falha. No modo automático o erro era engolido.
    detectSessionInUrl: false,
  },
});
