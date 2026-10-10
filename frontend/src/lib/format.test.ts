import { describe, expect, it } from "vitest";

import {
  formatDate,
  formatDateTime,
  isAwaitingCheck,
  isTrustedUrl,
  relativeTime,
  safeHref,
  splitLinks,
  trustedHosts,
  verdictLabel,
  visibleSuspiciousPoints,
} from "./format.ts";

describe("splitLinks", () => {
  it("separa URLs do texto e deixa a pontuação final fora do link", () => {
    expect(splitLinks("Veja https://www.tse.jus.br/a. E mais")).toEqual([
      { text: "Veja ", url: null },
      { text: "https://www.tse.jus.br/a", url: "https://www.tse.jus.br/a" },
      { text: ".", url: null },
      { text: " E mais", url: null },
    ]);
  });

  it("não transforma 'http:// algo' em link", () => {
    expect(splitLinks("http:// foo")).toEqual([{ text: "http:// foo", url: null }]);
  });

  it("caractere invisível no meio não cria um segundo link", () => {
    const parts = splitLinks(`https://www.tse.jus.br/a${String.fromCharCode(0xfeff)}https://evil.com/x`);
    expect(parts.filter((part) => part.url).map((part) => part.url)).toEqual([
      "https://www.tse.jus.br/a",
      "https://evil.com/x",
    ]);
    // O backend já remove esses caracteres; aqui o que protege é a lista de hosts confiáveis.
    const hosts = trustedHosts([]);
    expect(isTrustedUrl("https://evil.com/x", hosts)).toBe(false);
  });
});

describe("links confiáveis", () => {
  const hosts = trustedHosts(["https://www.aosfatos.org/noticias/x/", "javascript:alert(1)"]);

  it("aceita agências da API e sites institucionais (com subdomínio)", () => {
    expect(isTrustedUrl("https://aosfatos.org/outra", hosts)).toBe(true);
    expect(isTrustedUrl("https://www.tse.jus.br/eleicoes", hosts)).toBe(true);
    expect(isTrustedUrl("https://resultados.tse.jus.br/x", hosts)).toBe(true);
  });

  it("recusa qualquer outro host, inclusive imitações", () => {
    expect(isTrustedUrl("https://tse.jus.br.golpe.com/x", hosts)).toBe(false);
    expect(isTrustedUrl("https://golpe-tse.jus.br.com/x", hosts)).toBe(false);
    expect(isTrustedUrl("https://site-qualquer.com.br/", hosts)).toBe(false);
  });

  it("safeHref só aceita http(s)", () => {
    expect(safeHref("https://a.com/x")).toBe("https://a.com/x");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,x")).toBeNull();
    expect(safeHref(null)).toBeNull();
  });
});

describe("datas", () => {
  it("mostra data e hora no horário de Boa Vista", () => {
    expect(formatDateTime("2026-10-20T15:30:00Z")).toBe("20/10/2026 às 11:30");
  });

  it("não quebra com data inválida", () => {
    expect(formatDateTime("lixo")).toBe("");
    expect(relativeTime("lixo")).toBe("");
    expect(formatDate("lixo")).toBe("lixo");
  });

  it("data sem horário não muda de dia", () => {
    expect(formatDate("2026-10-20")).toBe("20/10/2026");
  });

  it("tempo relativo", () => {
    const now = Date.parse("2026-10-20T12:00:00Z");
    expect(relativeTime("2026-10-20T11:59:30Z", now)).toBe("agora");
    expect(relativeTime("2026-10-20T11:55:00Z", now)).toBe("há 5 min");
    expect(relativeTime("2026-10-19T12:00:00Z", now)).toBe("há 1 dia");
  });
});

it("veredito desconhecido aparece como Não verificável", () => {
  expect(verdictLabel("talvez")).toBe("Não verificável");
  expect(verdictLabel("falso")).toBe("Falso");
});

describe("isAwaitingCheck", () => {
  it("Não verificável sem checagem de agência é neutro (Ainda sem checagem)", () => {
    expect(isAwaitingCheck("nao_verificavel", 0)).toBe(true);
  });

  it("com checagem de agência ou outro veredito, não", () => {
    expect(isAwaitingCheck("nao_verificavel", 1)).toBe(false);
    expect(isAwaitingCheck("falso", 0)).toBe(false);
    expect(isAwaitingCheck("verdadeiro", 0)).toBe(false);
  });
});

describe("visibleSuspiciousPoints", () => {
  const points = [
    "Não cita nenhuma fonte oficial",
    "Data de publicação recente",
    "Pede para compartilhar antes que apaguem",
    "Imagem com sinais de montagem",
    "Usa letras maiúsculas e tom alarmista",
    "Perfil se passa pelo TRE",
  ];

  it("com veredito firme, mostra todos os pontos", () => {
    expect(visibleSuspiciousPoints(points, false)).toEqual(points);
  });

  it("em Ainda sem checagem, só pontos com sinal concreto de manipulação", () => {
    expect(visibleSuspiciousPoints(points, true)).toEqual([
      "Pede para compartilhar antes que apaguem",
      "Imagem com sinais de montagem",
      "Usa letras maiúsculas e tom alarmista",
      "Perfil se passa pelo TRE",
    ]);
  });

  it("falta de fonte ou data recente não viram suspeita em Ainda sem checagem", () => {
    expect(
      visibleSuspiciousPoints(["Sem autor identificado", "Não há fonte oficial", "Notícia de hoje"], true),
    ).toEqual([]);
  });
});

describe("visibleSuspiciousPoints: negação e variações", () => {
  it.each([
    "Não há sinais de montagem",
    "A imagem não foi editada",
    "Não pede para compartilhar",
    "Sem indícios de manipulação",
    "Nenhum sinal de adulteração no vídeo",
    "Foi compartilhado pelo G1",
    "Publicado por corrente política",
    // Achados do /code-review: palavras comuns em notícia política não são manipulação.
    "Cita o repasse de verbas federais ao estado",
    "A mensagem afirma que haverá golpe de Estado",
    "Acusa a imprensa de espalhar fake news",
  ])("sem sinal concreto: %s", (point) => {
    expect(visibleSuspiciousPoints([point], true)).toEqual([]);
  });

  it.each([
    "Usa montagem e não cita a fonte",
    "Perfil fake com o nome do TRE",
    "Página falsa imita o site do TSE",
    "Texto idêntico circula como golpe",
    "Imagem gerada por IA",
    "Pede para encaminhar a todos os contatos",
    "VÍDEO ALTERADO para mudar o sentido",
    // Negação longe do termo (ou "sem" com outro sentido) não esconde o sinal.
    "Não há autor identificado e o texto pede para compartilhar",
    "Foto sem crédito parece montada",
    "Incentiva o compartilhamento em massa",
    "Manda repassar para todos os grupos",
  ])("com sinal concreto: %s", (point) => {
    expect(visibleSuspiciousPoints([point], true)).toEqual([point]);
  });

  it("acento decomposto (NFD) também conta", () => {
    const nfd = "Vídeo alterado".normalize("NFD");
    expect(visibleSuspiciousPoints([nfd], true)).toEqual([nfd]);
  });
});

it("isAwaitingCheck: API antiga sem total_agencias conta como sem checagem", () => {
  expect(isAwaitingCheck("nao_verificavel", undefined)).toBe(true);
});
