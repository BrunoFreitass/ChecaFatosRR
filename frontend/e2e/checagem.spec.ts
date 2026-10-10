import { expect, test, type Page, type Route } from "@playwright/test";

const API = "http://127.0.0.1:9999";
const SUPABASE = "https://e2e-ref.supabase.co";
const STORAGE_KEY = "sb-e2e-ref-auth-token";
const USER_ID = "11111111-2222-4333-8444-555555555555";

const PROFILE = {
  apelido: "eleitor_rr",
  status: "active",
  suspended_until: null,
  is_admin: false,
  terms_version: "2026-10-08",
  created_at: "2026-10-09T12:00:00Z",
};

const LINK_CHECK = {
  id: "7k2m9p4q8r1s",
  tipo: "link",
  url: "https://www.folhabv.com.br/eleicoes/votacao-adiada",
  domain: "folhabv.com.br",
  title: "Votação do 2º turno em Roraima é adiada",
  published_at: "2026-10-20",
  texto: null,
  veredito: "falso",
  rebaixado_por_confianca: false,
  confianca: 92,
  resumo: "A votação não foi adiada: o TSE manteve o 2º turno em 25 de outubro.",
  pontos_suspeitos: ["Não cita nenhuma fonte oficial"],
  contexto_faltante: null,
  checagens_agencias: [
    {
      agencia: "Aos Fatos",
      titulo: "É falso que a votação em RR foi adiada",
      veredito: "Falso",
      url: "https://www.aosfatos.org/noticias/votacao-rr/",
      data: "2026-10-19",
    },
  ],
  fontes_sugeridas: ["Site oficial do TSE"],
  created_at: "2026-10-20T15:30:00Z",
  cache: false,
};

const TEXT_CHECK = {
  ...LINK_CHECK,
  id: "3h8j2k5m7n9p",
  tipo: "texto",
  url: null,
  domain: null,
  title: null,
  published_at: null,
  texto: "URGENTE: a votação foi adiada! Ligue [telefone oculto]",
  veredito: "nao_verificavel",
  rebaixado_por_confianca: true,
  confianca: 30,
  checagens_agencias: [],
  pontos_suspeitos: ["Não cita nenhuma fonte oficial", "Pede para repassar a todos os grupos"],
};

// Toda violação de CSP derruba o teste: o e2e roda no build de produção, com o mesmo
// cabeçalho que vai para o Cloudflare Pages (plugin em csp.ts).
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

type Handler = (route: Route) => Promise<void> | void;

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, DELETE",
};

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, headers: CORS, contentType: "application/json", body: JSON.stringify(body) });
}

