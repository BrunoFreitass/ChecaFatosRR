// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TseCandidate, TseResponse, TseResult } from "../lib/tse.ts";
import { Apuracao } from "./Apuracao.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api.ts", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly code: string,
      message: string,
    ) {
      super(message);
    }
  },
  apiFetch: api.apiFetch,
}));

// O mapa tem os próprios testes (MapaApuracao.test.tsx) e consulta /tse/mapa por conta própria;
// aqui só importa que a página o carrega sob demanda.
vi.mock("../components/MapaApuracao.tsx", () => ({
  default: () => <p>mapa carregado</p>,
}));

const CANDIDATE_A: TseCandidate = { numero: "22", nome: "CANDIDATO A", partido: "PA", vice: "VICE A", votos: 56104503, pct: 52.63, situacao: "", eleito: false };
const CANDIDATE_B: TseCandidate = { numero: "13", nome: "CANDIDATO B", partido: "PB", vice: "VICE B", votos: 900, pct: 47.37, situacao: "", eleito: false };

function result(overrides: Partial<TseResult> = {}): TseResult {
  return {
    escopo: "br",
    eleicao: "6258",
    turno: 2,
    cargo: "Presidente",
    final: false,
    totalizado_em: "2026-10-25T19:30:00-03:00",
    secoes_pct: 45.3,
    eleitores: 158745502,
    validos: 1000,
    brancos: 10,
    brancos_pct: 0.9,
    nulos: 20,
    nulos_pct: 1.8,
    abstencao: 300,
    abstencao_pct: 21.08,
    candidatos: [CANDIDATE_A, CANDIDATE_B],
    ...overrides,
  };
}

function response(br: TseResult | null, rr: TseResult | null = null): TseResponse {
  return {
    fonte: "TSE",
    link_oficial: "https://resultados.tse.jus.br/",
    escopos: [
      { escopo: "br", resultado: br },
      { escopo: "rr", resultado: rr },
    ],
  };
}

