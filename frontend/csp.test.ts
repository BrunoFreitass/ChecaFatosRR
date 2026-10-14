import { describe, expect, it } from "vitest";

import { assertBuildEnv, buildCsp, injectCsp } from "./csp.ts";

const PROD = { apiUrl: "https://checafatosrr-api-abc-rj.a.run.app", supabaseUrl: "https://abcd.supabase.co" };

describe("buildCsp", () => {
  it("só libera o próprio site, a API e o Supabase", () => {
    const csp = buildCsp(PROD);
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("script-src 'self'");
    expect(csp).toContain("style-src 'self'");
    expect(csp).toContain(
      "connect-src 'self' https://checafatosrr-api-abc-rj.a.run.app https://abcd.supabase.co",
    );
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("base-uri 'self'");
    expect(csp).toContain("form-action 'self'");
  });

  it("libera o Cloudflare Web Analytics (beacon e envio), sem mais nada de fora", () => {
    const csp = buildCsp(PROD);
    expect(csp).toContain("script-src 'self' https://static.cloudflareinsights.com;");
    expect(csp).toContain(
      "connect-src 'self' https://checafatosrr-api-abc-rj.a.run.app https://abcd.supabase.co https://cloudflareinsights.com;",
    );
  });

  it("nunca usa unsafe-inline nem unsafe-eval", () => {
    expect(buildCsp(PROD)).not.toMatch(/unsafe-/);
  });

  it("usa só a origem (sem caminho) das URLs", () => {
    const csp = buildCsp({ ...PROD, apiUrl: "https://api.exemplo.com.br/v1/" });
    expect(csp).toContain("connect-src 'self' https://api.exemplo.com.br https://abcd.supabase.co");
  });

  it("http só para localhost (e2e e desenvolvimento)", () => {
    expect(buildCsp({ ...PROD, apiUrl: "http://127.0.0.1:9999" })).toContain("http://127.0.0.1:9999");
    expect(() => buildCsp({ ...PROD, apiUrl: "http://api.exemplo.com.br" })).toThrow(/https/);
  });

  it("falha no build se faltar URL", () => {
    expect(() => buildCsp({ ...PROD, apiUrl: undefined })).toThrow(/VITE_API_URL/);
    expect(() => buildCsp({ ...PROD, supabaseUrl: "" })).toThrow(/VITE_SUPABASE_URL/);
    expect(() => buildCsp({ ...PROD, apiUrl: "não é url" })).toThrow(/VITE_API_URL/);
  });

  it("valores com espaço ou ponto e vírgula não injetam diretivas", () => {
    expect(() => buildCsp({ ...PROD, apiUrl: "https://a.com; script-src *" })).toThrow();
  });
});

describe("injectCsp", () => {
  const HEADERS = "# comentário\n/*\n  X-Frame-Options: DENY\n\n/assets/*\n  Cache-Control: immutable\n";

  it("acrescenta a CSP no bloco /*", () => {
    const out = injectCsp(HEADERS, "default-src 'self'");
    expect(out).toContain("/*\n  Content-Security-Policy: default-src 'self'\n  X-Frame-Options: DENY");
    expect(out).toContain("/assets/*\n  Cache-Control: immutable");
  });

  it("substitui uma CSP que já exista", () => {
    const once = injectCsp(HEADERS, "default-src 'self'");
    const twice = injectCsp(once, "default-src 'none'");
    expect(twice.match(/Content-Security-Policy/g)).toHaveLength(1);
    expect(twice).toContain("Content-Security-Policy: default-src 'none'");
  });

  it("falha se não houver o bloco /*", () => {
    expect(() => injectCsp("/assets/*\n  Cache-Control: x\n", "default-src 'self'")).toThrow(/\/\*/);
  });
});

describe("assertBuildEnv", () => {
  const ENV = {
    VITE_API_URL: PROD.apiUrl,
    VITE_SUPABASE_URL: PROD.supabaseUrl,
    VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_x",
  };

  it("aceita as três variáveis de produção e devolve a CSP", () => {
    expect(assertBuildEnv(ENV, { cloudflare: true })).toBe(buildCsp(PROD));
  });

  it("falha sem a chave publicável (senão o site publicado fica em branco)", () => {
    expect(() => assertBuildEnv({ ...ENV, VITE_SUPABASE_PUBLISHABLE_KEY: "" }, { cloudflare: true })).toThrow(
      /VITE_SUPABASE_PUBLISHABLE_KEY/,
    );
  });

  it("nunca aceita a chave secreta do Supabase no front", () => {
    expect(() =>
      assertBuildEnv({ ...ENV, VITE_SUPABASE_PUBLISHABLE_KEY: "sb_secret_abc" }, { cloudflare: false }),
    ).toThrow(/secret/);
  });

  it("no Cloudflare, localhost é recusado (build feito com variáveis locais)", () => {
    expect(() => assertBuildEnv({ ...ENV, VITE_API_URL: "http://127.0.0.1:9999" }, { cloudflare: true })).toThrow(
      /localhost/,
    );
    expect(() => assertBuildEnv({ ...ENV, VITE_API_URL: "http://127.0.0.1:9999" }, { cloudflare: false })).not.toThrow();
  });
});

describe("injectCsp com CRLF (checkout no Windows)", () => {
  it("mantém o mesmo fim de linha do arquivo", () => {
    const crlf = "/*\r\n  X-Frame-Options: DENY\r\n";
    const out = injectCsp(crlf, "default-src 'self'");
    expect(out).toBe("/*\r\n  Content-Security-Policy: default-src 'self'\r\n  X-Frame-Options: DENY\r\n");
  });

  it("só mexe na CSP do bloco /*", () => {
    const headers = "/*\n  X-A: 1\n/especial\n  Content-Security-Policy: default-src 'none'\n";
    const out = injectCsp(headers, "default-src 'self'");
    expect(out).toContain("/especial\n  Content-Security-Policy: default-src 'none'");
  });
});
