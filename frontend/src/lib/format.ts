import type { Verdict } from "./types.ts";

export const VERDICT_LABEL: Record<Verdict, string> = {
  verdadeiro: "Verdadeiro",
  enganoso: "Enganoso",
  falso: "Falso",
  fora_de_contexto: "Fora de contexto",
  nao_verificavel: "Não verificável",
};

export const AWAITING_CHECK_LABEL = "Ainda sem checagem";

/**
 * "Não verificável" sem checagem de agência é NEUTRO: ninguém checou ainda, o que não quer
 * dizer que o conteúdo seja falso. A tela mostra "Ainda sem checagem" em cinza.
 */
export function isAwaitingCheck(verdict: string, agencyChecks: number | undefined): boolean {
  // Sem a contagem (API anterior a este campo), o neutro é o lado seguro.
  return verdict === "nao_verificavel" && (agencyChecks ?? 0) === 0;
}

// Sinal concreto de manipulação: montagem, adulteração, imitação, golpe, apelo para espalhar,
// alarmismo. "Sem fonte" ou "data recente" não contam. Frases inteiras de propósito: "repasse
// de verbas", "golpe de Estado" e "fake news" são assunto de notícia, não sinal de manipulação.
const MANIPULATION = new RegExp(
  [
    "montag",
    "montad[ao]s?",
    "editad[ao]s?",
    "adulter",
    "manipulad",
    "manipula[çc]",
    "falsificad",
    "forjad",
    "recortad",
    "deepfake",
    "(perfil|conta|site|p[aá]gina|imagem|foto|v[ií]deo|[aá]udio|print)s? (fake|fals[ao]s?|alterad[ao]s?)",
    "gerad[ao]s? por (ia|intelig)",
    "como golpe",
    "golpe (do|da|de) (pix|whatsapp|cadastro|link)",
    "(^|[^a-zà-ÿ])é (um )?golpe",
    "se passa (por|pel[oa])",
    "finge ser",
    "pede (para|que) (compartilh|repass|encaminh|espalh|divulg)",
    "manda (compartilhar|repassar|encaminhar|espalhar)",
    "incentiva (o|a) (compartilhamento|divulga[çc][ãa]o)",
    "compartilhamento em massa",
    "(compartilhe|repasse|encaminhe|espalhe)m? (isso|essa|esta|este|para|a todos|com todos|antes|urgente|já|agora)",
    "antes que (apaguem|tirem|deletem|derrubem)",
    "urgente",
    "alarmis",
    "sensacionalis",
    "mai[uú]sculas",
    "corrente de (whatsapp|mensagens?)",
  ].join("|"),
  "gi",
);
// Negação que desfaz o sinal ("não há sinais de montagem", "a imagem não foi editada"): só
// expressões de negação e só nas palavras logo antes do termo. "Foto sem crédito parece
// montada" continua sinal.
const NEGATION =
  /n[ãa]o (h[áa]|houve|foi|foram|parece|parecem|apresenta|mostra|tem|existe)|sem (sinais?|ind[íi]cios?|evid[êe]ncias?|qualquer)|nenhum[a]? (sinal|ind[íi]cio|evid[êe]ncia|tra[çc]o)|nada indica|aus[êe]ncia de/i;
const NEGATION_JUST_BEFORE = /^(n[ãa]o|nem)$/i;
const NEGATION_WINDOW_WORDS = 5;
const CLAUSE = /[.;:!?,]+/;

function isNegated(before: string): boolean {
  const words = before.trim().split(/ +/).filter(Boolean);
  const window = words.slice(-NEGATION_WINDOW_WORDS).join(" ");
  return NEGATION.test(window) || NEGATION_JUST_BEFORE.test(words.at(-1) ?? "");
}

function hasManipulationSignal(point: string): boolean {
  return point
    .normalize("NFC")
    .split(CLAUSE)
    .some((clause) =>
      Array.from(clause.matchAll(MANIPULATION)).some(
        (match) => !isNegated(clause.slice(0, match.index)),
      ),
    );
}

