import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

// Menu de acessibilidade (exceção ao congelamento, 16/10/2026): build de produção com a CSP do
// Cloudflare Pages, API simulada. Roda no projeto "celular" (Pixel 7; alguns testes em 360 px) e
// no "desktop".
const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const KEY = "checafatos:a11y";
const SLUG = "7k2m9p4q8r1s";

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

function uf(a: number, b: number) {
  return {
    candidatos: [
      { numero: "22", nome: "CANDIDATO AZUL", pct: a },
      { numero: "13", nome: "CANDIDATO VERMELHO", pct: b },
    ],
    secoes_pct: 87.25,
    final: false,
    totalizado_em: "2026-10-25T19:30:00-03:00",
  };
}

const MAPA = {
  fonte: "TSE",
  link_oficial: "https://resultados.tse.jus.br/",
  eleicao: "6258",
  cores: { "13": "vermelho", "22": "azul" },
  estados: UFS.map((code, i) => ({ uf: code, resultado: code === "ac" ? null : i % 2 ? uf(45, 55) : uf(52, 48) })),
};

const RESULT = {
  fonte: "TSE",
  link_oficial: "https://resultados.tse.jus.br/",
  escopos: [
    { escopo: "br", resultado: null },
    { escopo: "rr", resultado: null },
  ],
};

const CHECK = {
  id: SLUG,
  tipo: "link",
  url: "https://g1.globo.com/rr/roraima/eleicoes/2026/noticia/votacao.ghtml",
  domain: "g1.globo.com",
  title: "Votação do 2º turno em Roraima é adiada",
  published_at: "2026-10-20",
  texto: null,
  veredito: "falso",
  rebaixado_por_confianca: false,
  confianca: 92,
  resumo: "A votação não foi adiada.",
  pontos_suspeitos: ["Sem fonte oficial."],
  contexto_faltante: null,
  checagens_agencias: [],
  fontes_sugeridas: ["Site oficial do TSE"],
  created_at: "2026-10-20T15:30:00Z",
  cache: false,
};

/** Respostas 200 para tudo o que as páginas públicas pedem (sem 404 no console). */
async function mockApi(page: Page) {
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/tse/resultado") return json(route, 200, RESULT);
    if (path === "/tse/mapa") return json(route, 200, MAPA);
    if (path === "/tse/status") return json(route, 200, { banner: false, texto: "", apuracao: true });
    if (path === "/legal/version") return json(route, 200, { version: "2026-10-14" });
    if (path === "/checks/latest") return json(route, 200, { items: [], next_cursor: null });
    if (path === "/comments") return json(route, 200, { items: [], next_cursor: null });
    if (path === `/checks/${SLUG}`) return json(route, 200, CHECK);
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
  });
}

/** Preferências salvas antes de a página abrir (como numa visita anterior). */
async function withPrefs(page: Page, prefs: Record<string, unknown>) {
  await page.addInitScript(
    ([key, value]) => {
      if (!sessionStorage.getItem("__a11y_seeded")) {
        localStorage.setItem(key, value);
        sessionStorage.setItem("__a11y_seeded", "1");
      }
    },
    [KEY, JSON.stringify(prefs)] as const,
  );
}

const menuButton = (page: Page) => page.getByRole("button", { name: /^Acessibilidade/ });
const htmlClasses = (page: Page) => page.evaluate(() => [...document.documentElement.classList]);

async function openMenu(page: Page) {
  await menuButton(page).click();
  const dialog = page.getByRole("dialog", { name: "Acessibilidade" });
  await expect(dialog).toBeVisible();
  return dialog;
}

const TOGGLES: [string, string][] = [
  ["Alto contraste", "a11y-contraste"],
  ["Inverter cores", "a11y-inverter"],
  ["Tons de cinza (monocromático)", "a11y-cinza"],
  ["Sublinhar links", "a11y-links"],
  ["Mais espaço entre linhas e letras", "a11y-espaco"],
  ["Pausar animações", "a11y-pausar"],
  ["Cursor grande", "a11y-cursor"],
];

