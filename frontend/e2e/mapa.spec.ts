import { expect, test, type Page, type Route } from "@playwright/test";

// Mapa por estado na Apuração (plano mapa-e-painel-admin): API simulada, build de produção com a
// CSP do Cloudflare Pages; roda no projeto "celular" e no "desktop".
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";

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

const UFS = [
  "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms", "mt", "pa",
  "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc", "se", "sp", "to",
];

function uf(a: number, b: number, secoes = 87.25) {
  return {
    candidatos: [
      { numero: "22", nome: "CANDIDATO AZUL", pct: a },
      { numero: "13", nome: "CANDIDATO VERMELHO", pct: b },
    ],
    secoes_pct: secoes,
    final: false,
    totalizado_em: "2026-10-25T19:30:00-03:00",
  };
}

// Acre sem dados; metade dos estados com cada um à frente.
const MAPA = {
  fonte: "TSE",
  link_oficial: "https://resultados.tse.jus.br/",
  eleicao: "6258",
  cores: { "13": "vermelho", "22": "azul" },
  estados: UFS.map((code, i) => ({
    uf: code,
    resultado: code === "ac" ? null : code === "rr" ? uf(60.5, 39.5) : i % 2 ? uf(45, 55) : uf(52, 48),
  })),
};

// Antes da apuração (hoje, com o 6258 ainda sem arquivo): nenhum estado com dados.
const MAPA_VAZIO = { ...MAPA, estados: UFS.map((code) => ({ uf: code, resultado: null })) };

const RESULT = {
  fonte: "TSE",
  link_oficial: "https://resultados.tse.jus.br/",
  escopos: [
    { escopo: "br", resultado: null },
    { escopo: "rr", resultado: null },
  ],
};

async function mockApi(page: Page, mapa: "on" | "off" | "empty") {
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/tse/resultado") return json(route, 200, RESULT);
    if (path === "/tse/mapa" && mapa === "on") return json(route, 200, MAPA);
    if (path === "/tse/mapa" && mapa === "empty") return json(route, 200, MAPA_VAZIO);
    if (path === "/tse/mapa") {
      return json(route, 404, { error: { code: "mapa_indisponivel", message: "Mapa indisponível." } });
    }
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
  });
}

/** Quanto o mapa (com a tabela, se aberta) passa da largura da tela; <= 0 = sem rolagem lateral. */
function mapOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const section = document.getElementById("mapa-titulo")?.closest("section");
    const items = section ? [section, ...section.querySelectorAll("*")] : [];
    const right = Math.max(0, ...items.map((el) => el.getBoundingClientRect().right));
    return right - document.documentElement.clientWidth;
  });
}

test("mapa carrega em chunk separado, colore os estados e mostra o detalhe ao tocar", async ({ page }) => {
  await mockApi(page, "on");
  const chunk = page.waitForResponse((r) => /\/assets\/MapaApuracao-[^/]+\.js$/.test(r.url()));
  await page.goto("/apuracao");
  await chunk;

  const mapa = page.getByRole("group", { name: /Mapa do Brasil/ });
  await expect(mapa).toBeVisible();
  await expect(mapa.locator("path[data-uf]")).toHaveCount(27);
  await expect(mapa.locator('path[data-uf="ac"]')).toHaveAttribute("fill", "#e2e8f0"); // sem dados
  await expect(mapa.locator('path[data-uf="rr"]')).toHaveAttribute("fill", "#2563eb"); // azul, 21 pontos

  await mapa.locator('path[data-uf="rr"]').click();
  const detalhe = page.getByRole("region", { name: /^Detalhe/ });
  await expect(detalhe).toContainText("Roraima");
  await expect(detalhe).toContainText("CANDIDATO AZUL: 60,50%");
  await expect(detalhe).toContainText("Urnas apuradas: 87,25%");

  // Teclado: o estado é um botão com nome; o foco mostra o detalhe.
  await mapa.getByRole("button", { name: "Amazonas" }).focus();
  await page.keyboard.press("Enter");
  await expect(detalhe).toContainText("Amazonas");

  const legenda = page.getByRole("list", { name: "Legenda do mapa" });
  await expect(legenda).toContainText("CANDIDATO VERMELHO à frente");
  await expect(legenda).toContainText("CANDIDATO AZUL à frente");
  await expect(page.getByText(/Natural Earth/)).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/eleit/i);

  expect(await mapOverflow(page)).toBeLessThanOrEqual(0);
});

test("tabela alternativa com os 27 estados", async ({ page }) => {
  await mockApi(page, "on");
  await page.goto("/apuracao");

  await page.getByText("Ver em tabela").click();
  const tabela = page.getByRole("table", { name: "Resultado por estado" });
  await expect(tabela).toBeVisible();
  await expect(tabela.getByRole("row")).toHaveCount(28); // cabeçalho + 27
  await expect(tabela.getByRole("row", { name: /Acre/ })).toContainText("Sem dados");
  await expect(tabela.getByRole("row", { name: /Roraima/ })).toContainText("CANDIDATO AZUL 60,50%");
  expect(await mapOverflow(page)).toBeLessThanOrEqual(0);
});