/** Em "Ainda sem checagem", só os pontos com sinal concreto de manipulação aparecem. */
export function visibleSuspiciousPoints(points: string[], awaitingCheck: boolean): string[] {
  return awaitingCheck ? points.filter(hasManipulationSignal) : points;
}

/** Rótulo seguro mesmo se a API mandar um veredito que esta versão do site não conhece. */
export function verdictLabel(verdict: string): string {
  return VERDICT_LABEL[verdict as Verdict] ?? VERDICT_LABEL.nao_verificavel;
}

// Horário de Roraima (UTC-4, sem horário de verão).
const TIME_ZONE = "America/Boa_Vista";

const DATE_TIME = new Intl.DateTimeFormat("pt-BR", {
  timeZone: TIME_ZONE,
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const DATE_ONLY = new Intl.DateTimeFormat("pt-BR", {
  timeZone: "UTC", // datas "AAAA-MM-DD" sem horário: não deslocar o dia
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

function parse(iso: string): Date | null {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "20/10/2026 às 14:32" no horário de Boa Vista (não depende da vírgula do ICU). */
export function formatDateTime(iso: string): string {
  const date = parse(iso);
  if (!date) return "";
  const part = Object.fromEntries(DATE_TIME.formatToParts(date).map((p) => [p.type, p.value]));
  return `${part.day}/${part.month}/${part.year} às ${part.hour}:${part.minute}`;
}

/** "2026-10-20" → "20/10/2026". */
export function formatDate(isoDate: string): string {
  const date = parse(`${isoDate.slice(0, 10)}T00:00:00Z`);
  return date ? DATE_ONLY.format(date) : isoDate;
}

/** "agora", "há 5 min", "há 3 h", "há 2 dias". */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const date = parse(iso);
  if (!date) return "";
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds < 60) return "agora";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `há ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `há ${hours} h`;
  const days = Math.round(hours / 24);
  return days === 1 ? "há 1 dia" : `há ${days} dias`;
}

// Mesma classe de caracteres do backend (app/services/gemini.py): só ASCII de URL.
const URL_PATTERN = /(https?:\/\/[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+)/;
const TRAILING_PUNCTUATION = /[.,;:!?]+$/;

export type TextPart = { text: string; url: string | null };

/** Divide um texto em pedaços de texto puro e URLs http(s) (pontuação final fica fora). */
export function splitLinks(text: string): TextPart[] {
  const parts: TextPart[] = [];
  text.split(URL_PATTERN).forEach((chunk, index) => {
    if (!chunk) return;
    // Com grupo de captura no split, as URLs ficam nos índices ímpares.
    if (index % 2 === 0) {
      parts.push({ text: chunk, url: null });
      return;
    }
    const tail = TRAILING_PUNCTUATION.exec(chunk)?.[0] ?? "";
    const url = chunk.slice(0, chunk.length - tail.length);
    parts.push({ text: url, url });
    if (tail) parts.push({ text: tail, url: null });
  });
  return parts;
}

/** Só http(s): defesa em profundidade para qualquer href montado com dado da API. */
export function safeHref(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

const INSTITUTIONAL_HOSTS = ["tse.jus.br", "justicaeleitoral.jus.br"];

/**
 * Hosts que podem virar link clicável no texto gerado pela IA: agências que a Fact Check
 * API devolveu e sites institucionais. Qualquer outro endereço aparece só como texto (uma
 * página maliciosa pode induzir a IA a repetir um link de golpe).
 */
export function trustedHosts(agencyUrls: string[]): Set<string> {
  const hosts = new Set(INSTITUTIONAL_HOSTS);
  for (const url of agencyUrls) {
    const href = safeHref(url);
    if (href) hosts.add(new URL(href).hostname.replace(/^www\./, ""));
  }
  return hosts;
}

export function isTrustedUrl(url: string, hosts: Set<string>): boolean {
  const href = safeHref(url);
  if (!href) return false;
  const host = new URL(href).hostname.replace(/^www\./, "");
  return [...hosts].some((allowed) => host === allowed || host.endsWith(`.${allowed}`));
}