test("painel em chunk separado; abre pelo teclado, foco preso, Esc fecha e devolve o foco", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  const chunk = page.waitForResponse((r) => /\/assets\/A11yPanel-[^/]+\.js$/.test(r.url()));

  await menuButton(page).focus();
  await page.keyboard.press("Enter");
  await chunk;
  const dialog = page.getByRole("dialog", { name: "Acessibilidade" });
  await expect(dialog).toBeVisible();
  await expect(menuButton(page)).toHaveAttribute("aria-expanded", "true");

  // 30 Tabs (mais que os controles do painel): o foco nunca sai dele.
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Shift+Tab");
  expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(menuButton(page)).toBeFocused();
});

test("cada opção liga e desliga; ligada, continua após recarregar (antes do JavaScript do site)", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  const dialog = await openMenu(page);

  for (const [label, cls] of TOGGLES) {
    const toggle = dialog.getByRole("button", { name: label, exact: false });
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(await htmlClasses(page)).toContain(cls);
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(await htmlClasses(page)).not.toContain(cls);
  }
  // Todas, em ordem: "Inverter cores" (2ª) desliga "Alto contraste" (1ª) — as duas se excluem.
  for (const [label] of TOGGLES) await dialog.getByRole("button", { name: label, exact: false }).click();
  await dialog.getByRole("button", { name: "Aumentar o texto" }).click();
  const persisted = TOGGLES.filter(([, cls]) => cls !== "a11y-contraste");

  // Sem o bundle do site (React), as classes já estão no <html>: quem aplica é o a11y-init.js,
  // antes da 1ª pintura (sem piscar).
  await page.route(/\/assets\/index-[^/]+\.js$/, (route) => route.abort());
  await page.reload();
  const classes = await htmlClasses(page);
  for (const [, cls] of persisted) expect(classes).toContain(cls);
  expect(classes).not.toContain("a11y-contraste");
  expect(classes).toContain("a11y-fonte-1");

  await page.unroute(/\/assets\/index-[^/]+\.js$/);
  await page.reload();
  const again = await openMenu(page);
  for (const [label] of persisted) {
    await expect(again.getByRole("button", { name: label, exact: false })).toHaveAttribute("aria-pressed", "true");
  }
  await expect(again.getByRole("button", { name: "Alto contraste", exact: false })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await again.getByRole("button", { name: "Restaurar padrão" }).click();
  expect(await htmlClasses(page)).toEqual([]);
  await page.reload();
  expect(await htmlClasses(page)).toEqual([]);
});

test("opções ligadas não geram erro no console", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(e.message));
  await mockApi(page);
  for (const path of ["/", "/apuracao", `/checagem/${SLUG}`]) {
    await page.goto(path);
    const dialog = await openMenu(page);
    for (const [label] of TOGGLES) await dialog.getByRole("button", { name: label, exact: false }).click();
    for (let i = 0; i < 3; i++) await dialog.getByRole("button", { name: "Aumentar o texto" }).click();
    await dialog.getByRole("button", { name: "Restaurar padrão" }).click();
    await page.keyboard.press("Escape");
  }
  expect(errors, "erros no console").toEqual([]);
});

