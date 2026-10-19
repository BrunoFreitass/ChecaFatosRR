// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { HowItWorks } from "./HowItWorks.tsx";

afterEach(cleanup);

describe("Como o ChecaFatosRR funciona", () => {
  it("é uma seção com título e uma lista ordenada de 5 passos, mais os limites", () => {
    render(<HowItWorks />);
    const section = screen.getByRole("region", { name: "Como o ChecaFatosRR funciona" });
    const list = within(section).getByRole("list");
    expect(list.tagName).toBe("OL");
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    expect(section.textContent).toContain("Limites:");
    expect(section.textContent).toContain("Uma mesma análise pode ser reaproveitada por até 24 horas.");
  });

  it("traz as frases conferidas contra o código (redes sociais, 70%, fontes, falha da busca)", () => {
    render(<HowItWorks />);
    const text = screen.getByRole("region", { name: "Como o ChecaFatosRR funciona" }).textContent ?? "";
    expect(text).toContain("Links de redes sociais (como Instagram, WhatsApp e YouTube) não podem ser abertos");
    expect(text).toContain("telefone, e-mail, CPF, CNPJ e chave Pix são mascarados antes da análise");
    expect(text).toContain("confiança de pelo menos 70%");
    expect(text).toContain("Quando a IA está indisponível, mostramos só as checagens encontradas.");
    expect(text).toContain("Se a busca nas checagens falhar, avisamos.");
  });
});
