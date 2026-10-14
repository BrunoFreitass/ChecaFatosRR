import { expect, test, type Page, type Route } from "@playwright/test";

// Nenhuma página rola para o lado no celular (bug do menu: +62 px no Pixel 7 deslogado).
// Pixel 7 (412 px) e 360 px (celulares menores), deslogado e logado com o apelido mais largo
// possível (20 caracteres, regra de app/services/nicknames.py). Build de produção com a CSP.
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const STORAGE_KEY = "sb-e2e-ref-auth-token";
const WIDEST_NICKNAME = "WWWWWWWWWW_WWWWWWWWW";

const PROFILE = {
  apelido: WIDEST_NICKNAME,
  status: "active",
  suspended_until: null,
  is_admin: false,
  terms_version: "2026-10-14",
  created_at: "2026-10-09T12:00:00Z",
};

const PAGES = [
  "/",
  "/apuracao",
  "/ultimas",
  "/comunidade",
  "/termos",
  "/privacidade",
  "/entrar",
  "/nao-existe",
];
const WIDTHS = [412, 360];

const cspViolations = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }) => {
  const found: string[] = [];
  cspViolations.set(page, found);
  await page.exposeFunction("__reportCspViolation", (detail: string) =>
    found.push(detail),
  );
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      const report = (
        window as unknown as { __reportCspViolation: (d: string) => void }
      ).__reportCspViolation;
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
  return route.fulfill({
    status,
    headers: CORS,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockApi(page: Page) {
  await page.route(`${SUPABASE}/**`, (route) =>
    route.fulfill({ status: 200, body: "{}" }),
  );
  await page.route(`${API}/**`, async (route) => {
    if (route.request().method() === "OPTIONS")
      return route.fulfill({ status: 204, headers: CORS });
    if (new URL(route.request().url()).pathname === "/me")
      return json(route, 200, PROFILE);
    return json(route, 404, {
      error: { code: "not_found", message: "Não encontrado." },
    });
  });
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

/** Quanto a página passa da largura da tela (<= 0: sem rolagem lateral). */
function overflow(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
}

async function checkAllPages(page: Page, extra: string[] = []) {
  const wide: string[] = [];
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 800 });
    for (const path of [...PAGES, ...extra]) {
      await page.goto(path);
      await expect(
        page.getByRole("navigation", { name: "Principal" }),
      ).toBeVisible();
      const extraPx = await overflow(page);
      if (extraPx > 0) wide.push(`${path} @${width}px: +${extraPx}px`);
    }
  }
  expect(wide, "páginas com rolagem lateral").toEqual([]);
}

test.describe("sem rolagem lateral no celular", () => {
  test.skip(({ isMobile }) => !isMobile, "só no projeto de celular");

  test("deslogado: menu inteiro visível e nenhuma página rola para o lado", async ({
    page,
  }) => {
    await mockApi(page);
    await checkAllPages(page);
    // Todos os itens do menu continuam lá (quebram linha, não somem).
    const nav = page.getByRole("navigation", { name: "Principal" });
    for (const name of ["Apuração", "Últimas", "Comunidade", "Entrar"]) {
      await expect(nav.getByRole("link", { name })).toBeVisible();
    }
  });

  test("logado com o apelido mais largo: nenhuma página rola para o lado", async ({
    page,
  }) => {
    await signIn(page);
    await mockApi(page);
    await checkAllPages(page, ["/minha-conta"]);
    await expect(
      page
        .getByRole("navigation", { name: "Principal" })
        .getByRole("link", { name: /Minha conta/ }),
    ).toBeVisible();
  });
});
