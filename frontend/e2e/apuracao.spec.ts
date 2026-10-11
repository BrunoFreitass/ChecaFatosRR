import { expect, test, type Page, type Route } from "@playwright/test";

// Apuração do TSE (etapa 5): API simulada, build de produção com a CSP do Cloudflare Pages.
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const BANNER = "Acompanhe a apuração oficial do 2º turno — fonte: TSE";

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

const CORS = { "access-control-allow-origin": "*" };

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
}

function scope(escopo: string, names: [string, string]) {
  return {
    escopo,
    eleicao: "6258",
    turno: 2,
    cargo: "Presidente",
    final: false,
    totalizado_em: "2026-10-25T19:30:00-03:00",
    secoes_pct: 45.3,
    eleitores: 1000,
    validos: 900,
    brancos: 10,
    brancos_pct: 1.1,
    nulos: 20,
    nulos_pct: 2.2,
    abstencao: 70,
    abstencao_pct: 7,
    candidatos: [
      { numero: "22", nome: names[0], partido: "PA", vice: "VICE A", votos: 500, pct: 55.56, situacao: "", eleito: false },
      { numero: "13", nome: names[1], partido: "PB", vice: "VICE B", votos: 400, pct: 44.44, situacao: "", eleito: false },
    ],
  };
}

const RESULT = {
  fonte: "TSE",
  link_oficial: "https://resultados.tse.jus.br/",
  escopos: [
    { escopo: "br", resultado: scope("br", ["CANDIDATO BRASIL A", "CANDIDATO BRASIL B"]) },
    { escopo: "rr", resultado: scope("rr", ["CANDIDATO RR A", "CANDIDATO RR B"]) },
  ],
};

async function mockApi(page: Page, banner: boolean) {
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/tse/resultado") return json(route, 200, RESULT);
    if (path === "/tse/status") return json(route, 200, { banner, texto: BANNER, apuracao: true });
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
  });
}

test("apuração mostra o resultado oficial do Brasil e de Roraima", async ({ page }) => {
  await mockApi(page, false);
  await page.goto("/apuracao");

  await expect(page.getByRole("heading", { level: 1 })).toContainText("Apuração");
  await expect(page.getByText("CANDIDATO BRASIL A")).toBeVisible();
  await expect(page.getByText("45,30% das seções totalizadas", { exact: true })).toBeVisible();
  await expect(page.getByText(/Fonte: TSE/)).toBeVisible();

  await page.getByRole("tab", { name: "Roraima" }).click();
  await expect(page.getByText("CANDIDATO RR A")).toBeVisible();
  await expect(page.getByText("CANDIDATO BRASIL A")).toHaveCount(0);
});

test("banner na home leva à apuração no dia", async ({ page }) => {
  await mockApi(page, true);
  await page.goto("/");

  await page.getByRole("link", { name: BANNER }).click();
  await expect(page).toHaveURL(/\/apuracao$/);
  await expect(page.getByText("CANDIDATO BRASIL A")).toBeVisible();
});

test("fora da janela, a home não mostra o banner", async ({ page }) => {
  await mockApi(page, false);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: BANNER })).toHaveCount(0);
});

test("o menu tem o link da apuração", async ({ page }) => {
  await mockApi(page, false);
  await page.goto("/");
  await page.getByRole("navigation", { name: "Principal" }).getByRole("link", { name: "Apuração" }).click();
  await expect(page).toHaveURL(/\/apuracao$/);
});

test("TSE fora: mantém os últimos números e avisa", async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/tse/resultado") {
      calls += 1;
      if (calls === 1) return json(route, 200, RESULT);
      return json(route, 503, { error: { code: "erro", message: "fora" } });
    }
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
  });
  await page.goto("/apuracao");
  await expect(page.getByText("CANDIDATO BRASIL A")).toBeVisible();

  await page.clock.runFor(61_000); // próxima consulta (a cada 60 s) falha

  await expect(page.getByText(/mostrando a última atualização/)).toBeVisible();
  await expect(page.getByText("CANDIDATO BRASIL A")).toBeVisible();
});

test("apuração desligada no painel mostra o aviso e o link oficial", async ({ page }) => {
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, (route) =>
    json(route, 404, { error: { code: "apuracao_indisponivel", message: "A apuração ainda não está disponível." } }),
  );
  await page.goto("/apuracao");
  await expect(page.getByText("A apuração ainda não está disponível.")).toBeVisible();
  await expect(page.getByRole("link", { name: /resultados\.tse\.jus\.br/ })).toBeVisible();
});
