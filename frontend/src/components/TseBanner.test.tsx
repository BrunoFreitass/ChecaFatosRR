// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TseBanner } from "./TseBanner.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api.ts", () => ({ apiFetch: api.apiFetch }));

const TEXT = "Acompanhe a apuração oficial do 2º turno — fonte: TSE";

/** Espera a consulta e o setState dela (sem isso, "não aparece" passaria antes da resposta). */
async function settled() {
  await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function renderBanner() {
  render(
    <MemoryRouter>
      <TseBanner />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // Com chaves: uma função devolvida pelo beforeEach vira teardown no Vitest.
  api.apiFetch.mockReset();
});
afterEach(cleanup);

describe("TseBanner", () => {
  it("dentro da janela do painel, leva para a apuração", async () => {
    api.apiFetch.mockResolvedValue({ banner: true, texto: TEXT, apuracao: true });
    renderBanner();

    const link = await screen.findByRole("link", { name: TEXT });
    expect(link.getAttribute("href")).toBe("/apuracao");
    expect(api.apiFetch).toHaveBeenCalledWith("/tse/status", expect.objectContaining({ anonymous: true }));
  });

  it("fora da janela, não aparece", async () => {
    api.apiFetch.mockResolvedValue({ banner: false, texto: TEXT, apuracao: true });
    renderBanner();
    await settled();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("com a apuração desligada, não aparece", async () => {
    api.apiFetch.mockResolvedValue({ banner: true, texto: TEXT, apuracao: false });
    renderBanner();
    await settled();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("erro ao consultar não atrapalha a home", async () => {
    let rejected = false;
    api.apiFetch.mockImplementation(() => {
      rejected = true;
      return Promise.reject(new Error("rede"));
    });
    renderBanner();
    await settled();
    expect(rejected).toBe(true);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
