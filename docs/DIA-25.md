# Roteiro do dia 25/10 (2º turno)

Para seguir sozinho, pelo celular. **Horários de Boa Vista** (1 hora a menos que Brasília).

**Endereços**

| O quê | Onde |
|---|---|
| Site | https://checafatosrr.pages.dev |
| API (saúde) | https://checafatosrr-api.onrender.com/health/ready |
| Painel do admin | site → Minha conta → **Painel do administrador** |
| Fila de moderação | site → Minha conta → **Fila de moderação** |
| Render | dashboard.render.com → `checafatosrr-api` |
| Situação do Render | https://status.render.com |
| Situação do Supabase | https://status.supabase.com |
| Resultado oficial | https://resultados.tse.jus.br |

---

## 1. Regras do dia

1. **Nenhum deploy no dia 25.** Não aperte "Manual Deploy" e não faça merge de nada.
2. Mude **só** as variáveis de ambiente que aparecem neste roteiro, e só na situação descrita.
   - Mudar uma variável no Render **reinicia** a API (cerca de 1 minuto, mesmo código).
   - Enquanto reinicia, o site pode demorar a responder. Isso é normal.
3. **Não aprove comentário político ofensivo só porque ele está na fila.** Na dúvida, remova.
4. Não mexa no `TSE_ELECTION_CODE`: deixe **sem definir** (o padrão já é `6258`) ou com `6258`.

---

## 2. Linha do tempo

### De manhã (até 10h)

- [ ] **UptimeRobot**: os monitores estão **verdes** (API `/health`, `/health/ready`, site)?
- [ ] Abra https://checafatosrr-api.onrender.com/health/ready
  - Esperado: `{"status":"ok","database":"ok","schema":"ok"}`.
  - Se demorar uns 50 segundos na primeira vez, a API estava "dormindo" (plano Free). Recarregue: a segunda vez é rápida.
- [ ] Abra o site e a página **Apuração**.
  - Esperado: "Aguardando os primeiros resultados do TSE" e o mapa **cinza**, com a frase "O mapa será preenchido conforme o TSE divulgar os resultados."
- [ ] **Render → Environment**: confira que **não** existe `TSE_ENABLED=false`, `TSE_MAPA_ENABLED=false`, nem `TSE_ELECTION_CODE` diferente de 6258.
- [ ] **Painel do admin**: veja a **cota do Gemini de hoje** (checagem e moderação) com bastante "Restante".

### Às 16h (fim da votação; começa o banner)

- [ ] O **banner** da home aparece sozinho às 16h ("Acompanhe a apuração oficial do 2º turno — fonte: TSE"). Não precisa fazer nada.
- [ ] Abra a **Apuração**: quando o TSE começar a divulgar, os números aparecem em até cerca de 1 minuto.
  - O mapa preenche os 27 estados em poucos minutos.
  - O horário "Atualizado em…" é o do **TSE**.

### Durante a divulgação (16h até a madrugada)

A cada 30–60 minutos:

- [ ] **UptimeRobot** verde.
- [ ] **Apuração**: o percentual de seções apuradas sobe e o "Atualizado em…" anda.
- [ ] **Render → Logs**: procure os nomes da seção 3 (principalmente `tse_fetch_failed`).
- [ ] **Fila de moderação**: há itens novos? (veja a seção 4)
- [ ] **Painel do admin**: cota do Gemini restante.

### De madrugada (fim da totalização)

- [ ] Quando o TSE terminar, a Apuração mostra "Totalização concluída pelo TSE" e, se for o caso, "Eleito" (só no Brasil, com o texto do TSE; o mapa nunca escreve "Eleito").
- [ ] Nada a desligar: o banner sai sozinho no fim do dia 26.

---

## 3. O que olhar e onde

**UptimeRobot**
- Avisa por e-mail ou aplicativo se a API ou o site caírem.
- Monitor da API `/health` (a cada 5 min, também mantém a API acordada) e `/health/ready` (a cada 60 min; confere banco e estrutura).

**Render (dashboard → `checafatosrr-api`)**
- **Events**: reinícios e deploys. No dia 25 **não deve aparecer deploy**. Reinício só se você mudou uma variável.
- **Metrics**: no plano Free o Render não mostra CPU nem memória. Para a saúde da API, use o UptimeRobot e os logs abaixo.
- **Logs**: digite o nome na busca.

