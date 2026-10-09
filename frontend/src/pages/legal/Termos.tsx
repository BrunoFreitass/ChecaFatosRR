import { ContactEmail, LegalPage } from "./LegalPage.tsx";

export function Termos() {
  return (
    <LegalPage title="Termos de Uso">
      <h2>1. O que é o ChecaFatosRR</h2>
      <p>
        Um site gratuito e apartidário para ajudar a identificar desinformação nas eleições de
        2026, com foco em Roraima. Você cola o link de uma notícia e recebe uma análise automatizada
        e as checagens já publicadas por agências de checagem.
      </p>

      <h2>2. Limites da análise</h2>
      <ul>
        <li>A análise é automatizada (inteligência artificial) e pode errar.</li>
        <li>Ela não substitui as agências de checagem nem as informações oficiais do TSE.</li>
        <li>O site não apoia nem critica candidatos ou partidos.</li>
        <li>O ChecaFatosRR não tem ligação com o TSE nem com qualquer órgão público.</li>
      </ul>

      <h2>3. Sua conta</h2>
      <ul>
        <li>O login é feito somente com uma conta Google.</li>
        <li>Nos espaços públicos aparece só o seu apelido.</li>
        <li>Você é responsável pelo que publica.</li>
        <li>Você pode excluir sua conta a qualquer momento em "Minha conta".</li>
      </ul>

      <h2>4. Regras de conduta</h2>
      <p>Não é permitido publicar:</p>
      <ul>
        <li>palavrões, ofensas ou ataques pessoais;</li>
        <li>conteúdo sexual;</li>
        <li>discurso de ódio ou discriminação;</li>
        <li>incitação à violência;</li>
        <li>conteúdo que configure crime;</li>
        <li>propaganda, spam ou correntes;</li>
        <li>desinformação apresentada como fato.</li>
      </ul>

      <h2>5. Moderação</h2>
      <ul>
        <li>
          Os comentários passam por um filtro automático e por uma análise de inteligência
          artificial antes de serem publicados.
        </li>
        <li>Conteúdo que viola as regras é bloqueado, com um aviso explicando o motivo.</li>
        <li>
          Cada violação confirmada gera um registro (strike). Com 3 strikes, a conta fica
          suspensa por 24 horas. Com 5 strikes, a conta é banida.
        </li>
        <li>
          Comentários com 3 denúncias ficam ocultos até a revisão da moderação. O administrador
          pode rever qualquer decisão.
        </li>
      </ul>

      <h2>6. Anúncios</h2>
      <p>
        O site pode exibir anúncios para cobrir seus custos. Anúncios de conteúdo político ou
        eleitoral não são aceitos.
      </p>

      <h2>7. Contato</h2>
      <p>
        Dúvidas sobre estes termos: <ContactEmail />.
      </p>
    </LegalPage>
  );
}
