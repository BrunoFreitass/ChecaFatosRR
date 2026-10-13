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
  "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
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


const OTHER = {
  id: "c-maria",
  apelido: "maria_rr",
  texto: "Alguém sabe se a votação mudou mesmo?",
  criado_em: "2026-10-20T16:00:00Z",
  removido_pelo_autor: false,
  respostas: [],
};

function comment(id: string, texto: string) {
  return {
    id,
    apelido: PROFILE.apelido,
    texto,
    criado_em: "2026-10-20T17:00:00Z",
    removido_pelo_autor: false,
    respostas: [],
  };
}

const listOf = (items: unknown[]) => (route: Route) => json(route, 200, { items, next_cursor: null });

test("comenta, responde, denuncia e apaga o próprio comentário", async ({ page }) => {
  const posts: unknown[] = [];
  const reports: string[] = [];
  const deletes: string[] = [];
  let getQuery = "";
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${LINK_CHECK.id}`]: (route) => json(route, 200, LINK_CHECK),
    "GET /comments": (route) => {
      getQuery = new URL(route.request().url()).search;
      return listOf([OTHER])(route);
    },
    "POST /comments": (route) => {
      const body = route.request().postDataJSON() as { texto: string; resposta_a?: string };
      posts.push(body);
      return json(route, 201, comment(body.resposta_a ? "c-reply" : "c-own", body.texto));
    },
    "POST /comments/c-maria/denuncia": (route) => {
      reports.push("c-maria");
      return json(route, 200, { status: "recebida" });
    },
    "DELETE /comments/c-own": (route) => {
      deletes.push("c-own");
      return route.fulfill({ status: 204, headers: CORS });
    },
  });

  await page.goto(`/checagem/${LINK_CHECK.id}`);
  await expect(page.getByRole("heading", { name: "Comentários" })).toBeVisible();
  await expect(page.getByText(OTHER.texto)).toBeVisible();
  expect(getQuery).toBe(`?checagem=${LINK_CHECK.id}`);

  // Comenta
  await page.getByLabel("Escreva um comentário").fill("Concordo, o TSE manteve o 2º turno.");
  await page.getByRole("button", { name: "Comentar", exact: true }).click();
  const own = page.getByRole("article", { name: "Comentário de eleitor_rr" }).first();
  await expect(own).toContainText("Concordo, o TSE manteve o 2º turno.");
  expect(posts[0]).toEqual({ texto: "Concordo, o TSE manteve o 2º turno.", checagem: LINK_CHECK.id });

  // Responde ao comentário da Maria
  const maria = page.getByRole("article", { name: "Comentário de maria_rr" });
  await maria.getByRole("button", { name: "Responder" }).click();
  const replyField = page.getByLabel("Responder a maria_rr");
  await expect(replyField).toBeFocused();
  await replyField.fill("Não mudou, confira o site do TSE.");
  await page.locator("form").filter({ has: replyField }).getByRole("button", { name: "Responder" }).click();
  await expect(maria).toContainText("Não mudou, confira o site do TSE.");
  expect(posts[1]).toEqual({
    texto: "Não mudou, confira o site do TSE.",
    checagem: LINK_CHECK.id,
    resposta_a: "c-maria",
  });

  // Denuncia o comentário de outra pessoa (o próprio não tem "Denunciar")
  await expect(own.getByRole("button", { name: "Denunciar" })).toHaveCount(0);
  await maria.getByRole("button", { name: "Denunciar" }).first().click();
  await expect(page.getByText("Denúncia recebida. Obrigado!")).toBeVisible();
  expect(reports).toEqual(["c-maria"]);

  // Apaga o próprio, com confirmação
  await own.getByRole("button", { name: "Apagar" }).click();
  await expect(page.getByText("Apagar este comentário? Não dá para desfazer.")).toBeVisible();
  await page.getByRole("button", { name: "Sim, apagar" }).click();
  await expect(page.getByText("Comentário removido pelo autor")).toBeVisible();
  await expect(page.getByText("Concordo, o TSE manteve o 2º turno.")).toHaveCount(0);
  expect(deletes).toEqual(["c-own"]);
});

test("comentário com HTML/XSS vindo da API aparece como texto literal", async ({ page }) => {
  const payload = "<img src=x onerror=alert(1)>";
  let dialog = false;
  page.on("dialog", (d) => {
    dialog = true;
    void d.dismiss();
  });
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${LINK_CHECK.id}`]: (route) => json(route, 200, LINK_CHECK),
    "GET /comments": listOf([{ ...OTHER, id: "c-xss", texto: payload }]),
  });

  await page.goto(`/checagem/${LINK_CHECK.id}`);
  await expect(page.getByText(payload, { exact: true })).toBeVisible();
  await expect(page.locator("img")).toHaveCount(0);
  expect(dialog).toBe(false);
});