| Nome no log | O que quer dizer | Preocupa? |
|---|---|---|
| `tse_fetch_ok` | Uma busca real no TSE deu certo | Não: é o normal |
| `tse_fetch_failed` / `tse_map_fetch_failed` | O TSE não respondeu numa busca | Só se repetir por mais de 10 min (seção 4) |
| `tse_refresh_failed` | A atualização em segundo plano falhou; o site segue com o último dado bom | Só se repetir por mais de 10 min |
| `tse_format_error` | O TSE mudou o formato do arquivo | **Sim** (seção 4, "TSE com dado errado") |
| `gemini_model_skipped` | Um modelo de IA estava indisponível e o próximo foi usado | Não, a menos que seja em todas as checagens |
| `moderation_ai_failed` | A IA de moderação não respondeu: o comentário **é publicado** e também vai para a fila, para você revisar | Sim, se repetir muito: revise a fila com frequência |
| `check_timing` | Tempo de cada checagem (diagnóstico) | Não |

**Painel do admin**
- Cota do Gemini de hoje (checagem e moderação): usadas, limite e restante.
- Fila pendente.
- Checagens, comentários e denúncias do dia.

**Fila de moderação**
- Comentários ocultos por denúncias (não aparecem para ninguém até você decidir).
- Comentários que a IA não conseguiu avaliar: **esses já estão publicados**. Revise primeiro e remova os ruins.
- Botões: **Aprovar**, **Remover**, **Banir** (e, nos bloqueados pela IA, **Publicar**, **Confirmar strike**, **Descartar**).

---

## 4. Se acontecer X → faça Y

| Se acontecer… | Faça… |
|---|---|
| **TSE fora ou lento** (`tse_fetch_failed` repetindo; o "Atualizado em…" para de andar) | **Nada.** O site continua mostrando o **último resultado bom**, com o horário do TSE, e volta sozinho quando o TSE voltar. A página tem o link para o resultado oficial. Não mude variáveis. |
| **TSE com dado errado** (`tse_format_error`, números estranhos) | Render → Environment → `TSE_ENABLED=false` → salvar. A Apuração mostra um aviso com o **link do TSE**. Para voltar: apague a variável ou ponha `true`. |
| **Mapa com problema** (cores erradas, travado, erro) | Render → Environment → `TSE_MAPA_ENABLED=false` → salvar. O mapa some; os números do Brasil e de Roraima continuam. |
| **Cota do Gemini esgotada** (painel: "Restante" 0) | **Nada: é esperado.** As checagens passam a mostrar só o que as agências publicaram ("só agências"). A cota volta à meia-noite do horário do Pacífico (**3h de Boa Vista**). Não aumente limites. |
| **Muitos comentários na fila** | Comece pelos que **já estão publicados** (a IA não avaliou): remova os ruins. Depois os "Oculto por denúncias": esses não aparecem para ninguém e podem esperar. Na dúvida, **Remover**. Não aprove ofensa política só para esvaziar a fila. |
| **Ataque ou spam de comentários** | 1) Na fila, no item do spammer, use **Banir**: a conta é banida (não comenta mais) e o comentário do item é removido. 2) Se vierem de muitas contas: Render → Environment → `COMMENT_MIN_INTERVAL_SECONDS=300` (5 min entre comentários; o padrão é 30) e/ou `COMMENTS_DAILY_LIMIT=3` (o padrão é 20). 3) **Não existe chave para desligar os comentários** sem mudar o código. |
| **API fora** (UptimeRobot vermelho; site sem dados) | 1) Abra `/health/ready`. Se demorar ~50 s e depois responder, a API estava dormindo: tudo certo. 2) Veja https://status.render.com: se houver incidente, é do Render, e é só **esperar**. 3) Render → Events/Logs: houve reinício? erro? 4) Se `/health/ready` responder `"database"` diferente de `ok`, veja https://status.supabase.com. **Não faça deploy, não faça rollback e não mude código.** |
| **Site fora** (a página não abre, mas a API responde) | Cloudflare Pages: veja https://www.cloudflarestatus.com. Esperar. |

---

## 5. Depois da eleição

- **Banner**: sai sozinho no fim do dia 26/10 (`TSE_BANNER_FIM` = 27/10 00:00 de Boa Vista). Nada a fazer.
- **Apuração e mapa**: podem continuar no ar com o resultado final.
  - Se quiser tirar a página: `TSE_ENABLED=false`.
  - Se quiser tirar só o mapa: `TSE_MAPA_ENABLED=false`.
- **Comentários e checagens**: continuam normalmente.
- **Variáveis mudadas no dia 25** (ex.: limite de comentários): volte ao padrão apagando a variável.
- **Ideias para depois da eleição** (no plano, `.claude/plans/checafatosrr.plan.md` §14):
  - função no banco para deixar a checagem mais rápida;
  - API e banco na mesma região.
- **Infraestrutura** (plano §15): Render Free com UptimeRobot e uma instância só. Rever depois da eleição.