/** Sessão do Supabase injetada antes do carregamento (como se o login Google já tivesse ocorrido). */
async function signIn(page: Page) {
  const session = {
    access_token: "token-e2e",
    token_type: "bearer",
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    refresh_token: "refresh-e2e",
    user: {
      id: USER_ID,
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

/** API simulada: GET /me + as rotas do teste. */
async function mockApi(page: Page, routes: Record<string, Handler>) {
  await page.route(`${SUPABASE}/**`, (route) => route.fulfill({ status: 200, body: "{}" }));
  await page.route(`${API}/**`, async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const path = new URL(request.url()).pathname;
    const handler = routes[`${request.method()} ${path}`];
    if (handler) return handler(route);
    if (path === "/me") return json(route, 200, PROFILE);
    return json(route, 404, { error: { code: "not_found", message: "Não encontrado." } });
  });
}

test("checa um link, mostra o resultado e lista em Últimas", async ({ page }) => {
  let body: unknown = null;
  await signIn(page);
  await mockApi(page, {
    "POST /checks": (route) => {
      body = route.request().postDataJSON();
      return json(route, 200, LINK_CHECK);
    },
    "GET /checks/latest": (route) =>
      json(route, 200, {
        items: [
          {
            id: LINK_CHECK.id,
            domain: LINK_CHECK.domain,
            title: LINK_CHECK.title,
            veredito: LINK_CHECK.veredito,
            created_at: LINK_CHECK.created_at,
            total_agencias: 1,
          },
          {
            id: "4k9m2n5p7q8r",
            domain: "g1.globo.com",
            title: "Eleitores de Roraima voltam às urnas no 2º turno",
            veredito: "nao_verificavel",
            created_at: "2026-10-20T15:00:00Z",
            total_agencias: 0,
          },
        ],
        next_cursor: null,
      }),
  });

  await page.goto("/");
  await page.getByLabel("Cole o link da notícia").fill("folhabv.com.br/eleicoes/votacao-adiada");
  await page.getByRole("button", { name: "Checar" }).click();

  await expect(page).toHaveURL(`/checagem/${LINK_CHECK.id}`);
  expect(body).toEqual({ url: "https://folhabv.com.br/eleicoes/votacao-adiada" });
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(LINK_CHECK.title);
  await expect(page.getByText("Falso", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Análise automatizada. Confira também")).toBeVisible();
  await expect(page.getByRole("link", { name: "É falso que a votação em RR foi adiada" })).toHaveAttribute(
    "rel",
    "noopener noreferrer nofollow ugc",
  );
  await expect(page.getByRole("link", { name: "Enviar no WhatsApp" })).toHaveAttribute(
    "href",
    /^https:\/\/wa\.me\/\?text=/,
  );

  await page.getByRole("navigation", { name: "Principal" }).getByRole("link", { name: "Últimas" }).click();
  await expect(page.getByRole("heading", { name: "Últimas checagens" })).toBeVisible();
  await expect(page.getByRole("link", { name: new RegExp(LINK_CHECK.title) })).toBeVisible();
  // Sem checagem de agência: neutro também na lista, igual à página do resultado.
  const awaiting = page.getByRole("link", { name: /voltam às urnas/ });
  await expect(awaiting).toContainText("Ainda sem checagem");
  await expect(awaiting).not.toContainText("Não verificável");
});

test("link de rede social leva a colar o texto da mensagem", async ({ page }) => {
  const bodies: unknown[] = [];
  await signIn(page);
  await mockApi(page, {
    "POST /checks": (route) => {
      const body = route.request().postDataJSON() as { url?: string; texto?: string };
      bodies.push(body);
      if (body.url) {
        return json(route, 422, {
          error: {
            code: "pagina_ilegivel",
            message: "Não conseguimos ler esse link. Cole o texto da mensagem para checar.",
            sugestao: "colar_texto",
          },
        });
      }
      return json(route, 200, TEXT_CHECK);
    },
  });

  await page.goto("/");
  await page.getByLabel("Cole o link da notícia").fill("https://www.instagram.com/p/abc123/");
  await page.getByRole("button", { name: "Checar" }).click();

  await expect(page.getByRole("alert")).toContainText("Não conseguimos ler esse link");
  await page.getByRole("button", { name: "Colar o texto da mensagem" }).click();

  const textarea = page.getByLabel("Cole aqui a mensagem que você recebeu no WhatsApp");
  await expect(textarea).toBeFocused();
  await textarea.fill("URGENTE: a votação foi adiada! Ligue (95) 99123-4567");
  await page.getByRole("button", { name: "Checar" }).click();

  await expect(page).toHaveURL(`/checagem/${TEXT_CHECK.id}`);
  expect(bodies[1]).toEqual({ texto: "URGENTE: a votação foi adiada! Ligue (95) 99123-4567" });
  // Sem checagem de agência: neutro, nunca "parece falso".
  await expect(page.getByText("Ainda sem checagem", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Isso não significa que seja falso.")).toBeVisible();
  await expect(page.getByText("Não verificável", { exact: true })).toHaveCount(0);
  // Só o ponto com sinal concreto de manipulação aparece.
  await expect(page.getByText("Pede para repassar a todos os grupos")).toBeVisible();
  await expect(page.getByText("Não cita nenhuma fonte oficial")).toHaveCount(0);
  await page.getByText("Mensagem analisada").click();
  await expect(page.getByText("[telefone oculto]")).toBeVisible();
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", "noindex");
});

test("colar uma mensagem no campo de link troca para a aba de texto", async ({ page }) => {
  await signIn(page);
  await mockApi(page, {});
  await page.goto("/");

  const message = "Atenção eleitores de Roraima: a urna vai trocar o seu voto se você votar depois das 15h!";
  await page.getByLabel("Cole o link da notícia").focus();
  await page.evaluate((text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    document.activeElement?.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }),
    );
  }, message);

  await expect(page.getByRole("tab", { name: "Texto da mensagem" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Cole aqui a mensagem que você recebeu no WhatsApp")).toHaveValue(message);
});

test("abas navegáveis pelo teclado (setas, Home e End)", async ({ page }) => {
  await signIn(page);
  await mockApi(page, {});
  await page.goto("/");

  const linkTab = page.getByRole("tab", { name: "Link" });
  const textTab = page.getByRole("tab", { name: "Texto da mensagem" });
  await linkTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(textTab).toBeFocused();
  await expect(textTab).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(linkTab).toBeFocused();
  await page.keyboard.press("End");
  await expect(textTab).toBeFocused();
});

test("sem login, guarda o que foi digitado e manda para o login", async ({ page }) => {
  await mockApi(page, {});
  await page.goto("/");

  await page.getByLabel("Cole o link da notícia").fill("https://g1.globo.com/rr/noticia.html");
  await page.getByRole("button", { name: "Checar" }).click();

  await expect(page).toHaveURL(/\/entrar/);
  const draft = await page.evaluate(() => window.sessionStorage.getItem("checafatosrr:rascunho"));
  expect(JSON.parse(draft ?? "null")).toEqual({ modo: "link", valor: "https://g1.globo.com/rr/noticia.html" });
});

test("análise de versão anterior do verificador mostra aviso discreto", async ({ page }) => {
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${LINK_CHECK.id}`]: (route) =>
      json(route, 200, { ...LINK_CHECK, cache: true, analise_anterior: true }),
  });

  await page.goto(`/checagem/${LINK_CHECK.id}`);

  await expect(page.getByRole("heading", { level: 1 })).toHaveText(LINK_CHECK.title);
  const note = page.getByText("Análise anterior", { exact: false });
  await expect(note).toBeVisible();
  await expect(note).toContainText("versão anterior do nosso verificador");
});

test("análise atual não mostra o aviso de análise anterior", async ({ page }) => {
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${LINK_CHECK.id}`]: (route) =>
      json(route, 200, { ...LINK_CHECK, analise_anterior: false }),
  });
  await page.goto(`/checagem/${LINK_CHECK.id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(LINK_CHECK.title);
  await expect(page.getByText("Análise anterior", { exact: false })).toHaveCount(0);
});

test("o site é servido com a CSP de produção", async ({ page }) => {
  await mockApi(page, {});
  const response = await page.goto("/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain(`connect-src 'self' ${API} ${SUPABASE}`);
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).not.toContain("unsafe-");
});

test("login com Google vai para o Supabase sem violar a CSP", async ({ page }) => {
  let authorize: URL | null = null;
  await mockApi(page, {});
  await page.route(`${SUPABASE}/auth/v1/authorize**`, (route) => {
    authorize = new URL(route.request().url());
    return route.fulfill({ status: 200, contentType: "text/html", body: "<p>Google</p>" });
  });

  await page.goto("/entrar");
  await page.getByRole("button", { name: "Entrar com Google" }).click();

  await expect.poll(() => authorize?.searchParams.get("provider")).toBe("google");
  expect(authorize!.searchParams.get("redirect_to")).toMatch(/\/auth\/callback$/);
});

test("compartilhar: copiar link e WhatsApp funcionam com a CSP", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${LINK_CHECK.id}`]: (route) => json(route, 200, LINK_CHECK),
  });

  await page.goto(`/checagem/${LINK_CHECK.id}`);
  await page.getByRole("button", { name: "Copiar link" }).click();
  await expect(page.getByText("Link copiado!")).toBeVisible();
  await expect(page.getByRole("link", { name: "Enviar no WhatsApp" })).toHaveAttribute(
    "href",
    /^https:\/\/wa\.me\/\?text=/,
  );
});

test("a CSP bloqueia script inline e o detector de violações registra", async ({ page }) => {
  await mockApi(page, {});
  await page.goto("/");
  const ran = await page.evaluate(() => {
    const script = document.createElement("script");
    script.textContent = "window.__inlineRan = true";
    document.head.appendChild(script);
    return (window as unknown as { __inlineRan?: boolean }).__inlineRan === true;
  });
  expect(ran).toBe(false); // bloqueado pela script-src 'self'
  const found = cspViolations.get(page)!;
  await expect.poll(() => found.length).toBeGreaterThan(0);
  expect(found[0]).toContain("script-src");
  found.length = 0; // a violação era esperada: não derruba o afterEach
});