test("axe-core: sem violações sérias com o painel aberto e com alto contraste + fonte máxima", async ({ page }) => {
  await mockApi(page);
  for (const path of ["/", "/apuracao"]) {
    await page.goto(path);
    await openMenu(page);
    const open = await new AxeBuilder({ page }).analyze();
    const serious = open.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious.map((v) => `${path} ${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
  }

  await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ fonte: 3, contraste: true })), KEY);
  await page.reload();
  await openMenu(page);
  const contrast = await new AxeBuilder({ page }).analyze();
  const serious = contrast.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
});

test("sem voz em português do Brasil: aviso e Ouvir desabilitado", async ({ page }) => {
  await page.addInitScript(() => {
    if ("speechSynthesis" in window) window.speechSynthesis.getVoices = () => [];
  });
  await mockApi(page);
  await page.goto("/");
  const dialog = await openMenu(page);

  await expect(dialog.getByText(/não tem voz em português do Brasil/)).toBeVisible({ timeout: 5000 });
  await expect(dialog.getByRole("button", { name: "Ouvir" })).toBeDisabled();
});

test.describe("celular: 360 px e Pixel 7", () => {
  test.skip(({ isMobile }) => !isMobile, "só no projeto de celular");

  const PAGES = ["/", "/apuracao", "/ultimas", "/comunidade", `/checagem/${SLUG}`, "/termos", "/privacidade", "/entrar", "/nao-existe"];

  for (const width of [360, 412]) {
    test(`${width} px: fonte máxima (e com mais espaço) não rola para o lado em nenhuma página`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await mockApi(page);
      for (const prefs of [{ fonte: 3 }, { fonte: 3, espaco: true, contraste: true }]) {
        await page.goto("/");
        await page.evaluate(([key, value]) => localStorage.setItem(key, value), [KEY, JSON.stringify(prefs)] as const);
        for (const path of PAGES) {
          await page.goto(path);
          await expect(page.locator("footer")).toBeVisible();
          await page.waitForLoadState("networkidle");
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
          );
          expect(overflow, `${path} ${JSON.stringify(prefs)}`).toBeLessThanOrEqual(0);
        }
        // O painel aberto também cabe na tela.
        await page.goto("/apuracao");
        const dialog = await openMenu(page);
        const box = await dialog.boundingBox();
        expect((box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? 0) <= width).toBe(true);
      }
    });
  }

  test("360 px: o botão não cobre o detalhe do mapa nem o “Ver em tabela”", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 640 });
    await mockApi(page);
    await page.goto("/apuracao");
    const mapa = page.getByRole("group", { name: /Mapa do Brasil/ });
    await mapa.locator('path[data-uf="rs"]').click(); // estado mais ao sul: o detalhe fica no pé da tela
    const detalhe = page.getByRole("region", { name: /^Detalhe/ });
    await expect(detalhe).toContainText("Rio Grande do Sul");
    await page.waitForTimeout(600); // fim da rolagem suave

    const button = await menuButton(page).boundingBox();
    const box = await detalhe.boundingBox();
    expect(button && box).toBeTruthy();
    if (!button || !box) return;
    const overlap =
      button.x < box.x + box.width &&
      box.x < button.x + button.width &&
      button.y < box.y + box.height &&
      box.y < button.y + button.height;
    expect(overlap, "botão sobre o detalhe").toBe(false);

    // “Ver em tabela” continua clicável: o ponto central não está sob o botão.
    const summary = page.getByText("Ver em tabela");
    await summary.scrollIntoViewIfNeeded();
    const s = await summary.boundingBox();
    if (!s) throw new Error("sem “Ver em tabela”");
    const hit = await page.evaluate(
      ({ x, y }) => document.elementFromPoint(x, y)?.closest("summary")?.textContent,
      { x: s.x + s.width / 2, y: s.y + s.height / 2 },
    );
    expect(hit).toBe("Ver em tabela");
  });
});

test("tons de cinza: o mapa perde as cores, mas a tabela abre e mostra os 27 estados", async ({ page }) => {
  await withPrefs(page, { cinza: true });
  await mockApi(page);
  await page.goto("/apuracao");
  expect(await htmlClasses(page)).toContain("a11y-cinza");
  expect(await page.evaluate(() => getComputedStyle(document.getElementById("root") as HTMLElement).filter)).toContain(
    "grayscale",
  );

  await page.getByText("Ver em tabela").click();
  const tabela = page.getByRole("table", { name: "Resultado por estado" });
  await expect(tabela).toBeVisible();
  await expect(tabela.getByRole("row")).toHaveCount(28);
});

test("pausar animações: o mapa rola até o detalhe sem animação", async ({ page }) => {
  await withPrefs(page, { pausar: true });
  await page.addInitScript(() => {
    const calls: unknown[] = [];
    (window as unknown as { __scrolls: unknown[] }).__scrolls = calls;
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (options?: boolean | ScrollIntoViewOptions) {
      calls.push(options ?? null);
      original.call(this, options);
    };
  });
  await mockApi(page);
  await page.goto("/apuracao");
  await page.getByRole("group", { name: /Mapa do Brasil/ }).locator('path[data-uf="rr"]').click();
  await expect(page.getByRole("region", { name: /^Detalhe/ })).toContainText("Roraima");

  const last = await page.evaluate(() => (window as unknown as { __scrolls: unknown[] }).__scrolls.at(-1));
  expect(last).toMatchObject({ behavior: "auto" });
});


test("alto contraste e inverter cores se excluem: a página nunca fica branca", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  const dialog = await openMenu(page);
  const contraste = dialog.getByRole("button", { name: "Alto contraste", exact: false });
  const inverter = dialog.getByRole("button", { name: "Inverter cores", exact: false });

  await contraste.click();
  await inverter.click();
  await expect(contraste).toHaveAttribute("aria-pressed", "false");
  expect(await htmlClasses(page)).not.toContain("a11y-contraste");

  await contraste.click();
  await expect(inverter).toHaveAttribute("aria-pressed", "false");
  const classes = await htmlClasses(page);
  expect(classes).toContain("a11y-contraste");
  expect(classes).not.toContain("a11y-inverter");
  // Fundo preto de verdade (sem a inversão por cima): o #root não tem filtro.
  expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe("rgb(0, 0, 0)");
  expect(await page.evaluate(() => getComputedStyle(document.getElementById("root") as HTMLElement).filter)).toBe(
    "none",
  );

  // Preferência antiga com as duas salvas: fica só o contraste, já antes do JavaScript do site.
  await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ contraste: true, inverter: true })), KEY);
  await page.route(/\/assets\/index-[^/]+\.js$/, (route) => route.abort());
  await page.reload();
  const early = await htmlClasses(page);
  expect(early).toContain("a11y-contraste");
  expect(early).not.toContain("a11y-inverter");
});

test("Ouvir: a seleção não fica presa — reabrir sem seleção e trocar de página leem a página", async ({ page }) => {
  // Voz pt-BR simulada (o Chromium de teste não tem vozes): registra o que seria falado.
  await page.addInitScript(() => {
    const spoken: string[] = [];
    (window as unknown as { __spoken: string[] }).__spoken = spoken;
    const voice = { lang: "pt-BR", name: "Teste", localService: true, default: true, voiceURI: "teste" };
    const fake = {
      getVoices: () => [voice],
      speak: (u: SpeechSynthesisUtterance) => spoken.push(u.text),
      cancel: () => undefined,
      pause: () => undefined,
      resume: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    };
    Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
    // A fala real só aceita vozes reais (TypeError com a simulada).
    class FakeUtterance {
      lang = "";
      voice: unknown = null;
      onend: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(readonly text: string) {}
    }
    Object.defineProperty(window, "SpeechSynthesisUtterance", { value: FakeUtterance, configurable: true });
  });
  const spoken = () => page.evaluate(() => (window as unknown as { __spoken: string[] }).__spoken.join(" "));
  const clearSpoken = () => page.evaluate(() => ((window as unknown as { __spoken: string[] }).__spoken.length = 0));
  await mockApi(page);
  await page.goto("/termos");
  await expect(page.locator("main h1")).toBeVisible();

  // Seleciona só o título e abre o menu: lê o trecho selecionado.
  await page.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector("main h1") as HTMLElement);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
  });
  const title = (await page.locator("main h1").innerText()).trim();
  let dialog = await openMenu(page);
  await dialog.getByRole("button", { name: "Ouvir" }).click();
  await expect(dialog.getByText("Lendo o trecho selecionado.")).toBeVisible();
  expect(await spoken()).toBe(title);

  // Fecha, desfaz a seleção e reabre: lê a página inteira, não o trecho antigo.
  await page.keyboard.press("Escape");
  await page.evaluate(() => window.getSelection()?.removeAllRanges());
  await clearSpoken();
  dialog = await openMenu(page);
  await dialog.getByRole("button", { name: "Ouvir" }).click();
  await expect(dialog.getByText("Lendo o conteúdo da página.")).toBeVisible();
  expect((await spoken()).length).toBeGreaterThan(title.length);

  // Seleção de novo, abre e troca de página (pelo histórico, sem tocar na página): a seleção
  // capturada não vale na página nova.
  await page.keyboard.press("Escape");
  await page.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector("main h1") as HTMLElement);
    window.getSelection()?.addRange(range);
  });
  dialog = await openMenu(page);
  await page.evaluate(() => {
    window.getSelection()?.removeAllRanges();
    history.pushState(null, "", "/privacidade");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page).toHaveURL(/\/privacidade$/);
  await clearSpoken();
  await dialog.getByRole("button", { name: "Ouvir" }).click();
  await expect(dialog.getByText("Lendo o conteúdo da página.")).toBeVisible();
  expect(await spoken()).not.toBe(title);
});
