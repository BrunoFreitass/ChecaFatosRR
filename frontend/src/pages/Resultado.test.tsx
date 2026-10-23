// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgencyCheck, Check } from "../lib/types.ts";
import { Resultado } from "./Resultado.tsx";

// A checagem chega pelo estado da rota (como vinda do formulário): nada de rede. O módulo
// real exige VITE_API_URL ao carregar, e a CI roda os testes sem variáveis de ambiente.
vi.mock("../lib/api.ts", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
  },
  apiFetch: vi.fn(),
}));
// Comentários têm teste próprio (components/Comments.test.tsx): aqui só onde aparecem.
vi.mock("../components/Comments.tsx", () => ({
  Comments: ({ checagem }: { checagem: string | null }) => <p>comentários de {checagem}</p>,
}));

const AGENCY: AgencyCheck = {
  agencia: "Aos Fatos",
  titulo: "Não houve adiamento",
  veredito: "Falso",
  url: "https://www.aosfatos.org/noticias/adiamento/",
  data: null,
};

function check(overrides: Partial<Check> = {}): Check {
  return {
    id: "k7mqp9xwvrzt",
    tipo: "link",
    url: "https://g1.globo.com/rj/noticia/ruas.ghtml",
    domain: "g1.globo.com",
    title: "Ruas diz que segurança pública será prioridade",
    published_at: "2026-10-10",
    texto: null,
    veredito: "nao_verificavel",
    rebaixado_por_confianca: false,
    confianca: 95,
    resumo: "Conteúdo publicado em g1.globo.com (origem confirmada) sobre: Ruas.",
    pontos_suspeitos: [],
    contexto_faltante: null,
    checagens_agencias: [],
    fontes_sugeridas: [],
    created_at: "2026-10-11T00:50:09Z",
    cache: false,
    ...overrides,
  };
}

function renderResult(value: Check) {
  const path = `/checagem/${value.id}`;
  const router = createMemoryRouter([{ path: "/checagem/:slug", element: <Resultado /> }], {
    initialEntries: [{ pathname: path, state: { check: value } }],
  });
  render(<RouterProvider router={router} />);
}

afterEach(cleanup);

