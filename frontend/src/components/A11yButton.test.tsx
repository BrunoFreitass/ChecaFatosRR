// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { A11Y_KEY } from "../lib/a11y.ts";
import { A11yButton } from "./A11yButton.tsx";

type FakeUtterance = {
  text: string;
  lang: string;
  voice: unknown;
  onend?: () => void;
  onerror?: (event: { error: string }) => void;
};

function fakeSynth(langs: string[]) {
  const spoken: FakeUtterance[] = [];
  const synth = {
    getVoices: () => langs.map((lang) => ({ lang, name: lang, localService: true })),
    speak: vi.fn((u: FakeUtterance) => spoken.push(u)),
    cancel: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  return { synth, spoken };
}

function install(langs: string[] | null) {
  if (langs === null) {
    Reflect.deleteProperty(window, "speechSynthesis");
    return null;
  }
  const fake = fakeSynth(langs);
  Object.defineProperty(window, "speechSynthesis", { value: fake.synth, configurable: true });
  vi.stubGlobal(
    "SpeechSynthesisUtterance",
    class {
      lang = "";
      voice: unknown = null;
      constructor(readonly text: string) {}
    },
  );
  return fake;
}

/** Troca de página sem tocar na página (navegação do próprio app, como um link seguido pelo teclado). */
function GoElsewhere() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => void navigate("/outra")}>
        ir para outra página
      </button>
      <button type="button" onClick={() => void navigate(-1)}>
        voltar
      </button>
    </>
  );
}

function renderButton() {
  render(
    <MemoryRouter initialEntries={["/"]}>
      <A11yButton />
      <GoElsewhere />
    </MemoryRouter>,
  );
}

const menuButton = () => screen.getByRole("button", { name: /^Acessibilidade/ });

/** Toque/clique no botão do menu: pointerdown (captura a seleção antes de o toque desfazê-la) + click. */
function pressMenu() {
  fireEvent.pointerDown(menuButton());
  fireEvent.click(menuButton());
}

async function openPanel() {
  renderButton();
  pressMenu();
  return screen.findByRole("dialog", { name: "Acessibilidade" });
}

function select(id: string) {
  const range = document.createRange();
  range.selectNodeContents(document.getElementById(id) as HTMLElement);
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

const PAGE = '<main id="conteudo"><p>Texto da página inteira.</p><p id="trecho">Só este trecho.</p></main>';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.className = "";
  install(["pt-BR"]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(window, "speechSynthesis");
  document.body.innerHTML = "";
});

