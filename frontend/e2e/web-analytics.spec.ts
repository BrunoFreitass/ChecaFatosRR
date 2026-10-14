import { expect, test, type Page } from "@playwright/test";

// Cloudflare Web Analytics (14/10/2026): o beacon carrega e envia sem violar a CSP de produção.
// Sem rede: o script e o envio são simulados; o que se testa é a CSP do build.
const BEACON = "https://static.cloudflareinsights.com/beacon.min.js";
const RUM = "https://cloudflareinsights.com/cdn-cgi/rum";

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
  await page.route("http://127.0.0.1:9999/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
  );
});

test.afterEach(async ({ page }) => {
  expect(cspViolations.get(page) ?? [], "violações de CSP").toEqual([]);
});

test("o beacon do Web Analytics carrega e envia sem violar a CSP", async ({ page }) => {
  let sent = false;
  await page.route(BEACON, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `fetch(${JSON.stringify(RUM)}, { method: "POST", body: "{}", mode: "no-cors" })
        .then(() => { window.__beaconSent = true; });`,
    }),
  );
  await page.route(`${RUM}**`, (route) => {
    sent = true;
    return route.fulfill({ status: 204 });
  });

  const response = await page.goto("/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self' https://static.cloudflareinsights.com");
  expect(csp).toContain("https://cloudflareinsights.com");

  await page.addScriptTag({ url: BEACON });
  await page.waitForFunction(() => (window as unknown as { __beaconSent?: boolean }).__beaconSent);
  expect(sent).toBe(true);
});

test("script de outro host continua bloqueado pela CSP", async ({ page }) => {
  // Sem este caso, um script-src * também passaria no teste acima.
  await page.route("https://outro-host.example.com/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/javascript", body: "window.__evil = true;" }),
  );
  await page.goto("/");

  await expect(page.addScriptTag({ url: "https://outro-host.example.com/x.js" })).rejects.toThrow();
  await expect
    .poll(() => cspViolations.get(page) ?? [])
    .toEqual([expect.stringContaining("outro-host.example.com")]);
  expect(await page.evaluate(() => (window as unknown as { __evil?: boolean }).__evil)).toBeUndefined();
  cspViolations.get(page)?.splice(0); // violação esperada: não derruba o afterEach
});

