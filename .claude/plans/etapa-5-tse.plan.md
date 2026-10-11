# Plano: Etapa 5 — Apuração do TSE (2º turno, 25/10/2026)

**Data:** 11/10/2026 · **Base:** `main` @ `a4e7bc4` · **Complexidade:** Média
**Prazo duro:** no ar e validada até **20/10** (de 22 a 26/10 não há deploy; trocar variável reinicia o serviço).
**Status:** APROVADO em 11/10/2026. Marco 1 (backend) concluído.

## Objetivo
Página "Apuração" com o resultado **oficial** do TSE para o 2º turno: Brasil e Roraima (presidente) e, se houver, governador de RR. Deve aguentar o pico da noite de 25/10 no Render Free (0,1 CPU, 5 GB de banda) e continuar útil se o TSE cair ou mudar o formato.

## Princípios (neutralidade e legislação)
- Só dado oficial: sem projeção, sem "vencendo", sem cor de partido. Ordem e percentuais do TSE; "Eleito" só quando o TSE marcar.
- Sempre "Fonte: TSE", com horário da última totalização e link para resultados.tse.jus.br.
- Mostrar o % de seções totalizadas em destaque (resultado parcial é parcial).

## Arquitetura (já decidida no plano principal, ajustada ao Render)
1. **Refresh preguiçoso com single-flight** (sem loop: Render Free dorme e tem 0,1 CPU).
   - `GET /tse/resultado` lê `tse_snapshots`. Se o snapshot tem mais de **30 s**, uma requisição pega o claim atômico (`update ... set fetched_at = now() where fetched_at < now() - 30s returning`) e busca no TSE; as outras recebem o snapshot atual (nunca esperam o TSE).
   - Igual ao `ReadyCache` do `/health/ready` (`backend/app/routers/health.py`), mas com o claim no banco.
2. **Snapshot normalizado**: grava o modelo interno (não o JSON bruto) e só sobrescreve se `tse_updated_at` for mais novo. TSE fora do ar ou formato inesperado → mantém o último bom e loga `tse_fetch_failed`.
3. **Banda** (o limite que suspende o Render): resposta compacta (~1 KB), um endpoint com todos os escopos, `ETag` + 304, `Cache-Control: public, max-age=15` e gzip. Estimativa: 2.000 pessoas × 4 h × 1 consulta/min ≈ 480 mil requisições ≈ 0,5–0,7 GB.
4. **Frontend**: página `/apuracao` (abas Brasil / Roraima), consulta a cada 60 s só com a aba visível (`visibilitychange`), `aria-live="polite"`, horário de Boa Vista. Banner na Home em 25/10 apontando para a apuração.
5. **Configurável sem código** (Render, variáveis `sync: false`): `TSE_ENABLED`, `TSE_URL_TEMPLATE` (com `{escopo}`, `{eleicao}`, `{cargo}`), `TSE_ELECTION_CODE`, `TSE_CARGOS` (ex.: `presidente:0001:br,rr;governador:0003:rr`), `TSE_REFRESH_SECONDS=30`.

## Validação do formato de 2026 (o maior risco)
- O 1º turno de 2026 (04/10) **já tem arquivos reais** no site de resultados do TSE. Validar o adaptador contra eles define o formato e o padrão de URL de 2026; o código do 2º turno costuma ser o do 1º + 1 (2022: 544 → 545).
- Fixtures: 2º turno de 2022 (BR e RR, presidente) + 1º turno de 2026 (BR, RR; presidente e governador). **Precisa da sua autorização** para baixar esses arquivos públicos (≤ 6 downloads, sem chave).
- Antes de 22/10: conferir se o TSE publicou a configuração do 2º turno de 2026 e fixar `TSE_ELECTION_CODE` no painel.

## Padrões a seguir
| Categoria | Fonte | Padrão |
|---|---|---|
| HTTP externo | `backend/app/services/fetcher.py` | httpx com timeout e teto de bytes |
| Repositório | `backend/app/repositories/checks.py` | SQL parametrizado, uma transação por método, claim atômico |
| Erros | `backend/app/errors.py` | `AppError` com código e mensagem em pt-BR |
| Single-flight | `backend/app/routers/health.py` (`ReadyCache`) | lock + dupla checagem de validade |
| Testes | `tests/unit`, `tests/smoke` com fakes, `tests/integration` no Postgres local | fixtures JSON reais em `tests/fixtures/tse/` |
| Front | `frontend/src/pages/Ultimas.tsx` | `apiFetch` + estados loading/erro + Vitest/Playwright |

## Arquivos
| Arquivo | Ação |
|---|---|
| `backend/app/services/tse/parser.py` | CRIAR: JSON TSE → `TseResult` (tolerante: números com vírgula, campos ausentes, candidato extra) **[teste obrigatório]** |
| `backend/app/services/tse/client.py` | CRIAR: baixa um escopo/cargo (timeout 5 s, teto 512 KB, User-Agent próprio) |
| `backend/app/repositories/tse.py` | CRIAR: `get_all`, `claim(scope, cargo)`, `save_if_newer` |
| `backend/app/services/tse/service.py` | CRIAR: snapshot + refresh single-flight (claim no banco + lock no processo) |
| `backend/app/routers/tse.py` + `schemas/tse.py` | CRIAR: `GET /tse/resultado` (ETag, 304, cache 15 s) |
| `backend/app/config.py`, `render.yaml`, `docs/DEPLOY.md` | ATUALIZAR: variáveis `TSE_*` |
| `supabase/migrations/2026101x_tse_cargo.sql` | CRIAR (se houver governador): chave `(scope, cargo)` em `tse_snapshots` |
| `frontend/src/pages/Apuracao.tsx`, rota, link no menu, banner na Home | CRIAR/ATUALIZAR |
| `frontend/e2e/apuracao.spec.ts` | CRIAR: API simulada, parcial → final, TSE fora (último snapshot) |

