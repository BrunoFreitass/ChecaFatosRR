// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { VerdictBadge } from "./VerdictBadge.tsx";

afterEach(cleanup);

describe("VerdictBadge", () => {
  it("Ainda sem checagem: cinza neutro, sem o texto 'Não verificável'", () => {
    render(<VerdictBadge verdict="nao_verificavel" awaitingCheck />);
    const badge = screen.getByText("Ainda sem checagem").parentElement;
    expect(badge?.className).toContain("bg-slate-100");
    expect(screen.queryByText("Não verificável")).toBeNull();
  });

  it("Não verificável com checagem de agência continua com o rótulo normal", () => {
    render(<VerdictBadge verdict="nao_verificavel" />);
    expect(screen.getByText("Não verificável")).toBeTruthy();
  });

  it("o modo neutro só vale para Não verificável", () => {
    render(<VerdictBadge verdict="falso" awaitingCheck />);
    expect(screen.getByText("Falso")).toBeTruthy();
    expect(screen.queryByText("Ainda sem checagem")).toBeNull();
  });
});
