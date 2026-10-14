// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import MapaApuracao from "./MapaApuracao.tsx";

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

const UFS = ["ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms", "mt", "pa",
  "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc", "se", "sp", "to"];

function resposta() {
  return {
    fonte: "TSE",
    eleicao: "6257",
    cores: { "13": "vermelho", "22": "azul" },
    estados: UFS.map((uf) => ({
      uf,
      resultado:
        uf === "ac"
          ? null
          : {
              candidatos: [
                { numero: uf === "rr" ? "22" : "13", nome: uf === "rr" ? "FLAVIO BOLSONARO" : "LULA", pct: 60.5 },
                { numero: uf === "rr" ? "13" : "22", nome: uf === "rr" ? "LULA" : "FLAVIO BOLSONARO", pct: 39.5 },
              ],
              secoes_pct: 87.25,
              final: false,
              totalizado_em: null as string | null,
            },
    })),
  };
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
  document.dispatchEvent(new Event("visibilitychange"));
}

const scrolls: ScrollIntoViewOptions[] = [];
let reducedMotion = false;

beforeEach(() => {
  setVisibility("visible");
  scrolls.length = 0;
  reducedMotion = false;
  // jsdom não tem scrollIntoView nem matchMedia: registra as chamadas.
  Element.prototype.scrollIntoView = function (options?: boolean | ScrollIntoViewOptions) {
    scrolls.push(typeof options === "object" ? options : {});
  };
  window.matchMedia = ((query: string) => ({
    matches: query.includes("prefers-reduced-motion") && reducedMotion,
    media: query,
  })) as unknown as typeof window.matchMedia;
});

afterEach(() => {
  cleanup();
  api.apiFetch.mockReset();
  vi.useRealTimers();
});

