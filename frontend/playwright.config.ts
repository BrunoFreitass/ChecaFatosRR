import { defineConfig, devices } from "@playwright/test";

// Porta própria (não a 4173 padrão do vite preview): nunca reaproveita um preview real.
const PORT = 4317;

// E2E sem backend nem Google: a API é simulada com page.route e a sessão do Supabase é
// injetada no localStorage (o login do Google não é automatizável).
export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
    locale: "pt-BR",
    timezoneId: "America/Boa_Vista",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "celular", use: { ...devices["Pixel 7"] } },
    // O mapa também é usado no computador (clique e teclado).
    { name: "desktop", use: { ...devices["Desktop Chrome"] }, testMatch: /mapa\.spec\.ts$/ },
  ],
  webServer: {
    // Build de PRODUÇÃO servido pelo vite preview, com a mesma CSP do Cloudflare Pages
    // (plugin em csp.ts): o servidor de dev injeta script inline e não serve para testar CSP.
    // Saída em dist-e2e: não sobrescreve o dist/ de um build real.
    command: `npx tsc -b && npx vite build --outDir dist-e2e --emptyOutDir && npx vite preview --outDir dist-e2e --port ${PORT} --strictPort`,
    timeout: 180_000,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false, // sempre o build de teste, com as URLs falsas abaixo
    // Variáveis de ambiente valem mais que o .env.local: nunca usa as chaves reais.
    env: {
      VITE_API_URL: "http://127.0.0.1:9999",
      VITE_SUPABASE_URL: "https://e2e-ref.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_e2e",
    },
  },
});