describe("Resultado", () => {
  it('esconde a confiança da análise em "Ainda sem checagem"', () => {
    renderResult(check());
    expect(screen.getAllByText("Ainda sem checagem").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Confiança da análise/)).toBeNull();
  });

  it("mostra a confiança quando há veredito com agência", () => {
    renderResult(check({ veredito: "falso", confianca: 85, checagens_agencias: [AGENCY] }));
    expect(screen.getByText("Confiança da análise: 85%")).toBeTruthy();
  });

  it("lista as checagens não usadas como assuntos relacionados, sem tirar o neutro", () => {
    renderResult(check({ checagens_relacionadas: [AGENCY] }));
    expect(screen.getAllByText("Ainda sem checagem").length).toBeGreaterThan(0);
    const heading = screen.getByRole("heading", {
      name: "Checagens sobre assuntos relacionados",
    });
    const section = heading.closest("section");
    expect(section?.textContent).toContain("Não houve adiamento");
    const link = screen.getByRole("link", { name: /Não houve adiamento/ });
    expect(link.getAttribute("href")).toBe(AGENCY.url);
  });

  it("relacionada com endereço perigoso aparece sem link", () => {
    renderResult(check({ checagens_relacionadas: [{ ...AGENCY, url: "javascript:alert(1)" }] }));
    expect(screen.getByText("Não houve adiamento").closest("a")).toBeNull();
  });

  it("lista vazia de relacionadas não mostra a seção", () => {
    renderResult(check({ checagens_relacionadas: [] }));
    expect(
      screen.queryByRole("heading", { name: "Checagens sobre assuntos relacionados" }),
    ).toBeNull();
  });

  it("checagem antiga, sem o campo de relacionadas, não mostra a seção", () => {
    renderResult(check());
    expect(
      screen.queryByRole("heading", { name: "Checagens sobre assuntos relacionados" }),
    ).toBeNull();
  });
describe("modo só agências (Gemini indisponível)", () => {
    const FOUND =
      "A análise automática está indisponível agora. Abaixo estão as checagens de agências " +
      "encontradas sobre o assunto: confira se tratam do mesmo conteúdo.";
    const agenciesOnly = (overrides: Partial<Check> = {}) =>
      check({ modo: "agencias", confianca: 0, resumo: FOUND, checagens_agencias: [AGENCY], ...overrides });

    it("mostra só o selo neutro: sem veredito, sem confiança e sem 'Ainda sem checagem'", () => {
      renderResult(agenciesOnly());
      expect(screen.getAllByText("Análise automática indisponível agora")).toHaveLength(1);
      expect(screen.queryByText("Não verificável")).toBeNull();
      expect(screen.queryByText("Ainda sem checagem")).toBeNull();
      expect(screen.queryByText(/Confiança da análise/)).toBeNull();
    });

    it("usa o selo neutro no título da página", () => {
      renderResult(agenciesOnly());
      expect(document.title).toContain("Análise automática indisponível agora");
    });

    it("lista as checagens das agências encontradas, com link", () => {
      renderResult(agenciesOnly());
      const heading = screen.getByRole("heading", { name: "Checagens de agências encontradas" });
      expect(heading.closest("section")?.textContent).toContain("Não houve adiamento");
      const link = screen.getByRole("link", { name: /Não houve adiamento/ });
      expect(link.getAttribute("href")).toBe(AGENCY.url);
    });

    it("sem checagens, diz que nada foi encontrado", () => {
      renderResult(agenciesOnly({ checagens_agencias: [], resumo: "Indisponível agora." }));
      expect(screen.getByText(/não encontramos checagens de agências/i)).toBeTruthy();
    });

    it("não mostra texto de IA (pouca confiança, pontos suspeitos, contexto)", () => {
      renderResult(
        agenciesOnly({
          rebaixado_por_confianca: true,
          pontos_suspeitos: ["Não cita fonte"],
          contexto_faltante: "Contexto da IA",
        }),
      );
      expect(screen.queryByText(/pouca confiança/)).toBeNull();
      expect(screen.queryByRole("heading", { name: "Pontos suspeitos" })).toBeNull();
      expect(screen.queryByRole("heading", { name: "Contexto que falta" })).toBeNull();
    });
  });

  it("busca nas agências falhou (modo com IA): diz que falhou, nunca 'não encontramos'", () => {
    renderResult(check({ checagens_agencias: [], busca_agencias: "falhou" }));
    const heading = screen.getByRole("heading", { name: "O que dizem as agências de checagem" });
    const section = heading.closest("section");
    expect(section?.textContent).toContain(
      "A busca nas agências de checagem falhou nesta análise. Tente novamente mais tarde.",
    );
    expect(section?.textContent).not.toMatch(/não encontramos/i);
  });

  it("busca falhou e sem veredito (\"Ainda sem checagem\"): nenhuma frase da página diz 'não encontramos'", () => {
    renderResult(check({ veredito: "nao_verificavel", checagens_agencias: [], busca_agencias: "falhou" }));
    expect(screen.getAllByText(/A busca nas agências de checagem falhou nesta análise/).length).toBeGreaterThanOrEqual(2);
    expect(document.body.textContent).not.toMatch(/não encontramos/i);
  });

  it("busca nas agências falhou (modo só agências): também diz que falhou", () => {
    renderResult(
      check({ modo: "agencias", confianca: 0, checagens_agencias: [], busca_agencias: "falhou", resumo: "x" }),
    );
    const heading = screen.getByRole("heading", { name: "Checagens de agências encontradas" });
    const section = heading.closest("section");
    expect(section?.textContent).toContain("A busca nas agências de checagem falhou nesta análise.");
    expect(section?.textContent).not.toMatch(/não encontramos/i);
  });

  it("busca ok e nada encontrado: 'não encontramos'", () => {
    renderResult(check({ checagens_agencias: [], busca_agencias: "ok" }));
    expect(screen.getByText("Não encontramos checagens de agências sobre este conteúdo.")).toBeTruthy();
    expect(screen.queryByText(/A busca nas agências de checagem falhou/)).toBeNull();
  });

  it("checagem antiga sem o registro da busca: o texto de antes (nada inventado)", () => {
    renderResult(check({ checagens_agencias: [] }));
    expect(screen.getByText("Não encontramos checagens de agências sobre este conteúdo.")).toBeTruthy();
    expect(screen.queryByText(/A busca nas agências de checagem falhou/)).toBeNull();
  });

  it("checagem sem o campo modo (API antiga) segue com o veredito de sempre", () => {
    renderResult(check({ veredito: "falso", confianca: 85, checagens_agencias: [AGENCY] }));
    expect(screen.queryByText("Análise automática indisponível agora")).toBeNull();
    expect(screen.getByRole("heading", { name: "O que dizem as agências de checagem" })).toBeTruthy();
  });
});


