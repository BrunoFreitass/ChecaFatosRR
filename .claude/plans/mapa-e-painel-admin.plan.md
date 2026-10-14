# Plano: mapa do Brasil na Apuração (A) e painel mínimo do admin (B)

**Origem:** pedido do usuário em 14/10/2026. As duas funcionalidades são **exceções ao congelamento**, aprovadas pelo usuário (ver checafatosrr.plan.md §14).
**Prioridade:** A antes de B. Se o prazo apertar, B é cortado antes do mapa.
**Prazos:** A pronto e validado no ensaio (`TSE_ELECTION_CODE=6257`) até **18/10**. B até **20/10**.
**Fora de escopo:** Gemini e prompt (congelado a partir de 22/10). Nenhum deploy em 25/10.
**Situação:** APROVADO pelo usuário em 14/10/2026, com as decisões abaixo.

## Decisões do usuário (14/10/2026)
1. **Mapa: Natural Earth** (Admin 1 – States, Provinces, 1:50m; domínio público, "No permission is needed to use Natural Earth"). A fonte e a licença ficam registradas no código (cabeçalho do `mapaUf.ts` e do script) e no rodapé do mapa.
2. **Tokens:** gravados no resultado da checagem (`checks.result`), sem migration. A moderação fica de fora nesta versão.
3. **Cores por número de candidato:** Lula = 13 = vermelho. **Não assumir o número do Flávio.** Os números e nomes são lidos do arquivo oficial do TSE (no ensaio com 6257 e no 6258, quando existir). O padrão de `TSE_MAPA_CORES` é montado a partir do que o TSE publica, e os números lidos são mostrados ao usuário. Candidato sem cor configurada fica em cinza neutro.
4. **Coleta:** com 1 só visitante (polling de 60 s), os 27 estados se atualizam em até ~3 min. São **até 9 estados vencidos por GET**, buscados em paralelo com timeout curto (5 s). Continua a trava de no máximo 1 busca por estado a cada 2 min, mais o single-flight. **Uma falha do TSE num estado não derruba a resposta dos outros**: o estado com falha mantém o último resumo bom, ou fica sem dado.

## Ordem de execução (aprovada)
- **PR 1 (14/10):** linha sobre o Cloudflare Web Analytics na Política de Privacidade, `CURRENT_TERMS_VERSION` com a data do deploy, CSP (`script-src https://static.cloudflareinsights.com`; `connect-src https://cloudflareinsights.com`) e e2e com a CSP de produção. Leva junto este plano. Merge autorizado se tudo passar.
- **PR 2 (até 17/10):** mapa (A), com a migration `tse_uf_snapshots`. O SQL vai para o usuário ANTES do merge. **Sem merge sem aprovação.**
- **PR 3 (19–20/10):** painel do admin (B). **Sem merge sem aprovação.**
- **Fluxo em todos:**
  - tdd-workflow;
  - /python-review, typescript-reviewer, database-reviewer (quando houver banco) e security-reviewer;
  - /code-review;
  - verification-loop;
  - /quality-gate;
  - Playwright com a CSP de produção;
  - só mocks no Gemini;
  - /save-session ao final de cada PR.

---

## A) Mapa do Brasil na página /apuracao

### Padrões a seguir (o que já existe)
| Categoria | Onde | Padrão |
|---|---|---|
| Coleta do TSE | `backend/app/services/tse/service.py` | Refresh preguiçoso: só quando alguém pede a página. Single-flight em 2 camadas: `TseCache` de 5 s no processo e claim no banco por escopo (`fetched_at` + `stale_seconds`). Se o TSE falha, fica o último resultado bom. |
| Parser | `backend/app/services/tse/parser.py` | `parse_result` devolve `TseResult` (candidatos com `pct`, `secoes_pct` vindo de `pst`, `eleito` só pelo status do TSE, `final`). |
| URL | `config.TSE_URL_TEMPLATE` | `ele2026/{eleicao}/dados/{escopo}/{escopo}-c0001-e{eleicao:06d}-u.json`, com validador de domínio oficial. |
| Endpoint | `backend/app/routers/tse.py` | `GET /tse/resultado` com ETag/304 e `Cache-Control: public` curto. `GET /tse/status` traz as chaves (`banner`, `apuracao`). |
| Frontend | `frontend/src/pages/Apuracao.tsx`, `src/lib/tse.ts` | Abas WAI-ARIA Brasil/Roraima. Polling de 60 s (`POLL_MS`) só com a aba visível, uma requisição por vez. Guarda `isTseResponse`. |
| Testes | `tests/unit/test_tse_*.py`, `tests/integration/test_tse_db.py`, `frontend/e2e/apuracao.spec.ts` | Fixture real do 1º turno (`tests/fixtures/tse/br-c0001-e006257-u.json`, ~9 KB). |

