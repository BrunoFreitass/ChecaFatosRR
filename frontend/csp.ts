// Content-Security-Policy do site. Uma fonte só para os dois lugares em que ela vale:
// - build: vai para dist/_headers (o Cloudflare Pages aplica no site publicado);
// - vite preview: mesmo cabeçalho, para os e2e rodarem com a CSP de produção.
// Sem 'unsafe-inline' e sem 'unsafe-eval': o build do Vite só tem scripts e CSS em arquivo.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { loadEnv, type Plugin } from "vite";

type CspInput = { apiUrl: string | undefined; supabaseUrl: string | undefined };

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

function origin(name: string, value: string | undefined): string {
  if (!value || /[\s;,]/.test(value)) {
    throw new Error(`${name} ausente ou inválida para a CSP`);
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} não é uma URL: ${value}`);
  }
  // http só para a máquina local (desenvolvimento e e2e); em produção, sempre https.
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOCAL_HOSTS.has(url.hostname))) {
    throw new Error(`${name} precisa ser https (só localhost pode usar http): ${value}`);
  }
  return url.origin;
}

export function buildCsp({ apiUrl, supabaseUrl }: CspInput): string {
  const api = origin("VITE_API_URL", apiUrl);
  const supabase = origin("VITE_SUPABASE_URL", supabaseUrl);
  return [
    "default-src 'self'",
    // Cloudflare Web Analytics (14/10/2026): o beacon vem deste host; sem cookies.
    "script-src 'self' https://static.cloudflareinsights.com",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    // O beacon envia para o próprio site (injeção automática) ou para cloudflareinsights.com.
    `connect-src 'self' ${api} ${supabase} https://cloudflareinsights.com`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

type BuildEnv = Record<string, string | undefined>;

/**
 * Confere as variáveis do build antes de gerar o site. Sem a chave publicável o site publicado
 * fica em branco; a chave secreta nunca pode ir para o JavaScript público; no Cloudflare
 * (CF_PAGES=1), localhost indica um build com variáveis locais por engano.
 */
export function assertBuildEnv(env: BuildEnv, { cloudflare }: { cloudflare: boolean }): string {
  const key = env.VITE_SUPABASE_PUBLISHABLE_KEY;
  if (!key) {
    throw new Error("VITE_SUPABASE_PUBLISHABLE_KEY ausente: o site publicado ficaria em branco");
  }
  if (key.startsWith("sb_secret_")) {
    throw new Error("VITE_SUPABASE_PUBLISHABLE_KEY recebeu a chave secret do Supabase: use a publicável");
  }
  // buildCsp valida as duas URLs (formato, https); aqui elas já são URLs válidas.
  const csp = buildCsp({ apiUrl: env.VITE_API_URL, supabaseUrl: env.VITE_SUPABASE_URL });
  if (cloudflare) {
    for (const name of ["VITE_API_URL", "VITE_SUPABASE_URL"] as const) {
      if (LOCAL_HOSTS.has(new URL(env[name] ?? "").hostname)) {
        throw new Error(`${name} aponta para localhost num build do Cloudflare Pages`);
      }
    }
  }
  return csp;
}

/** Acrescenta (ou troca) a CSP no bloco "/*" do arquivo _headers do Cloudflare Pages. */
export function injectCsp(headers: string, csp: string): string {
  const eol = headers.includes("\r\n") ? "\r\n" : "\n"; // checkout no Windows: CRLF
  const lines = headers.split(eol);
  const start = lines.findIndex((line) => line.trim() === "/*");
  if (start === -1) {
    throw new Error('_headers sem o bloco "/*" para a CSP');
  }
  // O bloco vai até a próxima linha sem recuo (outro caminho) ou o fim do arquivo.
  let end = start + 1;
  while (end < lines.length && /^\s/.test(lines[end])) end += 1;
  const block = lines
    .slice(start + 1, end)
    .filter((line) => !/^\s+Content-Security-Policy:/.test(line));
  return [
    ...lines.slice(0, start + 1),
    `  Content-Security-Policy: ${csp}`,
    ...block,
    ...lines.slice(end),
  ].join(eol);
}

/** Plugin do Vite: CSP no dist/_headers (build) e no vite preview (e2e). */
export function cspPlugin(): Plugin {
  let csp = "";
  let outDir = "dist";
  return {
    name: "checafatosrr-csp",
    configResolved(config) {
      outDir = config.build.outDir;
      if (config.command === "build" || config.isProduction) {
        const env = loadEnv(config.mode, config.envDir || process.cwd(), "VITE_");
        csp =
          config.command === "build"
            ? assertBuildEnv(env, { cloudflare: process.env.CF_PAGES === "1" })
            : buildCsp({ apiUrl: env.VITE_API_URL, supabaseUrl: env.VITE_SUPABASE_URL });
      }
    },
    writeBundle() {
      const file = join(outDir, "_headers");
      writeFileSync(file, injectCsp(readFileSync(file, "utf-8"), csp));
    },
    configurePreviewServer(server) {
      server.middlewares.use((_req, res, next) => {
        res.setHeader("Content-Security-Policy", csp);
        next();
      });
    },
  };
}