## Tarefas (TDD em cada uma)
1. **Fixtures + parser** (RED com os arquivos reais; casos: parcial 0%, 50%, final com eleito, campo faltando, número com vírgula, JSON quebrado).
2. **Cliente TSE** com transporte simulado (timeout, 404, 5xx, corpo grande demais).
3. **Repositório + migração** (integração no Postgres local: claim concorrente só um vence; snapshot mais velho não sobrescreve).
4. **Serviço single-flight** (unidade: 20 requisições simultâneas → 1 busca; TSE falhando → último snapshot + `tse_fetch_failed`; `TSE_ENABLED=false` → 404 amigável).
5. **Endpoint** (smoke: formato, ETag/304, `Cache-Control`, sem dados internos).
6. **Página Apuração** (Vitest + e2e com CSP): aba visível consulta a cada 60 s; aba oculta não consulta; "Atualizado às HH:MM (Boa Vista)"; "Eleito" só com a marca do TSE.
7. **Ensaio**: teste de carga leve local (500 requisições/min no endpoint com o TSE simulado) medindo CPU e banda; depois deploy até 18/10 e ensaio em produção com o 1º turno de 2026 (`TSE_ELECTION_CODE` do 1º turno) e com o TSE fora.
8. Revisões: `/python-review`, typescript-reviewer, `/code-review`, security-reviewer; PR + CI + merge.

## Cronograma
| Data | Marco |
|---|---|
| 12–14/10 | Tarefas 1–5 (backend) |
| 15–16/10 | Tarefa 6 (front) + revisões + merge |
| 17–18/10 | Deploy e ensaio em produção com o 1º turno de 2026 |
| até 21/10 | Código do 2º turno configurado (assim que o TSE publicar) |
| 25/10 | Monitorar (logs `tse_*`, UptimeRobot); nenhum deploy |

## Riscos
| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| Formato/URL de 2026 diferente | Média | Alto | Validar com o 1º turno de 2026; parser tolerante; template por variável |
| TSE bloquear ou limitar IPs de nuvem | Baixa | Alto | No máximo 1 busca por escopo a cada 30 s; User-Agent identificado; se falhar, último snapshot + link oficial |
| Pico estourar banda (5 GB) | Média | Alto | Resposta ~1 KB, ETag/304, consulta só com aba visível; com cartão, Starter (ver DEPLOY §3.5) |
| Render dormindo/cold start às 17h | Média | Médio | UptimeRobot a cada 5 min; contingência Starter |
| Código do 2º turno só sair em cima da hora | Média | Alto | Variável no painel; trocar até 21/10 (reinício de ~1 min, fora do pico) |
| Mostrar dado parcial como definitivo | Baixa | Alto | % de seções em destaque; "Eleito" só com a marca do TSE |

## Perguntas para você
1. **Cargos:** além de presidente (BR + RR), há 2º turno para **governador de Roraima** em 2026? Se sim, entra na página.
2. **Autoriza** baixar os arquivos públicos de resultado do TSE (2º turno de 2022 e 1º turno de 2026, ≤ 6 downloads) para as fixtures e para confirmar o formato de 2026?
3. **Intervalo:** 30 s no servidor e 60 s no navegador está bom? (Menor = mais banda e mais carga no TSE.)
4. **Banner na Home** em 25/10 apontando para a apuração: sim?

## Aceite
- [ ] Parser validado com arquivos reais de 2022 e do 1º turno de 2026
- [ ] 20 requisições simultâneas → 1 busca no TSE; TSE fora → último snapshot
- [ ] Ensaio em produção até 18/10; código do 2º turno configurado até 21/10
- [ ] Revisões e CI verdes; nenhum deploy de 22 a 26/10

## Respostas e descobertas (11/10/2026)
- Só presidente (Arthur Henrique eleito governador de RR no 1º turno): abas Brasil e Roraima.
- Intervalos: 30 s no servidor, 60 s no navegador. Banner neutro "Acompanhe a apuração oficial do 2º turno — fonte: TSE" de 16h de Boa Vista de 25/10 até o fim de 26/10, por variável no painel (`TSE_BANNER_INICIO`/`TSE_BANNER_FIM`).
- Downloads autorizados (6/6 usados): `ele-c.json` (códigos: 1º turno federal **6257**, 2º turno **6258**, já publicado); 2022 e o caminho `dados-simplificados/...-r.json` deram 404 (não existem mais); o formato de 2026 é `ele2026/<eleicao>/dados/<uf>/<uf>-c0001-e<eleicao 6 dígitos>-u.json` (fixture real do Brasil, 1º turno). **RR do 1º turno não foi baixado** (cota): o ensaio em produção com `TSE_ELECTION_CODE=6257` confere RR.
- No 1º turno o TSE marca `"e": "s"` para quem vai ao 2º turno (`"st": "2º turno"`): "Eleito" só pela situação.
