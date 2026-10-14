// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Privacidade } from "./Privacidade.tsx";

const api = vi.hoisted(() => ({ apiFetch: vi.fn(async () => ({ version: "2026-10-14" })) }));
vi.mock("../../lib/api.ts", () => ({ apiFetch: api.apiFetch }));

afterEach(() => {
  cleanup();
});

describe("Política de Privacidade", () => {
  it("informa as estatísticas de visitas do Cloudflare Web Analytics, sem cookies", () => {
    render(
      <MemoryRouter>
        <Privacidade />
      </MemoryRouter>,
    );

    const item = screen.getByText(/Cloudflare Web Analytics/);
    expect(item.textContent).toMatch(/sem cookies/);
    expect(item.textContent).toMatch(/contagens agregadas/);
  });
});
