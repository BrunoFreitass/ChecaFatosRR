import { createClient } from "@supabase/supabase-js";

import { env } from "../env.ts";

/** Usado SÓ para o login. Os dados passam sempre pela nossa API (lib/api.ts). */
export const supabase = createClient(env.supabaseUrl, env.supabasePublishableKey, {
  auth: {
    flowType: "pkce",
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
});