### Coleta dos 27 estados (proposta)
- **Mesmo mecanismo preguiçoso, sem tarefa em segundo plano.** O Render Free dorme, e uma tarefa periódica faria coleta sem ninguém olhando. A coleta acontece só quando alguém está na página.
- **Escalonamento (ajustado pelo usuário):** cada requisição a `GET /tse/mapa` tenta o claim de no máximo **9 UFs vencidas** (as mais antigas primeiro). Cada UF vence em **120 s**. As buscas rodam em paralelo, com **timeout de 5 s** cada.
  - Com uma pessoa olhando (polling de 60 s), as 27 UFs se renovam em 3 ciclos, **~3 min**. Com mais gente, em ~2 min, o limite da trava.
  - Carga máxima no TSE: 27 arquivos de ~9 KB a cada 2 min, ~0,2 req/s e ~250 KB. Sem impacto no Render nem no TSE.
- **O claim no banco garante no máximo 1 busca por UF a cada 120 s,** mesmo com várias instâncias ou abas.
- **Guarda só o resumo mais recente por UF:** percentual e número de cada candidato, `secoes_pct`, `totalizado_em` e `final`. Não guarda o JSON bruto.
- **Ensaio:** o claim compara `eleicao`. Um snapshot de outra eleição não aparece, como já acontece em BR/RR.

### Migration
> ⚠️ **MIGRATION PENDENTE: `20261015000000_tse_mapa.sql`** (aplicar ANTES do merge, no SQL Editor).

- A tabela `tse_snapshots` tem `check (scope in ('br','rr'))`. Em vez de mexer nela, a proposta é uma **tabela nova** `public.tse_uf_snapshots`:
  - colunas: `uf` (pk, check na lista das 27 siglas), `eleicao`, `resumo jsonb`, `tse_updated_at` e `fetched_at`;
  - RLS ligada, com revoke de `anon` e `authenticated`.
- **Marca no `/health/ready`:** `("table", "public.tse_uf_snapshots")`.

### Endpoint
- **`GET /tse/mapa`:** devolve `{eleicao, atualizado_em, estados: [{uf, candidatos: [{numero, nome, pct}], secoes_pct, final}]}`.
  - Os estados vêm em ordem fixa, com `null` para quem ainda não tem dado.
  - ETag/304 e `Cache-Control: public, max-age=30`, como em `/tse/resultado`.
- **`GET /tse/status`** ganha `"mapa": true|false`.

### Chave para desligar sem deploy
- **`TSE_MAPA_ENABLED`** (padrão `true`). Com `false`:
  - `/tse/status` devolve `mapa=false` e o frontend esconde o mapa;
  - `/tse/mapa` responde 404 e **não busca nada no TSE**.
- **Ajuste fino, também por variável de ambiente:** `TSE_MAPA_REFRESH_SECONDS` (padrão 120, entre 60 e 600) e `TSE_MAPA_LOTE` (padrão 9, entre 1 e 27).

### Cores e legenda (pedido do usuário)
- **Por número de candidato,** configurável **sem deploy:** `TSE_MAPA_CORES`.
  - Padrão: `13=vermelho` (Lula). O número do outro candidato é **lido do arquivo do TSE e mostrado ao usuário**, nunca assumido.
  - Vermelho e azul usam tons com contraste igual. Candidato sem cor configurada fica em cinza neutro.
- **Intensidade proporcional à margem** do 1º para o 2º colocado: 4 faixas, 0–5, 5–15, 15–30 e mais de 30 pontos. Faixas discretas ficam mais legíveis e acessíveis que um gradiente contínuo.
- **Sem dados:** cinza. **Outro líder** (só acontece no ensaio do 1º turno, com vários candidatos): cinza-escuro neutro, com o nome no detalhe.
- **Legenda idêntica para os dois:** mesmo formato e mesmo texto, só a cor muda.
- **"Eleito":** não existe por estado na eleição presidencial. O mapa nunca escreve "Eleito". O status nacional continua vindo do arquivo BR, com o texto oficial do TSE, como hoje.

