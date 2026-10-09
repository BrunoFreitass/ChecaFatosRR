function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`Variável ${name} não definida. Veja frontend/.env.example.`);
  }
  return value.replace(/\/+$/, "");
}

/** Configuração pública do frontend, validada uma única vez. */
export const env = {
  apiUrl: required("VITE_API_URL", import.meta.env.VITE_API_URL),
  supabaseUrl: required("VITE_SUPABASE_URL", import.meta.env.VITE_SUPABASE_URL),
  supabasePublishableKey: required(
    "VITE_SUPABASE_PUBLISHABLE_KEY",
    import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY,
  ),
} as const;