describe("MapaApuracao", () => {
  it("desenha os 27 estados e a tabela acessível com 27 linhas", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);

    const mapa = await screen.findByRole("group", { name: /Mapa do Brasil/ });
    expect(mapa.querySelectorAll("path")).toHaveLength(27);
    const tabela = screen.getByRole("table");
    expect(within(tabela).getAllByRole("row")).toHaveLength(28); // cabeçalho + 27
    expect(within(tabela).getByText("Acre").closest("tr")?.textContent).toMatch(/Sem dados/);
  });

  it("tocar num estado mostra os percentuais e as urnas apuradas", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    const mapa = await screen.findByRole("group", { name: /Mapa do Brasil/ });

    fireEvent.click(mapa.querySelector('path[data-uf="rr"]') as Element);

    const detalhe = screen.getByRole("region", { name: "Detalhe do estado" });
    expect(detalhe.textContent).toMatch(/Roraima/);
    expect(detalhe.textContent).toMatch(/FLAVIO BOLSONARO/);
    expect(detalhe.textContent).toMatch(/60,50%/);
    expect(detalhe.textContent).toMatch(/87,25%/);
  });

  it("nunca escreve Eleito e a legenda é igual para os dois", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    const { container } = render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    expect(container.textContent).not.toMatch(/eleit/i);
    const legenda = screen.getByRole("list", { name: "Legenda do mapa" });
    expect(legenda.textContent).toMatch(/LULA à frente/);
    expect(legenda.textContent).toMatch(/FLAVIO BOLSONARO à frente/);
    expect(container.textContent).toMatch(/Natural Earth/);
  });

  it("com o mapa desligado (404), some e para de consultar", async () => {
    const { ApiError } = await import("../lib/api.ts");
    api.apiFetch.mockRejectedValue(new ApiError(404, "mapa_indisponivel", "O mapa não está disponível."));
    const { container } = render(<MapaApuracao />);

    await vi.waitFor(() => expect(api.apiFetch).toHaveBeenCalledTimes(1));
    expect(container.textContent).toBe("");
  });

  it("consulta a API sem login, com sinal de cancelamento", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    expect(api.apiFetch).toHaveBeenCalledWith(
      "/tse/mapa",
      expect.objectContaining({ anonymous: true, signal: expect.any(AbortSignal) }),
    );
  });

  it("teclado: cada estado é um botão com nome; Enter mostra o detalhe", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    const roraima = screen.getByRole("button", { name: "Roraima" });
    expect(roraima.getAttribute("tabindex")).toBe("0");
    fireEvent.keyDown(roraima, { key: "Enter" });
    expect(screen.getByRole("region", { name: "Detalhe do estado" }).textContent).toMatch(/Roraima/);
    expect(roraima.getAttribute("aria-pressed")).toBe("true");
  });

  it("detalhe mostra quando o TSE atualizou o estado (horário de Boa Vista)", async () => {
    const data = resposta();
    const rr = data.estados.find((e) => e.uf === "rr")?.resultado;
    if (rr) rr.totalizado_em = "2026-10-25T19:30:00-03:00";
    api.apiFetch.mockResolvedValue(data);
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    fireEvent.click(screen.getByRole("button", { name: "Roraima" }));
    expect(screen.getByRole("region", { name: "Detalhe do estado" }).textContent).toMatch(/Atualizado em 25\/10\/2026 às 18:30/);
  });

  it("empate aparece como empate na tabela e na legenda", async () => {
    const data = resposta();
    const rr = data.estados.find((e) => e.uf === "rr")?.resultado;
    if (rr) for (const c of rr.candidatos) c.pct = 50;
    api.apiFetch.mockResolvedValue(data);
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    const linha = within(screen.getByRole("table")).getByText("Roraima").closest("tr");
    expect(linha?.textContent).toMatch(/Empate/);
    expect(screen.getByRole("list", { name: "Legenda do mapa" }).textContent).toMatch(/Empate/);
  });

  it("passar o mouse não muda o detalhe (região viva sem ruído)", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    const mapa = await screen.findByRole("group", { name: /Mapa do Brasil/ });

    fireEvent.mouseEnter(mapa.querySelector('path[data-uf="rr"]') as Element);
    expect(screen.getByRole("region", { name: "Detalhe do estado" }).textContent).not.toMatch(/Roraima/);
  });

  it("tabela e detalhe em ordem de percentual, mesmo se a API mandar fora de ordem", async () => {
    const data = resposta();
    data.estados.find((e) => e.uf === "rr")?.resultado?.candidatos.reverse(); // LULA 39,5 primeiro
    api.apiFetch.mockResolvedValue(data);
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    const linha = within(screen.getByRole("table")).getByText("Roraima").closest("tr");
    expect(linha?.textContent).toMatch(/FLAVIO BOLSONARO 60,50%.*LULA 39,50%/);
  });

  it("erro depois de carregado mantém o mapa e avisa", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.apiFetch.mockResolvedValueOnce(resposta()).mockRejectedValue(new Error("rede"));
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.getByText(/mostrando a última atualização do mapa/)).toBeTruthy();
    expect(screen.getByRole("group", { name: /Mapa do Brasil/ })).toBeTruthy();
  });

  it("ao voltar para a aba, atualiza na hora se já passou o intervalo", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    setVisibility("hidden");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000); // tick com a aba escondida: não consulta
    });
    expect(api.apiFetch).toHaveBeenCalledTimes(1);
    await act(async () => setVisibility("visible"));
    expect(api.apiFetch).toHaveBeenCalledTimes(2);
  });

  it("depois do 404 não consulta mais, nem ao voltar para a aba", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { ApiError } = await import("../lib/api.ts");
    api.apiFetch.mockRejectedValue(new ApiError(404, "mapa_indisponivel", "x"));
    render(<MapaApuracao />);
    await vi.waitFor(() => expect(api.apiFetch).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
      setVisibility("visible");
    });
    expect(api.apiFetch).toHaveBeenCalledTimes(1);
  });

  it("tocar num estado rola até o detalhe (suave) e põe o foco nele", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    fireEvent.click(screen.getByRole("button", { name: "Roraima" }));

    const detalhe = screen.getByRole("region", { name: "Detalhe do estado" });
    expect(document.activeElement).toBe(detalhe);
    expect(scrolls.at(-1)).toMatchObject({ behavior: "smooth", block: "nearest" });
  });

  it("com movimento reduzido, rola sem animação", async () => {
    reducedMotion = true;
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    fireEvent.keyDown(screen.getByRole("button", { name: "Roraima" }), { key: "Enter" });
    expect(scrolls.at(-1)).toMatchObject({ behavior: "auto" });
  });

  it("navegar com Tab pelos estados não tira o foco do mapa", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    const roraima = screen.getByRole("button", { name: "Roraima" });
    act(() => roraima.focus()); // o Tab do teclado
    expect(document.activeElement).toBe(roraima);
    expect(scrolls).toHaveLength(0);
    expect(screen.getByRole("region", { name: "Detalhe do estado" }).textContent).toMatch(/Roraima/);
  });

  it("legenda de intensidade: 4 faixas, cada uma com as amostras de cor e o texto", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });

    const faixas = screen.getByRole("list", { name: /Intensidade da cor/ });
    const itens = within(faixas).getAllByRole("listitem");
    expect(itens.map((i) => i.textContent)).toEqual(["até 5 pontos", "5 a 15 pontos", "15 a 30 pontos", "mais de 30 pontos"]);
    for (const item of itens) expect(item.querySelectorAll("svg rect")).toHaveLength(2); // vermelho e azul
  });

  it("antes da apuração (nenhum estado com dados) avisa e mantém o mapa cinza com a legenda", async () => {
    const data = resposta();
    for (const e of data.estados) e.resultado = null;
    api.apiFetch.mockResolvedValue(data);
    render(<MapaApuracao />);
    const mapa = await screen.findByRole("group", { name: /Mapa do Brasil/ });

    expect(screen.getByText("O mapa será preenchido conforme o TSE divulgar os resultados.")).toBeTruthy();
    const fills = new Set([...mapa.querySelectorAll("path[data-uf]")].map((p) => p.getAttribute("fill")));
    expect([...fills]).toEqual(["#e2e8f0"]);
    expect(screen.getByRole("list", { name: "Legenda do mapa" }).textContent).toMatch(/Sem dados/);
  });

  it("com algum estado apurado, não mostra o aviso de antes da apuração", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    await screen.findByRole("group", { name: /Mapa do Brasil/ });
    expect(screen.queryByText(/será preenchido conforme o TSE/)).toBeNull();
  });
});
