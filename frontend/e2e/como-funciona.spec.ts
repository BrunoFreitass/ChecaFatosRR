import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

// Seção "Como o ChecaFatosRR funciona" na home (texto aprovado em 19/10/2026). Build de produção
// com a CSP do Cloudflare Pages, API simulada; projetos "celular" (Pixel 7 e 360 px) e "desktop".
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const KEY = "checafatos:a11y";
const CORS = { "access-control-allow-origin": "*" };

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
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path === "/tse/status" ? { banner: false, texto: "", apuracao: true } : { items: [], next_cursor: null };
    return route.fulfill({ status: 200, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
  });
});

test.afterEach(async ({ page }) => {
  expect(cspViolations.get(page) ?? [], "violações de CSP").toEqual([]);
});

const section = (page: Page) => page.getByRole("region", { name: "Como o ChecaFatosRR funciona" });

test("aparece logo abaixo do formulário, com lista ordenada de 5 passos e os limites", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await page.goto("/");

  const how = section(page);
  await expect(how).toBeVisible();
  await expect(how.getByRole("heading", { level: 2, name: "Como o ChecaFatosRR funciona" })).toBeVisible();
  await expect(how.locator("ol > li")).toHaveCount(5);
  await expect(how).toContainText("Limites:");

  // Abaixo do formulário (o botão de enviar fica antes da seção).
  const form = await page.locator("form").first().boundingBox();
  const box = await how.boundingBox();
  expect(form && box && box.y >= form.y + form.height).toBe(true);
  expect(errors, "erros no console").toEqual([]);
});

test("axe-core: sem violações sérias na seção, também com alto contraste", async ({ page }) => {
  for (const prefs of [{}, { contraste: true, fonte: 3 }]) {
    await page.goto("/");
    await page.evaluate(([key, value]) => localStorage.setItem(key, value), [KEY, JSON.stringify(prefs)] as const);
    await page.reload();
    await expect(section(page)).toBeVisible();
    const result = await new AxeBuilder({ page }).include("#como-funciona").analyze();
    const serious = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious.map((v) => `${JSON.stringify(prefs)} ${v.id}`)).toEqual([]);
  }
});

test.describe("celular", () => {
  test.skip(({ isMobile }) => !isMobile, "só no projeto de celular");

  for (const width of [360, 412]) {
    test(`${width} px: fonte máxima, mais espaço e alto contraste sem rolagem lateral`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await page.goto("/");
      await page.evaluate(
        ([key, value]) => localStorage.setItem(key, value),
        [KEY, JSON.stringify({ fonte: 3, espaco: true, contraste: true })] as const,
      );
      await page.reload();
      await expect(section(page)).toBeVisible();
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
      // Texto legível no alto contraste: branco sobre preto.
      const colors = await section(page).locator("li").first().evaluate((el) => {
        const style = getComputedStyle(el);
        return [style.color, style.backgroundColor];
      });
      expect(colors).toEqual(["rgb(255, 255, 255)", "rgb(0, 0, 0)"]);
    });
  }
});
