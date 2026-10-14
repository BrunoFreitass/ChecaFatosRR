// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
              totalizado_em: null,
            },
    })),
  };
}

afterEach(() => {
  cleanup();
  api.apiFetch.mockReset();
});

describe("MapaApuracao", () => {
  it("desenha os 27 estados e a tabela acessível com 27 linhas", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);

    const mapa = await screen.findByRole("img", { name: /Mapa do Brasil/ });
    expect(mapa.querySelectorAll("path")).toHaveLength(27);
    const tabela = screen.getByRole("table");
    expect(within(tabela).getAllByRole("row")).toHaveLength(28); // cabeçalho + 27
    expect(within(tabela).getByText("Acre").closest("tr")?.textContent).toMatch(/Sem dados/);
  });

  it("tocar num estado mostra os percentuais e as urnas apuradas", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    render(<MapaApuracao />);
    const mapa = await screen.findByRole("img", { name: /Mapa do Brasil/ });

    fireEvent.click(mapa.querySelector('path[data-uf="rr"]') as Element);

    const detalhe = screen.getByRole("status");
    expect(detalhe.textContent).toMatch(/Roraima/);
    expect(detalhe.textContent).toMatch(/FLAVIO BOLSONARO/);
    expect(detalhe.textContent).toMatch(/60,50%/);
    expect(detalhe.textContent).toMatch(/87,25%/);
  });

  it("nunca escreve Eleito e a legenda é igual para os dois", async () => {
    api.apiFetch.mockResolvedValue(resposta());
    const { container } = render(<MapaApuracao />);
    await screen.findByRole("img", { name: /Mapa do Brasil/ });

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
    await screen.findByRole("img", { name: /Mapa do Brasil/ });

    expect(api.apiFetch).toHaveBeenCalledWith(
      "/tse/mapa",
      expect.objectContaining({ anonymous: true, signal: expect.any(AbortSignal) }),
    );
  });
});