describe("Botão de acessibilidade", () => {
  it("abre o painel, Esc fecha e o foco volta ao botão", async () => {
    const dialog = await openPanel();
    const button = screen.getByRole("button", { name: /Acessibilidade/ });

    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(button);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("o foco fica preso no painel (Tab no último volta ao primeiro; Shift+Tab no primeiro vai ao último)", async () => {
    await openPanel();
    const fechar = screen.getByRole("button", { name: "Fechar o menu de acessibilidade" });
    const restaurar = screen.getByRole("button", { name: "Restaurar padrão" });

    restaurar.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(fechar);

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(restaurar);
  });
});

describe("Painel: opções", () => {
  it("cada opção liga e desliga a classe no <html> e é salva no navegador", async () => {
    await openPanel();
    const options: [string, string][] = [
      ["Alto contraste", "a11y-contraste"],
      ["Inverter cores", "a11y-inverter"],
      ["Tons de cinza (monocromático)", "a11y-cinza"],
      ["Sublinhar links", "a11y-links"],
      ["Mais espaço entre linhas e letras", "a11y-espaco"],
      ["Pausar animações", "a11y-pausar"],
      ["Cursor grande", "a11y-cursor"],
    ];
    for (const [label, cls] of options) {
      const toggle = screen.getByRole("button", { name: new RegExp(`^${label.replace(/[()]/g, "\\$&")}`) });
      fireEvent.click(toggle);
      expect(toggle.getAttribute("aria-pressed")).toBe("true");
      expect(document.documentElement.classList.contains(cls)).toBe(true);
      expect(JSON.parse(localStorage.getItem(A11Y_KEY) ?? "{}")[cls.slice(5)]).toBe(true);

      fireEvent.click(toggle);
      expect(toggle.getAttribute("aria-pressed")).toBe("false");
      expect(document.documentElement.classList.contains(cls)).toBe(false);
    }
  });

  it("fonte: A+ até o máximo (aria-disabled, sem perder o foco) e A− até o mínimo", async () => {
    await openPanel();
    const mais = screen.getByRole("button", { name: "Aumentar o texto" });
    const menos = screen.getByRole("button", { name: "Diminuir o texto" });

    fireEvent.click(mais);
    fireEvent.click(mais);
    fireEvent.click(mais);
    expect(document.documentElement.classList.contains("a11y-fonte-3")).toBe(true);
    expect(mais.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(mais); // no limite, nada muda
    expect(document.documentElement.classList.contains("a11y-fonte-3")).toBe(true);
    expect(screen.getByText("150%")).toBeTruthy();

    for (let i = 0; i < 4; i++) fireEvent.click(menos);
    expect(document.documentElement.classList.contains("a11y-fonte-menos")).toBe(true);
    expect(menos.getAttribute("aria-disabled")).toBe("true");
  });

  it("alto contraste e inverter cores se excluem: ligar um desliga o outro (sem página branca)", async () => {
    await openPanel();
    const contraste = screen.getByRole("button", { name: /^Alto contraste/ });
    const inverter = screen.getByRole("button", { name: /^Inverter cores/ });

    fireEvent.click(contraste);
    fireEvent.click(inverter);
    expect(inverter.getAttribute("aria-pressed")).toBe("true");
    expect(contraste.getAttribute("aria-pressed")).toBe("false");
    expect(document.documentElement.classList.contains("a11y-contraste")).toBe(false);
    expect(document.documentElement.classList.contains("a11y-inverter")).toBe(true);

    fireEvent.click(contraste);
    expect(contraste.getAttribute("aria-pressed")).toBe("true");
    expect(inverter.getAttribute("aria-pressed")).toBe("false");
    expect(document.documentElement.classList.contains("a11y-inverter")).toBe(false);
    expect(JSON.parse(localStorage.getItem(A11Y_KEY) ?? "{}")).toMatchObject({ contraste: true, inverter: false });
  });

  it("tons de cinza mostra o aviso do “Ver em tabela”", async () => {
    await openPanel();
    fireEvent.click(screen.getByRole("button", { name: /^Tons de cinza/ }));
    expect(screen.getByText(/Ver em tabela/)).toBeTruthy();
  });

  it("restaurar padrão tira todas as classes e salva o padrão", async () => {
    await openPanel();
    fireEvent.click(screen.getByRole("button", { name: /^Alto contraste/ }));
    fireEvent.click(screen.getByRole("button", { name: "Aumentar o texto" }));

    fireEvent.click(screen.getByRole("button", { name: "Restaurar padrão" }));

    expect(document.documentElement.className).toBe("");
    expect(JSON.parse(localStorage.getItem(A11Y_KEY) ?? "{}")).toMatchObject({ fonte: 0, contraste: false });
  });

  it("abre já com as preferências salvas", async () => {
    localStorage.setItem(A11Y_KEY, JSON.stringify({ fonte: 2, links: true }));
    await openPanel();
    expect(screen.getByText("125%")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Sublinhar links/ }).getAttribute("aria-pressed")).toBe("true");
  });
});

describe("Painel: Ouvir", () => {
  it("com voz pt-BR, lê o conteúdo principal em partes; Parar cancela", async () => {
    const fake = install(["en-US", "pt-BR"]);
    const main = document.createElement("main");
    main.id = "conteudo";
    main.innerHTML = "<h1>Apuração</h1><p>Resultado oficial.</p>";
    document.body.append(main);
    await openPanel();

    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text).join(" ")).toContain("Resultado oficial.");
    expect(fake?.spoken.every((u) => u.lang === "pt-BR")).toBe(true);
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Pausar" }));
    expect(fake?.synth.pause).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    expect(fake?.synth.resume).toHaveBeenCalled();

    fake?.synth.cancel.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "Parar" }));
    expect(fake?.synth.cancel).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Parar" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText("Leitura parada.")).toBeTruthy();

    // A fala cancelada termina depois (onend atrasado): não muda o estado nem o aviso.
    act(() => fake?.spoken.at(-1)?.onend?.());
    expect(screen.queryByText("Leitura concluída.")).toBeNull();
  });

  it("fim da leitura volta ao estado inicial", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", '<main id="conteudo"><p>Curto.</p></main>');
    await openPanel();
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    act(() => fake?.spoken.at(-1)?.onend?.());

    expect(screen.getByText("Leitura concluída.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Parar" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("Ouvir depois de Pausar: cancela e retoma o motor (no Chrome, sem resume() a nova fala fica muda)", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", '<main id="conteudo"><p>Um texto.</p></main>');
    await openPanel();
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));
    fireEvent.click(screen.getByRole("button", { name: "Pausar" }));
    fake?.synth.cancel.mockClear();
    fake?.synth.resume.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.synth.cancel).toHaveBeenCalledTimes(1);
    expect(fake?.synth.resume).toHaveBeenCalledTimes(1);
    expect(fake?.synth.cancel.mock.invocationCallOrder[0]).toBeLessThan(
      fake?.synth.resume.mock.invocationCallOrder[0] ?? 0,
    );
    expect(screen.getByRole("button", { name: "Pausar" }).getAttribute("aria-disabled")).toBe("false");
  });

  it("sem texto para ler: avisa e volta ao estado inicial (mesmo se lia algo antes)", async () => {
    install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", '<main id="conteudo"><p>Algo.</p></main>');
    await openPanel();
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));
    (document.getElementById("conteudo") as HTMLElement).innerHTML = "";

    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(screen.getByText("Não há texto para ler nesta página.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Parar" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("erro de verdade no meio da fila para tudo; interrupção pelo próprio cancelamento não", async () => {
    const fake = install(["pt-BR"]);
    const long = Array.from({ length: 6 }, (_, i) => `Frase número ${i} ${"palavra ".repeat(8)}fim.`).join(" ");
    document.body.insertAdjacentHTML("beforeend", `<main id="conteudo"><p>${long}</p></main>`);
    await openPanel();
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));
    expect(fake?.spoken.length ?? 0).toBeGreaterThan(1);

    act(() => fake?.spoken[0]?.onerror?.({ error: "interrupted" }));
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();

    fake?.synth.cancel.mockClear();
    act(() => fake?.spoken[0]?.onerror?.({ error: "synthesis-failed" }));
    expect(fake?.synth.cancel).toHaveBeenCalledTimes(1);
    expect(screen.getByText("A leitura foi interrompida.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Parar" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("lê o trecho selecionado na página (não o conteúdo inteiro)", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML(
      "beforeend",
      '<main id="conteudo"><p>Texto da página inteira.</p><p id="trecho">Só este trecho.</p></main>',
    );
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("trecho") as HTMLElement);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    await openPanel();

    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text)).toEqual(["Só este trecho."]);
    expect(screen.getByText("Lendo o trecho selecionado.")).toBeTruthy();
    window.getSelection()?.removeAllRanges();
  });

  it("reabrir sem seleção lê a página (a seleção da abertura anterior não fica guardada)", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", PAGE);
    select("trecho");
    await openPanel();
    fireEvent.keyDown(document, { key: "Escape" });
    window.getSelection()?.removeAllRanges();

    pressMenu();
    await screen.findByRole("dialog", { name: "Acessibilidade" });
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text).join(" ")).toContain("Texto da página inteira.");
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();
    expect(screen.queryByText("Lendo o trecho selecionado.")).toBeNull();
  });

  it("trocar de página com o painel aberto limpa a seleção capturada", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", PAGE);
    select("trecho");
    await openPanel();
    window.getSelection()?.removeAllRanges();

    fireEvent.click(screen.getByRole("button", { name: "ir para outra página" })); // sem pointerdown
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text).join(" ")).toContain("Texto da página inteira.");
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();
  });

  it("ir para outra página e voltar (painel aberto) não ressuscita a seleção capturada", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", PAGE);
    select("trecho");
    await openPanel();
    window.getSelection()?.removeAllRanges();

    fireEvent.click(screen.getByRole("button", { name: "ir para outra página" }));
    fireEvent.click(screen.getByRole("button", { name: "voltar" }));
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text).join(" ")).toContain("Texto da página inteira.");
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();
  });

  it("toque no botão sem click não vaza a seleção para uma abertura posterior pelo teclado", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", PAGE);
    const now = vi.spyOn(performance, "now").mockReturnValue(1000);
    select("trecho");
    renderButton();
    fireEvent.pointerDown(menuButton()); // arrastou para fora: não houve click
    window.getSelection()?.removeAllRanges();

    now.mockReturnValue(10_000); // bem depois, abre pelo teclado (Enter = click sem pointerdown)
    fireEvent.click(menuButton());
    await screen.findByRole("dialog", { name: "Acessibilidade" });
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text).join(" ")).toContain("Texto da página inteira.");
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();
    now.mockRestore();
  });

  it("tocar na página fora do painel descarta a seleção capturada na abertura", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", PAGE);
    select("trecho");
    await openPanel();
    window.getSelection()?.removeAllRanges();

    fireEvent.pointerDown(document.getElementById("conteudo") as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text).join(" ")).toContain("Texto da página inteira.");
    expect(screen.getByText("Lendo o conteúdo da página.")).toBeTruthy();
  });

  it("seleção desfeita pelo toque no botão (celular) ainda é lida nessa abertura", async () => {
    const fake = install(["pt-BR"]);
    document.body.insertAdjacentHTML("beforeend", PAGE);
    select("trecho");
    renderButton();
    fireEvent.pointerDown(menuButton());
    window.getSelection()?.removeAllRanges(); // o toque desfez a seleção antes do click
    fireEvent.click(menuButton());
    await screen.findByRole("dialog", { name: "Acessibilidade" });

    fireEvent.click(screen.getByRole("button", { name: "Ouvir" }));

    expect(fake?.spoken.map((u) => u.text)).toEqual(["Só este trecho."]);
    expect(screen.getByText("Lendo o trecho selecionado.")).toBeTruthy();
  });

  it("sem voz pt-BR: aviso e botão Ouvir desabilitado", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    install(["pt-PT", "en-US"]);
    await openPanel();

    act(() => {
      vi.advanceTimersByTime(2500);
    });

    expect((screen.getByRole("button", { name: "Ouvir" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/não tem voz em português do Brasil/)).toBeTruthy();
  });

  it("navegador sem Web Speech: aviso e botão desabilitado na hora", async () => {
    install(null);
    await openPanel();

    expect((screen.getByRole("button", { name: "Ouvir" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/não tem voz em português do Brasil/)).toBeTruthy();
  });

  it("fechar o menu encerra a leitura", async () => {
    const fake = install(["pt-BR"]);
    await openPanel();
    fake?.synth.cancel.mockClear();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(fake?.synth.cancel).toHaveBeenCalled();
  });
});