describe("comentários", () => {
  it("aparecem em checagens de link", () => {
    renderResult(check());
    expect(screen.getByText("comentários de k7mqp9xwvrzt")).toBeTruthy();
  });

  it("não aparecem em texto colado", () => {
    renderResult(check({ tipo: "texto", url: null, domain: null, title: null, texto: "Mensagem." }));
    expect(screen.queryByText(/comentários de/)).toBeNull();
  });
});

function whatsappText(): string {
  const href = screen.getByRole("link", { name: "Enviar no WhatsApp" }).getAttribute("href") ?? "";
  return decodeURIComponent(href.replace("https://wa.me/?text=", ""));
}

const ESTADAO = "https://www.estadao.com.br/estadao-verifica/frude-transmissao-alteracao-votos-leicoes/";

function agencyPage(alegacao: string | null): Check {
  return check({
    url: ESTADAO,
    domain: "estadao.com.br",
    title: "É falso que transmissão de dados permitiu alterar votos | Estadão",
    veredito: "nao_verificavel",
    confianca: 0,
    resumo: "Este link é uma checagem publicada por Estadão.",
    modo: "checagem_de_agencia",
    checagem_de_agencia: {
      agencia: "Estadão",
      url: ESTADAO,
      classificacao: "Falso",
      alegacao,
      titulo: "É falso que transmissão de dados permitiu alterar votos",
    },
  });
}

describe("link que é a própria checagem de uma agência (23/10)", () => {
  it("mostra a agência, a alegação verificada e a classificação dela, sem veredito nem confiança nossos", () => {
    renderResult(agencyPage("Transmissão dos dados permitiu alterar votos"));
    expect(screen.getByText("Este link é uma checagem publicada por Estadão.")).toBeTruthy();
    expect(screen.getByText("Alegação verificada: Transmissão dos dados permitiu alterar votos")).toBeTruthy();
    expect(screen.getByText("Classificação da agência: Falso")).toBeTruthy();
    const read = screen.getByRole("link", { name: "Ler a checagem completa" });
    expect(read.getAttribute("href")).toBe(ESTADAO);
    expect(screen.queryByText(/Confiança da análise/)).toBeNull();
    expect(screen.queryByText("Não verificável")).toBeNull();
    expect(screen.queryByText("Ainda sem checagem")).toBeNull();
    expect(screen.queryByText(/Título da checagem/)).toBeNull();
    expect(screen.queryByText(/Análise automatizada/)).toBeNull(); // não houve análise nossa
  });

  it("texto do WhatsApp cita a agência, a alegação e a classificação", () => {
    renderResult(agencyPage("Transmissão dos dados permitiu alterar votos"));
    expect(whatsappText()).toMatch(
      /^ChecaFatosRR: checagem de Estadão — alegação "Transmissão dos dados permitiu alterar votos" classificada como Falso\n/,
    );
  });

  it("sem claimReviewed: mostra o título como 'Título da checagem', nunca como alegação", () => {
    renderResult(agencyPage(null));
    expect(
      screen.getByText("Título da checagem: É falso que transmissão de dados permitiu alterar votos"),
    ).toBeTruthy();
    expect(screen.queryByText(/Alegação verificada/)).toBeNull();
    expect(whatsappText()).not.toMatch(/alegação/);
  });
});

describe("não verificável (23/10)", () => {
  it("com checagens encontradas: sem confiança da análise", () => {
    renderResult(check({ veredito: "nao_verificavel", confianca: 60, checagens_agencias: [AGENCY] }));
    expect(screen.queryByText(/Confiança da análise/)).toBeNull();
  });

  it("texto do WhatsApp: ainda sem checagem conclusiva", () => {
    renderResult(check({ veredito: "nao_verificavel", checagens_agencias: [AGENCY] }));
    expect(whatsappText()).toMatch(/— ainda sem checagem conclusiva\n/);
    cleanup();
    renderResult(check());
    expect(whatsappText()).toMatch(/— ainda sem checagem conclusiva\n/);
  });

  it("modo só agências mantém o selo dele no WhatsApp (não houve análise)", () => {
    renderResult(check({ modo: "agencias", veredito: "nao_verificavel", confianca: 0 }));
    expect(whatsappText()).toMatch(/— Análise automática indisponível agora$/m);
  });

  it("veredito com agência mantém o texto de sempre no WhatsApp", () => {
    renderResult(check({ veredito: "falso", confianca: 85, checagens_agencias: [AGENCY] }));
    expect(whatsappText()).toMatch(/— Falso\n/);
  });
});
