function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`Variável ${name} não definida. Veja frontend/.env.example.`);
  }
  return value.replace(/\/+$/, "");
}

/** Configuração pública do frontend, validada uma única vez. */
export const env = {
  apiUrl: required("VITE_API_URL", import.meta.env.VITE_API_URL),
} as const;
