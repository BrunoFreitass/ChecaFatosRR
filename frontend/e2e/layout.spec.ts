import { expect, test, type Page, type Route } from "@playwright/test";

// Nenhuma página rola para o lado no celular (bug do menu: +62 px no Pixel 7 deslogado).
// Pixel 7 (412 px) e 360 px (celulares menores): deslogado, sem apelido ("Escolher apelido") e
// logado com o apelido mais largo possível (20 caracteres, regra de app/services/nicknames.py).
// Build de produção com a CSP. Cada página só é medida depois que o menu do usuário aparece.
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const STORAGE_KEY = "sb-e2e-ref-auth-token";
const WIDEST_NICKNAME = "WWWWWWWWWW_WWWWWWWWW";
const SLUG = "7k2m9p4q8r1s";

const PROFILE = {
  apelido: WIDEST_NICKNAME,
  status: "active",
  suspended_until: null,
  is_admin: true, // abre também /admin/moderacao
  terms_version: "2026-10-14",
  created_at: "2026-10-09T12:00:00Z",
};

// Checagem com texto longo sem espaço (link e trecho colado): o caso mais provável de estourar.
const LONG = "https://www.exemplo-de-portal-de-noticias-com-nome-comprido.com.br/eleicoes/2026/roraima/".concat(
  "votacao-do-segundo-turno-foi-adiada-segundo-mensagem-que-circula-nos-grupos-de-whatsapp",
);
const CHECK = {
  id: SLUG,
  tipo: "link",
  url: LONG,
  domain: "exemplo-de-portal-de-noticias-com-nome-comprido.com.br",
  title: "Votação do 2º turno em Roraima é adiada ".repeat(3).trim(),
  published_at: "2026-10-20",
  texto: null,
  veredito: "falso",
  rebaixado_por_confianca: false,
  confianca: 92,
  resumo: `A votação não foi adiada. Fonte: ${LONG}`,
  pontos_suspeitos: [`Link estranho: ${LONG}`],
  contexto_faltante: null,
  checagens_agencias: [
    { agencia: "Aos Fatos", titulo: "É falso que a votação em RR foi adiada", veredito: "Falso", url: LONG, data: "2026-10-19" },
  ],
  fontes_sugeridas: ["Site oficial do TSE"],
  created_at: "2026-10-20T15:30:00Z",
  cache: false,
};

const PUBLIC_PAGES = [
  "/",
  "/apuracao",
  "/ultimas",
  "/comunidade",
  `/checagem/${SLUG}`,
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

async function mockApi(page: Page, me: "profile" | "no_profile") {
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(route.request().url()).pathname;
    if (path === "/me" && me === "profile") return json(route, 200, PROFILE);
    if (path === "/me") return json(route, 404, { error: { code: "profile_not_found", message: "Sem perfil." } });
    if (path === `/checks/${SLUG}`) return json(route, 200, CHECK);
    if (path === "/admin/moderacao/fila") return json(route, 200, { items: [] });
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
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
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

/**
 * Abre cada página nas duas larguras e mede só depois que o menu inteiro (com o item do usuário)
 * apareceu e a rede parou: medir antes do /me responder não testaria o caso que quebrava.
 */
async function checkAllPages(page: Page, paths: string[], userLink: string | RegExp) {
  const wide: string[] = [];
  const nav = page.getByRole("navigation", { name: "Principal" });
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 800 });
    for (const path of paths) {
      await page.goto(path);
      for (const name of ["Apuração", "Últimas", "Comunidade"]) {
        await expect(nav.getByRole("link", { name, exact: true })).toBeVisible();
      }
      await expect(nav.getByRole("link", { name: userLink })).toBeVisible();
      await page.waitForLoadState("networkidle");
      const extraPx = await overflow(page);
      if (extraPx > 0) wide.push(`${path} @${width}px: +${extraPx}px`);
    }
  }
  expect(wide, "páginas com rolagem lateral").toEqual([]);
}

test.describe("sem rolagem lateral no celular", () => {
  test.skip(({ isMobile }) => !isMobile, "só no projeto de celular");

  test("deslogado: nenhuma página rola para o lado", async ({ page }) => {
    await mockApi(page, "no_profile");
    await checkAllPages(page, [...PUBLIC_PAGES, "/minha-conta"], "Entrar");
  });

  test("logado sem apelido (Escolher apelido): nenhuma página rola para o lado", async ({ page }) => {
    await signIn(page);
    await mockApi(page, "no_profile");
    await checkAllPages(page, [...PUBLIC_PAGES, "/bem-vindo"], "Escolher apelido");
  });

  test("logado com o apelido mais largo: nenhuma página rola e o apelido é cortado", async ({ page }) => {
    await signIn(page);
    await mockApi(page, "profile");
    await checkAllPages(page, [...PUBLIC_PAGES, "/minha-conta", "/admin/moderacao"], /Minha conta/);

    const account = page.getByRole("navigation", { name: "Principal" }).getByRole("link", { name: /Minha conta/ });
    await expect(account).toHaveAccessibleName(`Minha conta (${WIDEST_NICKNAME})`); // nome inteiro
    const box = await account.boundingBox();
    expect(box?.width ?? Infinity).toBeLessThanOrEqual(160); // max-w-40: cortado com "…"
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44); // alvo de toque
  });

  test("no computador o cabeçalho continua numa linha só", async ({ page }) => {
    await mockApi(page, "no_profile");
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Principal" });
    await expect(nav.getByRole("link", { name: "Entrar" })).toBeVisible();

    const logo = await page.locator("header a[href=\"/\"]").first().boundingBox();
    const menu = await nav.boundingBox();
    expect(Math.abs((logo?.y ?? 0) + (logo?.height ?? 0) / 2 - ((menu?.y ?? 0) + (menu?.height ?? 0) / 2))).toBeLessThan(4);
  });
});
