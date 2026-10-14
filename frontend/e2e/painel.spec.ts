import { expect, test, type Page, type Route } from "@playwright/test";

import { painelFixture } from "../src/lib/painel.fixture.ts";

// Painel do admin (plano mapa-e-painel-admin §B): API simulada, build de produção com a CSP do
// Cloudflare Pages; roda no projeto "celular" e no "desktop".
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const STORAGE_KEY = "sb-e2e-ref-auth-token";

const PROFILE = {
  apelido: "admin_rr",
  status: "active",
  suspended_until: null,
  is_admin: true,
  terms_version: "2026-10-14",
  created_at: "2026-10-09T12:00:00Z",
};

const cspViolations = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }) => {
  const found: string[] = [];
  cspViolations.set(page, found);
  await page.exposeFunction("__reportCspViolation", (detail: string) => found.push(detail));
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      const report = (window as unknown as { __reportCspViolation: (d: string) => void })
        .__reportCspViolation;
      void report(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
});

test.afterEach(async ({ page }) => {
  expect(cspViolations.get(page) ?? [], "violações de CSP").toEqual([]);
});

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
};

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
}

/** API simulada; devolve a lista de caminhos pedidos (para conferir o que não foi chamado). */
async function mockApi(page: Page, isAdmin: boolean): Promise<string[]> {
  const asked: string[] = [];
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(route.request().url()).pathname;
    asked.push(path);
    if (path === "/me") return json(route, 200, { ...PROFILE, is_admin: isAdmin });
    if (path === "/admin/painel") {
      if (!isAdmin) return json(route, 403, { error: { code: "forbidden", message: "Sem permissão." } });
      return json(route, 200, painelFixture());
    }
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
  });
  return asked;
}

async function signIn(page: Page) {
  const session = {
    access_token: "token-e2e",
    token_type: "bearer",
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    refresh_token: "refresh-e2e",
    user: {
      id: "11111111-2222-4333-8444-555555555555",
      aud: "authenticated",
      role: "authenticated",
      email: "pessoa@example.com",
      app_metadata: { provider: "google" },
      user_metadata: {},
      created_at: "2026-10-09T12:00:00Z",
    },
  };
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, value),
    [STORAGE_KEY, JSON.stringify(session)] as const,
  );
}

function pageOverflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

test("o admin chega ao painel pela Minha conta e vê as métricas", async ({ page }) => {
  await signIn(page);
  await mockApi(page, true);
  await page.goto("/minha-conta");

  const chunk = page.waitForResponse((r) => /\/assets\/PainelAdmin-[^/]+\.js$/.test(r.url()));
  await page.getByRole("link", { name: "Painel do administrador" }).click();
  await chunk; // chunk separado: quem não é admin nunca baixa
  await expect(page).toHaveURL(/\/minha-conta\/painel$/);

  await expect(page.getByRole("heading", { name: "Painel do administrador" })).toBeVisible();
  const cota = page.getByRole("table", { name: /Cota do Gemini hoje/ });
  await expect(cota.getByRole("row", { name: /Checagem/ })).toContainText("964");
  await expect(page.getByRole("table", { name: /Checagens por dia/ }).getByRole("row")).toHaveCount(8);
  await expect(page.getByRole("table", { name: /Tokens por dia/ })).toContainText("25.200");

  const fila = page.getByRole("link", { name: /Fila de moderação/ });
  await expect(fila).toHaveAttribute("href", "/admin/moderacao");
  const analytics = page.getByRole("link", { name: /Cloudflare Web Analytics/ });
  await expect(analytics).toHaveAttribute("href", "https://dash.cloudflare.com/?to=/:account/web-analytics");
  await expect(analytics).toHaveAttribute("rel", "noopener noreferrer");
  expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
});

test("usuário comum não vê o link nem o painel, e a API do painel não é chamada", async ({ page }) => {
  await signIn(page);
  const asked = await mockApi(page, false);

  await page.goto("/minha-conta");
  await expect(page.getByText(/Apelido:/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Painel do administrador" })).toHaveCount(0);

  await page.goto("/minha-conta/painel");
  await expect(page.getByText("Só a administração do site pode ver esta página.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Painel do administrador" })).toHaveCount(0);
  expect(asked).not.toContain("/admin/painel");
});

test("sem login, o painel manda para a página de entrar", async ({ page }) => {
  await mockApi(page, false);
  await page.goto("/minha-conta/painel");
  await expect(page).toHaveURL(/\/entrar\?volta=%2Fminha-conta%2Fpainel$/);
});

test.describe("celular estreito", () => {
  test.skip(({ isMobile }) => !isMobile, "só no projeto de celular");

  test("360 px: tabelas largas rolam dentro da caixa, a página não rola para o lado", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await signIn(page);
    await mockApi(page, true);
    await page.goto("/minha-conta/painel");

    const region = page.getByRole("region", { name: /Checagens por dia/ });
    await expect(region).toBeVisible();
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    // A caixa rola sozinha e recebe foco pelo teclado.
    expect(await region.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    await region.focus();
    await expect(region).toBeFocused();
  });
});