test("mapa desligado (404) não aparece e a apuração continua", async ({ page }) => {
  await mockApi(page, "off");
  const mapa = page.waitForResponse((r) => r.url().endsWith("/tse/mapa"));
  await page.goto("/apuracao");
  await mapa;

  await expect(page.getByText("Aguardando os primeiros resultados do TSE.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Mapa por estado" })).toHaveCount(0);
  await expect(page.getByRole("group", { name: /Mapa do Brasil/ })).toHaveCount(0);
});

// Ajustes pedidos na revisão dos prints (14/10): legenda de intensidade, rolagem até o detalhe e
// aviso antes da apuração — no celular, em 412 px (Pixel 7) e em 360 px.
test.describe("celular: Pixel 7 e 360 px", () => {
  test.skip(({ isMobile }) => !isMobile, "só no projeto de celular");

  /** Registra as chamadas de scrollIntoView (para conferir a animação). */
  async function spyScroll(page: Page) {
    await page.addInitScript(() => {
      const calls: unknown[] = [];
      (window as unknown as { __scrolls: unknown[] }).__scrolls = calls;
      const original = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function (options?: boolean | ScrollIntoViewOptions) {
        calls.push(options ?? null);
        original.call(this, options);
      };
    });
  }

  function lastScroll(page: Page) {
    return page.evaluate(() => (window as unknown as { __scrolls: unknown[] }).__scrolls.at(-1));
  }

  for (const width of [412, 360]) {
    test(`${width} px: legenda de intensidade com as 4 faixas inteiras, sem rolagem lateral`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await mockApi(page, "on");
      await page.goto("/apuracao");

      const faixas = page.getByRole("list", { name: /Cor mais forte/ });
      await expect(faixas.getByRole("listitem")).toHaveCount(4);
      for (const label of ["até 5 pontos", "5 a 15 pontos", "15 a 30 pontos", "mais de 30 pontos"]) {
        const item = faixas.getByRole("listitem").filter({ hasText: label });
        await expect(item).toBeVisible();
        await expect(item.locator("svg rect")).toHaveCount(2); // amostra vermelha e azul
        const box = await item.boundingBox();
        expect((box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? 0) <= width, `${label} cabe na tela`).toBe(true);
      }
      expect(await mapOverflow(page)).toBeLessThanOrEqual(0);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth),
      ).toBeLessThanOrEqual(0);
    });

    test(`${width} px: tocar num estado rola suave até o detalhe e põe o foco nele`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      await spyScroll(page);
      await mockApi(page, "on");
      await page.goto("/apuracao");
      const mapa = page.getByRole("group", { name: /Mapa do Brasil/ });
      await page.evaluate(() => window.scrollTo(0, 0));
      const antes = page.getByRole("region", { name: /^Detalhe/ });
      await expect(antes).not.toBeInViewport(); // o detalhe começa abaixo da dobra

      await mapa.locator('path[data-uf="rs"]').tap();

      const detalhe = page.getByRole("region", { name: /^Detalhe/ });
      await expect(detalhe).toContainText("Rio Grande do Sul");
      await expect(detalhe).toBeFocused();
      await expect(detalhe).toBeInViewport();
      expect(await lastScroll(page)).toMatchObject({ behavior: "smooth", block: "nearest" });
    });

    test(`${width} px: com movimento reduzido, vai até o detalhe sem animação`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await spyScroll(page);
      await mockApi(page, "on");
      await page.goto("/apuracao");
      const mapa = page.getByRole("group", { name: /Mapa do Brasil/ });
      await page.evaluate(() => window.scrollTo(0, 0));
      const antes = page.getByRole("region", { name: /^Detalhe/ });
      await expect(antes).not.toBeInViewport(); // o detalhe começa abaixo da dobra

      await mapa.locator('path[data-uf="rs"]').tap();

      const detalhe = page.getByRole("region", { name: /^Detalhe/ });
      await expect(detalhe).toBeFocused();
      await expect(detalhe).toBeInViewport();
      expect(await lastScroll(page)).toMatchObject({ behavior: "auto" });
    });

    test(`${width} px: antes da apuração, mapa cinza com aviso e legenda`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      const errors: string[] = [];
      page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
      await mockApi(page, "empty");
      await page.goto("/apuracao");

      await expect(page.getByText("O mapa será preenchido conforme o TSE divulgar os resultados.")).toBeVisible();
      const fills = await page
        .getByRole("group", { name: /Mapa do Brasil/ })
        .locator("path[data-uf]")
        .evaluateAll((paths) => [...new Set(paths.map((p) => p.getAttribute("fill")))]);
      expect(fills).toEqual(["#e2e8f0"]);
      await expect(page.getByRole("list", { name: "Legenda do mapa" })).toContainText("Sem dados");
      await expect(page.getByRole("list", { name: /Cor mais forte/ })).toBeVisible();
      expect(await mapOverflow(page)).toBeLessThanOrEqual(0);
      expect(errors, "erros no console").toEqual([]);
    });
  }
});
