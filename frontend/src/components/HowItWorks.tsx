// "Como o ChecaFatosRR funciona" (home, abaixo do formulário). Texto aprovado pelo usuário em
// 19/10/2026, frase a frase conferida contra o código: mudou o comportamento, mude o texto.
export function HowItWorks() {
  return (
    <section aria-labelledby="como-funciona" className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 sm:p-6">
      <h2 id="como-funciona" className="text-xl font-bold">
        Como o ChecaFatosRR funciona
      </h2>
      <ol className="list-decimal space-y-2 pl-6 text-slate-800">
        <li>
          Você envia o link ou o texto. Se for um link de site, nosso sistema abre a página para ler
          o conteúdo. Links de redes sociais (como Instagram, WhatsApp e YouTube) não podem ser
          abertos; nesse caso, pedimos que você cole o texto. Em textos colados, dados pessoais como
          telefone, e-mail, CPF, CNPJ e chave Pix são mascarados antes da análise.
        </li>
        <li>
          Buscamos checagens já publicadas, em português, na ferramenta Google Fact Check Tools, que
          reúne verificações de agências de checagem e de veículos de imprensa.
        </li>
        <li>
          Uma inteligência artificial (Gemini, do Google) lê o conteúdo e as checagens encontradas e
          escreve uma explicação do contexto. Quando a IA está indisponível, mostramos só as
          checagens encontradas.
        </li>
        <li>
          Regras de segurança: só classificamos como “Verdadeiro” quando uma agência de checagem
          confirmou. Sem checagem de agência, só usamos “Falso” quando a análise tem confiança de
          pelo menos 70%, e também podemos indicar “Enganoso” ou “Fora de contexto”. Quando a
          confiança é baixa, mostramos “Ainda sem checagem” com os pontos de atenção.
        </li>
        <li>
          Mostramos as fontes: cada checagem encontrada aparece com o nome de quem publicou, a
          classificação e, quando disponíveis, a data e o link original. Se a busca nas checagens
          falhar, avisamos.
        </li>
      </ol>
      <p className="text-slate-700">
        <strong>Limites:</strong> a análise é automática e pode errar. Não fazemos checagem manual.
        Uma mesma análise pode ser reaproveitada por até 24 horas. Confira sempre as fontes.
      </p>
    </section>
  );
}
