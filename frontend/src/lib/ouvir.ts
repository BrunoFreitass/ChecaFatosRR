// "Ouvir" do menu de acessibilidade: leitura em voz alta com a Web Speech API do próprio
// navegador. Só voz em português do Brasil e só voz LOCAL (localService): as vozes "de rede"
// (ex.: as "Google" do Chrome no computador) mandariam o texto ao fabricante do navegador, e a
// Política de Privacidade não muda — com elas, nada sai do aparelho.

/** Voz pt-BR instalada no aparelho (o Android usa "pt_BR"); sem ela, o botão fica desabilitado. */
export function findPtBrVoice(voices: readonly SpeechSynthesisVoice[]): SpeechSynthesisVoice | null {
  return voices.find((v) => v.localService && v.lang.replace("_", "-").toLowerCase() === "pt-br") ?? null;
}

// Escondidos: atributos, a classe "hidden" do Tailwind e o conteúdo de <details> fechado (ex.:
// "Ver em tabela": 27 linhas que a pessoa não abriu).
const SKIP =
  "[hidden], [aria-hidden='true'], .hidden, details:not([open]) > :not(summary), script, style, noscript, template, svg";

/** A seleção do usuário, se houver; senão o conteúdo principal. */
export function textToRead(doc: Document): string {
  return doc.getSelection()?.toString().trim() || mainText(doc);
}

/** O conteúdo principal (<main>), sem o que está escondido. */
export function mainText(doc: Document): string {
  const main = doc.getElementById("conteudo") ?? doc.querySelector("main");
  if (!main) return "";
  const copy = main.cloneNode(true) as HTMLElement;
  for (const node of copy.querySelectorAll(SKIP)) node.remove();
  // Blocos viram quebras: as frases de títulos e parágrafos não se juntam numa só.
  for (const block of copy.querySelectorAll("h1, h2, h3, h4, p, li, td, th, caption, summary, dt, dd")) {
    block.append(". ");
  }
  return (copy.textContent ?? "").replace(/(\s*\.\s*){2,}/g, ". ").replace(/\s+/g, " ").trim();
}

/** Partes de até `max` caracteres, cortadas no fim das frases (ou entre palavras). Fim de frase
 * é pontuação seguida de espaço: "87.25" e "1.234" não são cortados. */
export function splitForSpeech(text: string, max = 200): string[] {
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean) return [];
  const parts: string[] = [];
  let current = "";
  const add = (piece: string) => {
    const joined = current ? `${current} ${piece}` : piece;
    if (joined.length > max) return false;
    current = joined;
    return true;
  };
  for (const sentence of clean.split(/(?<=[.!?])\s+/)) {
    if (add(sentence)) continue;
    if (current) parts.push(current);
    current = "";
    if (add(sentence)) continue;
    for (const word of sentence.split(" ")) {
      if (add(word)) continue;
      if (current) parts.push(current);
      current = word;
    }
  }
  if (current) parts.push(current);
  return parts;
}