### Mapa SVG (pesquisa prévia feita)
- **Fonte recomendada: Natural Earth**, "Admin 1 – States, Provinces", escala 1:50m, filtrada no Brasil. É **domínio público** declarado ("No permission is needed to use Natural Earth"), sem atribuição obrigatória, mas vamos citá-la no rodapé.
  - Alternativa: a API de malhas do IBGE (`servicodados.ibge.gov.br/api/v4/malhas/paises/BR?intrarregiao=UF&qualidade=minima&formato=image/svg+xml`), que já devolve o SVG. Não achei os termos de reúso do IBGE de forma explícita, então ela só entra se você confirmar a licença.
- **Geração única e offline,** por um script Python no repo (`frontend/scripts/mapa_uf.py`, sem dependência nova):
  - projeção equiretangular, simplificação Douglas–Peucker e arredondamento de coordenadas;
  - resultado: `src/lib/mapaUf.ts`, com o path de cada UF;
  - meta: < 40 KB brutos, ~12 KB gzip.
- **Bundle:** o mapa entra num **chunk carregado sob demanda** (`import()`, só na aba Mapa). O bundle principal não cresce.
- **Acessibilidade e celular:**
  - o SVG é responsivo (`viewBox`);
  - tocar ou passar o mouse num estado mostra um painel abaixo do mapa, não um tooltip flutuante, que funciona melhor no toque. O painel traz o % de cada candidato e o % de urnas apuradas;
  - o SVG tem `role="img"` com `aria-label` resumido, e o detalhe por estado fica numa **tabela alternativa** (27 linhas: UF, líder, % de cada candidato, % de urnas), visível como "Ver em tabela" e lida por leitor de tela;
  - os estados não entram na ordem do Tab, para não criar 27 paradas; o acesso pelo teclado é pela tabela;
  - contraste AA em todas as faixas.
- **Polling:** igual ao da página atual (60 s, só com a aba visível, uma requisição por vez), aproveitando o mesmo hook.
- **CSP:** nada muda, porque o SVG vem do próprio bundle.

### Arquivos
| Arquivo | Ação |
|---|---|
| `supabase/migrations/20261015000000_tse_mapa.sql` | CREATE (tabela + RLS) |
| `backend/app/routers/health.py` | UPDATE (marca da migration) |
| `backend/app/config.py`, `.env.example`, `render.yaml` | UPDATE (`TSE_MAPA_*`) |
| `backend/app/repositories/tse.py` | UPDATE (claim em lote das UFs vencidas, save e get_all das UFs) |
| `backend/app/services/tse/mapa.py` | CREATE (coleta escalonada e resumo por UF) |
| `backend/app/routers/tse.py` | UPDATE (`GET /tse/mapa`, `mapa` no `/tse/status`) |
| `frontend/scripts/mapa_uf.py`, `frontend/src/lib/mapaUf.ts` | CREATE (geometria gerada + licença) |
| `frontend/src/components/MapaApuracao.tsx` | CREATE (SVG, painel de detalhe, legenda, tabela alternativa) |
| `frontend/src/lib/tse.ts`, `frontend/src/pages/Apuracao.tsx` | UPDATE (tipo e guarda de `/tse/mapa`; aba ou seção Mapa) |

### Testes (tdd-workflow)
- **Unitários:**
  - resumo por UF a partir da fixture;
  - faixas de margem e cores por número;
  - escolha do lote (as mais antigas primeiro, no máximo 4);
  - chave desligada não busca nada.
- **Integração (Postgres real):**
  - claim em lote com duas sessões concorrentes (nenhuma UF buscada duas vezes);
  - snapshot de outra eleição invisível;
  - migration e marca no `/health/ready`.
- **Smoke:** `/tse/mapa` com ETag/304, chave desligada dando 404, `/tse/status` com `mapa`.
- **Vitest:**
  - cores e faixas;
  - painel de detalhe ao tocar;
  - tabela alternativa com as 27 linhas;
  - cinza sem dados;
  - "Eleito" nunca aparece no mapa;
  - polling pausado com a aba oculta.
- **Playwright com a CSP de produção:** o mapa carrega (`/tse/mapa` simulado), toque no celular, tabela, chave desligada esconde o mapa, sem erro de CSP no console.
- **Validação real:** ensaio com `TSE_ELECTION_CODE=6257` em produção até 18/10 (só leitura, com o 1º turno).

