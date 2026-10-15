// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { findPtBrVoice, splitForSpeech, textToRead } from "./ouvir.ts";

function voice(lang: string, name = lang, localService = true): SpeechSynthesisVoice {
  return { lang, name, default: false, localService, voiceURI: name } as SpeechSynthesisVoice;
}

afterEach(() => {
  document.body.innerHTML = "";
  window.getSelection()?.removeAllRanges();
});

describe("Ouvir: voz", () => {
  it("escolhe a voz em português do Brasil (pt-BR ou pt_BR)", () => {
    expect(findPtBrVoice([voice("en-US"), voice("pt-BR", "Luciana")])?.name).toBe("Luciana");
    expect(findPtBrVoice([voice("pt_BR", "Android")])?.name).toBe("Android");
  });

  it("sem voz pt-BR (só pt-PT ou nenhuma), não há voz: o botão fica desabilitado", () => {
    expect(findPtBrVoice([voice("pt-PT"), voice("en-US")])).toBeNull();
    expect(findPtBrVoice([])).toBeNull();
  });

  it("voz de rede (o texto iria ao fabricante do navegador) não serve: só voz local", () => {
    expect(findPtBrVoice([voice("pt-BR", "Google português do Brasil", false)])).toBeNull();
    expect(
      findPtBrVoice([voice("pt-BR", "Google português do Brasil", false), voice("pt-BR", "Maria")])?.name,
    ).toBe("Maria");
  });
});

describe("Ouvir: o que ler", () => {
  it("lê a seleção, se houver", () => {
    document.body.innerHTML =
      '<main id="conteudo"><p id="a">Texto da página inteira.</p><p id="b">Só este trecho.</p></main>';
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("b") as HTMLElement);
    window.getSelection()?.addRange(range);

    expect(textToRead(document)).toBe("Só este trecho.");
  });

  it("sem seleção, lê o conteúdo principal (não o menu nem o rodapé)", () => {
    document.body.innerHTML =
      '<header>Menu</header><main id="conteudo"><h1>Apuração</h1><p>Resultado oficial.</p></main><footer>Rodapé</footer>';

    const text = textToRead(document);

    expect(text).toContain("Apuração");
    expect(text).toContain("Resultado oficial.");
    expect(text).not.toContain("Menu");
    expect(text).not.toContain("Rodapé");
  });

  it("partes escondidas (hidden, aria-hidden) não são lidas", () => {
    document.body.innerHTML =
      '<main id="conteudo"><p>Visível.</p><p hidden>Escondido.</p><span aria-hidden="true">Ícone</span></main>';

    const text = textToRead(document);

    expect(text).toContain("Visível.");
    expect(text).not.toContain("Escondido.");
    expect(text).not.toContain("Ícone");
  });
});

describe("Ouvir: o que não se vê não é lido", () => {
  it("pula a classe hidden e o conteúdo de <details> fechado (a tabela do mapa)", () => {
    document.body.innerHTML =
      '<main id="conteudo"><p>Mapa.</p><p class="hidden">Só no computador.</p>' +
      "<details><summary>Ver em tabela</summary><table><tr><td>Acre</td></tr></table></details></main>";

    const text = textToRead(document);

    expect(text).toContain("Ver em tabela");
    expect(text).not.toContain("Só no computador");
    expect(text).not.toContain("Acre");
  });
});

describe("Ouvir: partes curtas", () => {
  it("não corta números com ponto (87.25, 1.234) no meio", () => {
    const text = `${"palavra ".repeat(20)}87.25 de 1.234 urnas. Outra frase.`;
    const parts = splitForSpeech(text, 200);
    expect(parts.some((p) => p.includes("87.25 de 1.234 urnas."))).toBe(true);
  });

  it("quebra textos longos em frases (alguns navegadores cortam falas longas)", () => {
    const long = "Primeira frase curta. " + "palavra ".repeat(80) + "fim. Última frase!";

    const parts = splitForSpeech(long, 200);

    expect(parts.every((p) => p.length <= 200)).toBe(true);
    expect(parts.join(" ").replace(/\s+/g, " ")).toBe(long.trim().replace(/\s+/g, " "));
    expect(parts[0]).toBe("Primeira frase curta.");
  });

  it("texto vazio não gera fala", () => {
    expect(splitForSpeech("   ", 200)).toEqual([]);
  });
});
