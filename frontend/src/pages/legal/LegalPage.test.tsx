// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LegalPage } from "./LegalPage.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../../lib/api.ts", () => ({ apiFetch: api.apiFetch }));

afterEach(() => {
  cleanup();
  api.apiFetch.mockReset();
});

describe("LegalPage", () => {
  it("mostra a data da versão vigente informada pelo servidor", async () => {
    api.apiFetch.mockResolvedValue({ version: "2026-10-13" });

    render(<LegalPage title="Termos de Uso">texto</LegalPage>);

    expect(await screen.findByText(/Versão de 13\/10\/2026/)).toBeTruthy();
    expect(screen.getByRole("status").textContent).toMatch(/13\/10\/2026/);
    expect(api.apiFetch).toHaveBeenCalledWith(
      "/legal/version",
      expect.objectContaining({ anonymous: true }),
    );
  });

  it("sem resposta do servidor, não inventa uma data", async () => {
    api.apiFetch.mockRejectedValue(new Error("rede"));

    render(<LegalPage title="Termos de Uso">texto</LegalPage>);

    expect(await screen.findByText(/Versão vigente indisponível no momento/)).toBeTruthy();
    expect(screen.queryByText(/Versão de \d/)).toBeNull();
  });
});
