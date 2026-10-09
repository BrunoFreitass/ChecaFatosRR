import { ContactEmail, LegalPage } from "./LegalPage.tsx";

export function Privacidade() {
  return (
    <LegalPage title="Política de Privacidade">
      <p>
        Esta política explica quais dados o ChecaFatosRR trata, por quê e quais são os seus
        direitos, conforme a Lei Geral de Proteção de Dados (LGPD, Lei 13.709/2018).
      </p>

      <h2>1. Dados que coletamos</h2>
      <ul>
        <li>
          <strong>Da sua conta Google:</strong> o identificador da conta e o e-mail. Eles servem
          só para o login e nunca são exibidos no site. Não coletamos CPF, telefone nem endereço.
        </li>
        <li>
          <strong>Que você informa:</strong> o apelido, a data em que aceitou estes termos e o que
          você publica (comentários, denúncias, links e mensagens enviados para checagem).
        </li>
        <li>
          <strong>Checagens:</strong> o resultado de cada checagem fica público, sem o seu apelido
          nem qualquer ligação visível com você.
          <ul>
            <li>Links checados aparecem na página “Últimas checagens”.</li>
            <li>
              Mensagens coladas não são listadas nem aparecem em buscadores: só quem recebe o link
              do resultado consegue ver.
            </li>
            <li>
              Antes de guardar uma mensagem colada, escondemos automaticamente telefones, e-mails,
              CPF e chaves Pix. Mesmo assim, não cole conversas pessoais.
            </li>
          </ul>
        </li>
        <li>
          <strong>De moderação:</strong>
          <ul>
            <li>registros de violações e strikes;</li>
            <li>o texto bloqueado, guardado por até 90 dias para revisão;</li>
            <li>
              para contas banidas, um código irreversível (hash) gerado a partir da conta Google,
              que impede o retorno com a mesma conta.
            </li>
          </ul>
        </li>
        <li>
          <strong>Técnicos:</strong> registros de acesso (data, página e erros), usados para
          segurança e para corrigir problemas.
        </li>
      </ul>

      <h2>2. Para que usamos</h2>
      <ul>
        <li>Permitir o login e mostrar seu apelido.</li>
        <li>Fazer as checagens de links.</li>
        <li>Moderar comentários e aplicar as regras.</li>
        <li>Evitar abusos e manter o site seguro.</li>
      </ul>
      <p>
        As bases legais são a execução do serviço que você solicitou e o legítimo interesse em
        manter o espaço seguro (LGPD, art. 7º, incisos V e IX).
      </p>

      <h2>3. Com quem compartilhamos</h2>
      <p>Só com os serviços necessários para o site funcionar:</p>
      <ul>
        <li>Supabase: banco de dados e login, com servidores em São Paulo.</li>
        <li>Google Cloud: servidor da aplicação.</li>
        <li>Cloudflare: hospedagem do site.</li>
        <li>
          Google Gemini (inteligência artificial): recebe o texto dos links checados, das mensagens
          coladas (já sem telefones, e-mails, CPF e chaves Pix) e dos comentários, para análise e
          moderação. Seus dados de conta não são enviados. No plano gratuito, o Google pode usar
          esse conteúdo para melhorar os próprios produtos.
        </li>
        <li>
          Google Fact Check Tools: recebe um trecho curto do conteúdo (o título ou a primeira
          frase) para buscar checagens já publicadas por agências.
        </li>
      </ul>
      <p>Não vendemos dados.</p>

      <h2>4. Por quanto tempo guardamos</h2>
      <ul>
        <li>Os dados da conta ficam guardados enquanto ela existir.</li>
        <li>Textos bloqueados pela moderação são guardados por até 90 dias.</li>
        <li>
          Quando você exclui sua conta, apagamos o apelido, os comentários e o histórico. A única
          exceção é o código de banimento, no caso de contas banidas.
        </li>
        <li>
          Os resultados das checagens continuam no site depois da exclusão da conta, sem nenhuma
          ligação com você. Se uma mensagem checada tiver dados seus, peça a remoção pelo e-mail
          do encarregado (item 6).
        </li>
      </ul>

      <h2>5. Seus direitos</h2>
      <ul>
        <li>
          Você pode confirmar quais dados temos, acessá-los, corrigi-los, pedir que sejam apagados
          e revogar seu consentimento.
        </li>
        <li>Para excluir sua conta, use "Minha conta" a qualquer momento.</li>
        <li>
          Para os demais pedidos, escreva para <ContactEmail />.
        </li>
      </ul>

      <h2>6. Encarregado pelo tratamento de dados</h2>
      <p>
        O contato do encarregado pelo tratamento de dados pessoais (LGPD, art. 41) é{" "}
        <ContactEmail />. Respondemos aos pedidos em até 15 dias.
      </p>

      <h2>7. Armazenamento no navegador</h2>
      <p>
        Guardamos a sua sessão de login no armazenamento local do navegador, para que você
        continue conectado. Não usamos cookies de rastreamento nem de publicidade.
      </p>
    </LegalPage>
  );
}