### Estimativa
| Parte | Tempo |
|---|---|
| Backend (migration, repo, serviço, endpoint, testes) | ~4–5 h |
| Geometria (script, conferência visual, licença) | ~1,5 h |
| Frontend (componente, painel, tabela, a11y, e2e) | ~4–5 h |
| Revisões, verificação e PR | ~2 h |
| **Total** | **~12–14 h, até 17/10, deixando 18/10 para o ensaio** |

### Se o prazo apertar (cortes, em ordem)
1. Intensidade pela margem: cor única por líder.
2. Painel de detalhe ao tocar: só a tabela traz o detalhe.
3. Lote adaptativo: lote fixo.

**Nunca cortar:** a tabela acessível, a chave de desligar e o claim no banco.

### Riscos
| Risco | Mitigação |
|---|---|
| TSE muda o formato do arquivo por UF | Mesmo parser já testado com o 1º turno, e o ensaio de 18/10 confirma |
| TSE lento ou fora do ar no dia | Fica o último resumo bom por UF; timeout de 10 s; lote limitado |
| Percepção de parcialidade nas cores | Legenda idêntica, faixas iguais, contraste igual; cores por número, configuráveis sem deploy |
| Migration esquecida | Alerta de migration pendente + marca no `/health/ready` (503 `schema_desatualizado`) |
| Bundle | Chunk sob demanda; o bundle principal não cresce |

---

## B) Painel mínimo do admin (só leitura)

### Padrões a seguir
- **Autorização:** `app.auth.require_admin` (Google + `ADMIN_EMAIL`), o mesmo da fila. No frontend, o padrão de `AdminModeracao.tsx` (`profile.is_admin`, aviso para quem não é admin, a API devolve 403).
- **Leituras agrupadas:** uma só transação com vários `select`, porque cada transação custa ~0,7 s entre Virgínia e São Paulo.

### Métricas e de onde vêm (já gravamos?)
| Métrica | Fonte | Já existe? |
|---|---|---|
| Checagens por dia e por resultado (7 dias) | `checks.created_at`, `veredito` e `prompt_version` (`"agencias"` = só agências) | Sim |
| Uso de cada modelo hoje e cota restante | `gemini_model_usage` (dia do Pacífico) + `GEMINI_MODEL_LIMITS` | Sim |
| Cota global hoje (checagem e moderação) | `gemini_usage` | Sim |
| **Tokens consumidos** | Hoje vão **só para o log** (`gemini_usage` no `_log_usage`: input, output e thoughts) e **não são gravados** | **Não** |
| Comentários por dia (7 dias) | `comments.created_at` | Sim |
| Fila de moderação aberta | `moderation_queue.status = 'pending'` | Sim |
| Denúncias (7 dias) | `comment_reports.created_at` | Sim |
| Usuários novos por dia | `profiles.created_at` | Sim |
| Suspensos e banidos | `profiles.status` + `suspended_until` | Sim |

**Proposta para os tokens, sem migration e sem transação extra:** gravar `{"tokens": {"in": .., "out": .., "thoughts": ..}}` dentro do `checks.result` (jsonb), que já é escrito no `save_result`. O painel soma os últimos 7 dias. Os tokens da moderação ficam de fora nesta versão: são pequenos e exigiriam mexer na gravação do comentário. Se quiser incluí-los, a alternativa são colunas novas em `gemini_model_usage`, com migration e uma escrita a mais por chamada (~0,7 s). Não recomendo antes da eleição.

### Endpoint e tela
- **`GET /admin/painel`** (só admin, `Cache-Control: no-store`): um JSON com as métricas acima, em **uma transação**.
- **Tela `/minha-conta/painel`**, que só aparece para o admin, com link a partir de "Minha conta":
  - tabelas simples e números, sem biblioteca de gráficos (bundle);
  - link para a fila de moderação, que continua igual.
- **Visitas: Cloudflare Web Analytics** (sem cookies), ativado por você no painel do Pages. O painel só traz um link para o dashboard do Cloudflare; os dados não passam pela nossa API.