function renderPage() {
  render(
    <MemoryRouter>
      <Apuracao />
    </MemoryRouter>,
  );
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  api.apiFetch.mockReset();
  setVisibility("visible");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Apuracao", () => {
  it("mostra o resultado oficial do Brasil com a fonte e as seções totalizadas", async () => {
    api.apiFetch.mockResolvedValue(response(result()));
    renderPage();

    expect(await screen.findByText("CANDIDATO A")).toBeTruthy();
    expect(screen.getByText("56.104.503 votos")).toBeTruthy();
    expect(screen.getByText("52,63%")).toBeTruthy();
    expect(screen.getByText("45,30% das seções totalizadas")).toBeTruthy();
    // Leitor de tela: a região viva diz também de qual aba é o número.
    expect(screen.getByText("Brasil: 45,30% das seções totalizadas")).toBeTruthy();
    expect(screen.getByText(/Fonte: TSE/)).toBeTruthy();
    expect(screen.getByText(/25\/10\/2026 às 18:30/)).toBeTruthy(); // 19h30 de Brasília = 18h30 em Boa Vista
    expect(screen.getByRole("link", { name: /resultados\.tse\.jus\.br/ }).getAttribute("href")).toBe(
      "https://resultados.tse.jus.br/",
    );
    // Consulta pública: sem o token do usuário (cache HTTP e sem preflight).
    expect(api.apiFetch).toHaveBeenCalledWith("/tse/resultado", expect.objectContaining({ anonymous: true }));
  });

  it("troca para a aba Roraima", async () => {
    api.apiFetch.mockResolvedValue(
      response(result(), result({ escopo: "rr", candidatos: [{ ...CANDIDATE_B, nome: "SO EM RR" }] })),
    );
    renderPage();
    await screen.findByText("CANDIDATO A");

    fireEvent.click(screen.getByRole("tab", { name: "Roraima" }));

    expect(screen.getByRole("tab", { name: "Roraima" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("SO EM RR")).toBeTruthy();
    expect(screen.queryByText("CANDIDATO A")).toBeNull();
  });

  it('mostra "Eleito" só quando o TSE marca', async () => {
    const final = result({
      secoes_pct: 100,
      final: true,
      candidatos: [
        { ...CANDIDATE_A, eleito: true, situacao: "Eleito" },
        { ...CANDIDATE_B, situacao: "Não eleito" },
      ],
    });
    api.apiFetch.mockResolvedValue(response(final));
    renderPage();

    expect(await screen.findAllByText("Eleito")).toHaveLength(1);
    expect(screen.queryByText("Não eleito")).toBeNull();
  });

  it("parcial não tem eleito nem destaque de vencedor", async () => {
    api.apiFetch.mockResolvedValue(response(result()));
    renderPage();
    await screen.findByText("CANDIDATO A");
    expect(screen.queryByText("Eleito")).toBeNull();
    expect(screen.queryByText(/vencendo|liderando|projeção/i)).toBeNull();
  });

  it("antes dos primeiros números, avisa que aguarda o TSE", async () => {
    api.apiFetch.mockResolvedValue(response(null, null));
    renderPage();
    expect(await screen.findByText(/Aguardando os primeiros resultados do TSE/)).toBeTruthy();
  });

  it("apuração desligada mostra o link oficial", async () => {
    const { ApiError } = await import("../lib/api.ts");
    api.apiFetch.mockRejectedValue(new ApiError(404, "apuracao_indisponivel", "A apuração ainda não está disponível."));
    renderPage();
    expect(await screen.findByText("A apuração ainda não está disponível.")).toBeTruthy();
    expect(screen.getByRole("link", { name: /resultados\.tse\.jus\.br/ })).toBeTruthy();
  });

  it("falha ao atualizar mantém os números anteriores", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.apiFetch.mockResolvedValueOnce(response(result())).mockRejectedValueOnce(new Error("rede"));
    renderPage();
    await screen.findByText("CANDIDATO A");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(screen.getByText("CANDIDATO A")).toBeTruthy();
    expect(screen.getByText(/Não foi possível atualizar agora/)).toBeTruthy();
  });

  it("consulta a cada 60 s só com a aba visível", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.apiFetch.mockResolvedValue(response(result()));
    renderPage();
    await screen.findByText("CANDIDATO A");
    expect(api.apiFetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(api.apiFetch).toHaveBeenCalledTimes(2);

    act(() => setVisibility("hidden"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    expect(api.apiFetch).toHaveBeenCalledTimes(2);

    act(() => setVisibility("visible")); // voltou para a aba: atualiza na hora
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalledTimes(3));
  });

  // ---------- revisão (11/10/2026) ----------

  it("abas pelo teclado: setas, Home e End (padrão WAI-ARIA)", async () => {
    api.apiFetch.mockResolvedValue(response(result(), result({ escopo: "rr" })));
    renderPage();
    await screen.findByText("CANDIDATO A");
    const brasil = screen.getByRole("tab", { name: "Brasil" });
    const roraima = screen.getByRole("tab", { name: "Roraima" });
    expect(brasil.getAttribute("tabindex")).toBe("0");
    expect(roraima.getAttribute("tabindex")).toBe("-1");

    fireEvent.keyDown(brasil, { key: "ArrowRight" });
    expect(roraima.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(roraima);
    fireEvent.keyDown(roraima, { key: "Home" });
    expect(brasil.getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(brasil, { key: "End" });
    expect(roraima.getAttribute("aria-selected")).toBe("true");
  });

  it("cada aba controla um painel que existe", async () => {
    api.apiFetch.mockResolvedValue(response(result()));
    renderPage();
    await screen.findByText("CANDIDATO A");
    for (const tab of screen.getAllByRole("tab")) {
      const id = tab.getAttribute("aria-controls") ?? "";
      expect(document.getElementById(id)).not.toBeNull();
    }
  });

  it("resposta com formato inesperado vira erro, sem derrubar a página", async () => {
    api.apiFetch.mockResolvedValue(null);
    renderPage();
    expect(await screen.findByText("Não foi possível carregar a apuração.")).toBeTruthy();
  });

  it("turno e cargo vêm do TSE (ensaio com o 1º turno)", async () => {
    api.apiFetch.mockResolvedValue(response(result({ turno: 1 })));
    renderPage();
    expect(await screen.findByText("Presidente · 1º turno")).toBeTruthy();
  });

  it("sem percentual de seções não mostra um traço solto", async () => {
    api.apiFetch.mockResolvedValue(response(result({ secoes_pct: null })));
    renderPage();
    await screen.findByText("CANDIDATO A");
    expect(screen.queryByText(/das seções totalizadas/)).toBeNull();
  });

  it("não sobrepõe consultas: um tick com outra em andamento é ignorado", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let release: (value: TseResponse) => void = () => undefined;
    api.apiFetch
      .mockResolvedValueOnce(response(result()))
      .mockImplementationOnce(() => new Promise<TseResponse>((resolve) => (release = resolve)))
      .mockResolvedValue(response(result()));
    renderPage();
    await screen.findByText("CANDIDATO A");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000); // 2ª consulta fica pendurada
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000); // tick com a 2ª ainda em andamento
    });
    expect(api.apiFetch).toHaveBeenCalledTimes(2);
    await act(async () => release(response(result())));
  });

  it("carrega o mapa por estado sob demanda, depois do resultado", async () => {
    api.apiFetch.mockResolvedValue(response(result()));
    renderPage();

    expect(await screen.findByText("mapa carregado")).toBeTruthy();
    expect(api.apiFetch).toHaveBeenCalledTimes(1); // só /tse/resultado; o mapa faz a sua
  });
});
