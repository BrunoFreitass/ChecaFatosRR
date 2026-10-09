import { defineConfig, devices } from "@playwright/test";

const PORT = 4173;

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
  projects: [{ name: "celular", use: { ...devices["Pixel 7"] } }],
  webServer: {
    command: `npm run dev -- --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    // Variáveis de ambiente valem mais que o .env.local: nunca usa as chaves reais.
    env: {
      VITE_API_URL: "http://127.0.0.1:9999",
      VITE_SUPABASE_URL: "https://e2e-ref.supabase.co",
      VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_e2e",
    },
  },
});