### Ajuste exato na CSP (`frontend/csp.ts`, que gera o `_headers`)
- `script-src 'self' https://static.cloudflareinsights.com`. A FAQ oficial cita `https://static.cloudflareinsights.com/beacon.min.js`; o host inteiro cobre o caso com `?token=`.
- `connect-src 'self' <api> <supabase> https://cloudflareinsights.com`:
  - na injeção automática o beacon envia para o próprio domínio (`'self'`, que já está lá);
  - no snippet manual ele envia para `cloudflareinsights.com`;
  - incluímos os dois para funcionar nas duas formas.
- **Teste:** o e2e roda com a CSP de produção e confere que o beacon não é bloqueado (script simulado).

### LGPD
- **Nada por pessoa:** sem tempo de uso individual, sem lista de usuários e sem e-mails. Só contagens agregadas por dia.
- O Cloudflare Web Analytics não usa cookies. A Política de Privacidade ganha uma linha sobre "estatísticas de visitas agregadas, sem cookies (Cloudflare Web Analytics)" e a versão dos termos é atualizada.

### Migration
**Nenhuma** com a proposta de tokens no `checks.result`.

### Arquivos
| Arquivo | Ação |
|---|---|
| `backend/app/repositories/painel.py` | CREATE (leituras agregadas, 1 transação) |
| `backend/app/routers/admin_painel.py` (ou rota em `comments.py`, ao lado da fila) | CREATE |
| `backend/app/services/check_service.py` | UPDATE (`tokens` no `result` gravado) |
| `backend/app/services/gemini.py` | UPDATE mínimo: expor os tokens do `usage_metadata` ao serviço, **sem mexer no prompt nem no comportamento** |
| `frontend/src/pages/PainelAdmin.tsx`, `src/router.tsx`, `src/pages/MinhaConta.tsx` | CREATE/UPDATE |
| `frontend/csp.ts`, `frontend/csp.test.ts` | UPDATE (Web Analytics) |
| `frontend/src/pages/legal/Privacidade.tsx`, `backend/app/legal.py` | UPDATE (linha sobre visitas + versão) |

### Testes
- **Integração:** cada métrica com dados criados no Postgres real; 403 para quem não é admin; 1 transação; nenhum dado pessoal no JSON.
- **Vitest:** a tela renderiza as tabelas, esconde-se para quem não é admin e tem o link para a fila.
- **Playwright com a CSP de produção:** o admin vê o painel; o usuário comum não vê; o beacon passa na CSP.

### Estimativa
| Parte | Tempo |
|---|---|
| Backend (repositório, endpoint, tokens no result, testes) | ~3–4 h |
| Frontend (tela, rota, link, CSP, e2e) | ~3 h |
| Revisões, verificação e PR | ~1,5 h |
| **Total** | **~8–9 h (19–20/10)** |

### Se o prazo apertar (cortes, em ordem)
1. Tokens: ficam no log.
2. Usuários novos, suspensos e banidos: viram um único total.
3. A tela inteira: o JSON do endpoint basta para o admin.

**B inteiro é cortado antes de qualquer parte essencial do mapa.**

### Riscos
| Risco | Mitigação |
|---|---|
| Consultas pesadas no banco | Janelas de 7 dias com índices em `created_at` (já existem em `checks`, `comments` e `moderation_log`); 1 transação; só o admin acessa |
| Exposição de dados pessoais | Só agregados; teste que confere a ausência de e-mail, apelido e id no JSON |
| Mudança no `save_result` | Só um campo a mais no jsonb; testes de checagem cobrem |

---

## Calendário
| Data | O quê |
|---|---|
| 14–15/10 | A: backend + migration (PR A1) → você aplica a migration → merge |
| 15–17/10 | A: geometria + frontend + e2e (PR A2) → merge |
| até 18/10 | Ensaio com `TSE_ELECTION_CODE=6257` (mapa + BR/RR) → volta para 6258 até 21/10 |
| 19–20/10 | B (PR B) → merge |
| 21/10 | Teste de carga leve (já no plano) |
| 22/10 em diante | Prompt congelado; só correções |
| 25/10 | Nenhum deploy |

**AGUARDANDO APROVAÇÃO.** Decisões pedidas:
1. Fonte do mapa: Natural Earth (domínio público) ou IBGE, se você confirmar a licença.
2. Tokens no `checks.result` (sem migration), com a moderação de fora.
3. O número de candidato do Flávio para `TSE_MAPA_CORES`.
4. Uma linha na Política de Privacidade sobre o Web Analytics, com troca da versão dos termos.