test("violação de regra no comentário mostra o aviso e o link para os Termos", async ({ page }) => {
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${LINK_CHECK.id}`]: (route) => json(route, 200, LINK_CHECK),
    "GET /comments": listOf([]),
    "POST /comments": (route) =>
      json(route, 422, {
        error: {
          code: "violacao",
          message: "Comentário bloqueado.",
          regra: "Ataques pessoais",
          explicacao: "O comentário ofende uma pessoa.",
          strikes: 1,
          status: "active",
          suspenso_ate: null,
        },
      }),
  });

  await page.goto(`/checagem/${LINK_CHECK.id}`);
  await expect(page.getByText("Ainda não há comentários.")).toBeVisible();
  await page.getByLabel("Escreva um comentário").fill("texto ofensivo");
  await page.getByRole("button", { name: "Comentar", exact: true }).click();

  const alert = page.getByRole("alert").filter({ hasText: "Aviso de violação" });
  await expect(alert).toContainText("Aviso de violação: Ataques pessoais");
  await expect(alert).toContainText("O comentário ofende uma pessoa.");
  await expect(alert.getByRole("link", { name: "Termos de Uso" })).toHaveAttribute("href", "/termos");
  await expect(page.getByText("Advertências ativas: 1.", { exact: false })).toBeVisible();
});

test("texto colado não mostra a seção de comentários", async ({ page }) => {
  let commentsRequested = false;
  await signIn(page);
  await mockApi(page, {
    [`GET /checks/${TEXT_CHECK.id}`]: (route) => json(route, 200, TEXT_CHECK),
    "GET /comments": (route) => {
      commentsRequested = true;
      return listOf([])(route);
    },
  });

  await page.goto(`/checagem/${TEXT_CHECK.id}`);
  await expect(page.getByText("Mensagem analisada")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Comentários" })).toHaveCount(0);
  await expect(page.getByLabel("Escreva um comentário")).toHaveCount(0);
  expect(commentsRequested).toBe(false);
});

test("menu Comunidade leva ao feed; sem login pede a conta Google", async ({ page }) => {
  let getQuery: string | null = null;
  await mockApi(page, {
    "GET /comments": (route) => {
      getQuery = new URL(route.request().url()).search;
      return listOf([OTHER])(route);
    },
  });

  await page.goto("/");
  await page.getByRole("navigation", { name: "Principal" }).getByRole("link", { name: "Comunidade" }).click();

  await expect(page).toHaveURL(/\/comunidade$/);
  await expect(page.getByRole("heading", { level: 1, name: "Comunidade" })).toBeVisible();
  await expect(page.getByText(OTHER.texto)).toBeVisible();
  expect(getQuery).toBe("");
  await expect(page.getByRole("link", { name: "Entre com sua conta Google" })).toBeVisible();
  await expect(page.getByLabel("Escreva um comentário")).toHaveCount(0);
});

const QUEUE = [
  {
    id: 101,
    tipo: "comment_review",
    motivo: "hidden_by_reports",
    categoria: null,
    confianca: null,
    criado_em: "2026-10-20T18:00:00Z",
    apelido: "joao_rr",
    comentario: "c-joao",
    status_comentario: "hidden",
    texto: "Comentário suspeito do João",
  },
  {
    id: 102,
    tipo: "strike_confirmation",
    motivo: "ai_low_confidence",
    categoria: "ataque",
    confianca: 0.62,
    criado_em: "2026-10-20T18:05:00Z",
    apelido: "ana_rr",
    comentario: "c-ana",
    status_comentario: "blocked",
    texto: "Comentário bloqueado da Ana",
  },
];

test("admin vê a fila, aprova um item e confirma o banimento", async ({ page }) => {
  const actions: unknown[] = [];
  await signIn(page);
  await mockApi(page, {
    "GET /me": (route) => json(route, 200, { ...PROFILE, is_admin: true }),
    "GET /admin/moderacao/fila": (route) => json(route, 200, { items: QUEUE }),
    "POST /admin/moderacao/fila/101": (route) => {
      actions.push(["101", route.request().postDataJSON()]);
      return json(route, 200, { status: "ok" });
    },
    "POST /admin/moderacao/fila/102": (route) => {
      actions.push(["102", route.request().postDataJSON()]);
      return json(route, 200, { status: "ok" });
    },
  });

  await page.goto("/admin/moderacao");
  await expect(page.getByRole("heading", { name: "Fila de moderação" })).toBeVisible();
  await expect(page.getByText("Comentário suspeito do João")).toBeVisible();
  await expect(page.getByText("Comentário bloqueado da Ana")).toBeVisible();

  const joao = page.getByRole("listitem").filter({ hasText: "Comentário suspeito do João" });
  await joao.getByRole("button", { name: "Aprovar" }).click();
  await expect(page.getByText("Comentário suspeito do João")).toHaveCount(0);
  expect(actions).toEqual([["101", { acao: "aprovar" }]]);

  const ana = page.getByRole("listitem").filter({ hasText: "Comentário bloqueado da Ana" });
  await ana.getByRole("button", { name: "Banir" }).click();
  await expect(ana.getByRole("group", { name: "Confirmar banimento" })).toBeVisible();
  expect(actions).toHaveLength(1); // pedir confirmação não bane
  await ana.getByRole("button", { name: "Sim, banir ana_rr" }).click();
  await expect(page.getByText("Comentário bloqueado da Ana")).toHaveCount(0);
  expect(actions[1]).toEqual(["102", { acao: "banir" }]);
  await expect(page.getByText("A fila está vazia.")).toBeVisible();
});

test("usuário comum não vê a fila e ela não é requisitada", async ({ page }) => {
  let queueRequested = false;
  await signIn(page);
  await mockApi(page, {
    "GET /admin/moderacao/fila": (route) => {
      queueRequested = true;
      return json(route, 200, { items: QUEUE });
    },
  });

  await page.goto("/admin/moderacao");
  await expect(page.getByText("Só a administração do site pode ver esta página.")).toBeVisible();
  await expect(page.getByText("Comentário suspeito do João")).toHaveCount(0);
  expect(queueRequested).toBe(false);
});
