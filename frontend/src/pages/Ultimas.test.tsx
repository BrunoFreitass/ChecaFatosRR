// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CheckSummary } from "../lib/types.ts";
import { Ultimas } from "./Ultimas.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api.ts", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
  },
  apiFetch: api.apiFetch,
}));

afterEach(() => {
  cleanup();
  api.apiFetch.mockReset();
});

function item(overrides: Partial<CheckSummary> = {}): CheckSummary {
  return {
    id: "k7mqp9xwvrzt",
    domain: "folhabv.com.br",
    title: "Calendário do 2º turno é mantido em Roraima",
    veredito: "falso",
    created_at: new Date().toISOString(),
    total_agencias: 1,
    ...overrides,
  };
}

function renderList(items: CheckSummary[]) {
  api.apiFetch.mockResolvedValue({ items, next_cursor: null });
  const router = createMemoryRouter([{ path: "/ultimas", element: <Ultimas /> }], {
    initialEntries: ["/ultimas"],
  });
  render(<RouterProvider router={router} />);
}

describe("Últimas: checagens de agência (26/10)", () => {
  it('mostra o selo "Checagem de <agência>" e a classificação dela, sem veredito nosso', async () => {
    renderList([
      item({
        id: "ataxpy4sp8e4",
        domain: "estadao.com.br",
        title: "Homem que aparece em vídeo não foi preso por fazer Pix para 8 de ...",
        veredito: "nao_verificavel",
        total_agencias: 0,
        modo: "checagem_de_agencia",
        agencia: "Estadão",
        classificacao: "Enganoso",
      }),
    ]);
    expect(await screen.findByText("Checagem de Estadão")).toBeTruthy();
    expect(screen.getByText("Classificação da agência: Enganoso")).toBeTruthy();
    expect(screen.queryByText("Não verificável")).toBeNull();
    expect(screen.queryByText("Ainda sem checagem")).toBeNull();
  });

  it("link de agência sem classificação: só o selo, nada inventado", async () => {
    renderList([
      item({
        domain: "aosfatos.org",
        veredito: "nao_verificavel",
        total_agencias: 0,
        modo: "checagem_de_agencia",
        agencia: "Aos Fatos",
        classificacao: null,
      }),
    ]);
    expect(await screen.findByText("Checagem de Aos Fatos")).toBeTruthy();
    expect(screen.queryByText(/Classificação da agência/)).toBeNull();
  });

  it("item comum continua com o veredito de sempre", async () => {
    renderList([item()]);
    expect(await screen.findByText("Falso")).toBeTruthy();
    expect(screen.queryByText(/Checagem de /)).toBeNull();
  });
});
