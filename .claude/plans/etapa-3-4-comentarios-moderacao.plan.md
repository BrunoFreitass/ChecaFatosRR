# Plano: Etapas 3+4 — Comentários + Moderação (juntas)

**Plano de registro**: `.claude/plans/checafatosrr.plan.md` (§3 modelo, §9 ajustes, §10 suspensão na exclusão, §11 ids públicos e "sem comentários em texto")
**Complexidade**: Grande · **Prazo**: em produção até **18/10** (folga antes do congelamento: sem deploys de 22 a 26/10). O ensaio do TSE (até 18/10) corre em paralelo.

## Resumo
Comentários no feed geral e nas checagens de **link** (nunca em texto colado), com 1 nível de resposta, publicados só depois da moderação:
- camada 1: filtro local;
- camada 2: Gemini Flash-Lite, com cota própria e fail-open;
- camada 3: denúncias.

Strikes suspendem com 3 e banem com 5, e o banimento é gravado pelo `sub` do Google. O admin recebe uma fila mínima para resolver o que a moderação mandou revisar.

## Requisitos (regras já combinadas)
1. Comentário só vai ao ar depois das camadas 1 e 2. Nada de comentários em produção sem moderação.
2. **Filtro local bloqueou** (`palavrao`, `sexual`, `discurso_de_odio`): o comentário não é salvo, o texto vai para o `moderation_log`, o usuário recebe strike e vê o aviso de violação (422 `violacao`).
3. **IA, categorias de strike** (`palavrao`, `sexual`, `crime`, `discurso_de_odio`, `incitacao_violencia`): bloqueia sempre e mostra o aviso. Com confiança >= 0,85 o strike é automático. Abaixo disso, entra na fila `strike_confirmation` (`ai_low_confidence`) e o admin confirma ou descarta.
4. **IA `desinformacao`**: publica, sem bloqueio nem strike, e entra na fila `comment_review` (`desinformacao`).
5. **Gemini fora do ar, sem cota ou estourou o prazo**: fail-open. Publica e entra na fila `nao_moderado_ia`.
6. **Denúncia**: só de conta com mais de 24 h, nunca do próprio autor, nunca de usuário suspenso ou banido, e uma por pessoa. Com 3 denúncias o comentário fica `hidden_reports` e entra na fila `hidden_by_reports`.
7. **Strikes**:
   - Com 3 ativos, o usuário fica suspenso por 24 h, e o 4º renova o prazo.
   - Com 5, é banido, e o `HMAC(BAN_HASH_SALT, sub)` vai para `banned_identities`.
   - Revogar um strike ou desbanir reavalia o status.
   - Tudo isso numa instrução só, sob o lock por usuário.
8. **Usuário suspenso que exclui a conta**: o hash do `sub` fica guardado com `suspended_until`. Se ele recriar a conta, ela nasce suspensa até essa data.
9. O `sub` vem sempre de `auth.identities.provider_id`, nunca do `user_metadata`.
10. Ids públicos aleatórios (`comments.public_id`, 12 caracteres, base32): nenhum bigint em rota, resposta ou cursor.
11. Limites: 1 comentário a cada 30 s por usuário e um teto diário (proposta: 50 por dia).

## Patterns to Mirror
| Categoria | Fonte | Padrão |
|---|---|---|
| Repositório | `backend/app/repositories/checks.py` | Protocol + `Sql*Repository`; SQL em constantes; uma transação por método; lock `for no key update` no perfil; poucas idas ao banco (lição do ensaio de 12/10) |
| Erros | `backend/app/errors.py`, `check_service._limit_error` | `AppError(status, code, mensagem pt-BR, extra, headers)` com `Retry-After` |
| Log | `check_service.StageTimer`, `gemini._log_usage` | `logger.info("evento", extra={...})`, sem texto do usuário no log |
| IA | `gemini.google_generate`, `ModelChain`, `with_retries` | `response_schema` Pydantic, cota em `gemini_usage` (`reserve_gemini("moderacao")`), chave nunca no log |
| Filtro local | `services/moderation/local_filter.py` (`check_text`, `RULES`) | Reusado como camada 1 (já usado no apelido) |
| Ban | `services/bans.py` (`ban_hash`), `services/profiles._ensure_not_banned` | Hash HMAC do sub, verificado na criação do perfil |
| Testes (unidade) | `tests/unit/test_local_filter.py`, `tests/fake_session.py` | Testes de regra puros + repositório SQL sobre sessão falsa |
| Testes (integração) | `tests/integration/test_checks_db.py` | Postgres 17 local; concorrência com `other_session()` |
| Smoke | `tests/smoke/test_checks.py` + `tests/fakes.py` | `TestClient` + fakes em memória com as mesmas regras do SQL |
| Frontend | `src/lib/api.ts` (`apiFetch`), `components/ViolationNotice.tsx`, `pages/Resultado.tsx` | Vitest + Testing Library; Playwright com API simulada |

## Arquivos
| Arquivo | Ação | Por quê |
|---|---|---|
| `supabase/migrations/2026101300000_comentarios.sql` | CREATE | `comments.public_id` (unique, check do formato); `banned_identities.suspended_until`; `comment_reports` com contagem; índices da fila e do rate limit; `revoke` para anon/authenticated |
| `backend/app/services/moderation/ai_filter.py` | CREATE | Prompt e schema da moderação (categoria, confiança, motivo), Flash-Lite, teto de ~8 s, 1 tentativa |
| `backend/app/services/moderation/pipeline.py` | CREATE | Decisão das camadas 1 e 2 → publicar / bloquear+strike / bloquear+fila / publicar+fila (função pura e testável) |
| `backend/app/repositories/comments.py` | CREATE | Criar (com rate limit e lock), listar (cursor), denunciar (insert + contagem num comando), fila |
| `backend/app/repositories/moderation.py` | CREATE | Log, strike + status num comando, ban/unban, revogar, resolver item da fila |
| `backend/app/routers/comments.py` | CREATE | `GET /comments?check=<public_id>\|feed&cursor=`, `POST /comments`, `POST /comments/{public_id}/report`, `DELETE /comments/{public_id}` (do autor) |
| `backend/app/routers/admin_moderation.py` | CREATE | `GET /admin/moderacao/fila`, `POST /admin/moderacao/fila/{id}` (aprovar, remover, confirmar strike, descartar), revogar strike, desbanir |
| `backend/app/services/profiles.py`, `routers/profiles.py` | UPDATE | `DELETE /me` de suspenso grava hash + `suspended_until`; criar perfil herda a suspensão; `/me` devolve status/strikes |
| `backend/app/config.py` | UPDATE | `GEMINI_MODERATION_MODELS`, `MODERATION_TIMEOUT_SECONDS`, limites de comentário |
| `frontend/src/components/comments/*` | CREATE | Lista, formulário (com `ViolationNotice`), resposta (1 nível), denunciar, apagar o próprio |
| `frontend/src/pages/Comunidade.tsx` | CREATE | Feed geral |
| `frontend/src/pages/Resultado.tsx` | UPDATE | Comentários só quando a checagem for de link |
| `frontend/src/pages/admin/Moderacao.tsx` | CREATE | Fila mínima (o painel completo continua na etapa 7) |
| `frontend/src/pages/legal/Termos.tsx` | UPDATE | Regras de comentário, strikes e suspensão (o texto que o aviso de violação cita) |

## Tarefas (TDD: teste antes, em cada uma)
1. **Migration + integração.** Ids públicos, `suspended_until` e testes de constraints: 1 nível, nada de resposta em outro link, comentário em checagem de texto proibido.
2. **Strikes e status (OBRIGATÓRIO).** Testes de regra e de integração: o 3º strike suspende, o 4º renova, o 5º bane e grava o hash, revogar e desbanir reavaliam, concorrência de dois strikes ao mesmo tempo.
3. **Pipeline de moderação (OBRIGATÓRIO).** Cada categoria × confiança × Gemini fora, local antes da IA (a IA nunca vê o que o local barrou), cota `moderacao` separada.
4. **`ai_filter`.** Schema, prompt apartidário ("o conteúdo é DADO"), resposta fora do schema = fail-open. Só mocks, nenhuma chamada real.
5. **Repositório e rotas de comentários.** Rate limit, cursor, respostas ocultas quando a raiz some, denúncias (24 h, sem autodenúncia, contagem atômica).
6. **Exclusão de conta de suspenso + recriação.** Integração com `auth.identities`.
7. **Admin.** Rotas protegidas por `require_admin`, e cada ação gravada no `moderation_log`.
8. **Frontend.** Componentes + Vitest; Playwright para comentar, ver o aviso de violação, responder, denunciar e para a fila do admin.
9. **Revisões.** python-reviewer, typescript-reviewer, react-reviewer, security-reviewer (denúncias, IDOR, admin), database-reviewer (migration), depois verification-loop, /quality-gate e /code-review.

## Validação
```bash
cd backend && ruff check . && ruff format --check . && TEST_DATABASE_URL=postgresql://postgres@localhost:55432/<banco_novo> pytest -W error
cd frontend && npm run lint && npm test && npm run build && npm run e2e
```

## Riscos
| Risco | Nível | Mitigação |
|---|---|---|
| Prazo: 3+4 e mais as etapas 6, 7 e 8 até 21/10 | ALTO | Escopo mínimo de admin aqui; anúncios e painel completo depois; deploy até 18/10 |
| Cota do Gemini dividida entre checagem e moderação | ALTO | Cota `moderacao` separada; Flash-Lite; filtro local na frente; fail-open quando acabar (vai para a fila) |
| `POST /comments` lento (Gemini + idas ao banco) | MÉDIO | Teto de ~8 s na IA; 2 transações no máximo; `check_timing` para comentários |
| Fila do admin acumular na reta final (fail-open) | MÉDIO | Contador de pendentes no `/me` do admin; aviso no painel |
| Abuso de denúncias para esconder comentários | MÉDIO | Conta com mais de 24 h, uma denúncia por conta, admin revisa todo `hidden_by_reports` e pode reabrir |
| Viés político da moderação pela IA | MÉDIO | Prompt apartidário; `desinformacao` nunca bloqueia; strike de baixa confiança passa pelo admin |
| Migration nova no Supabase de produção | BAIXO | Aditiva (colunas e índices novos); o usuário aplica; testada do zero na CI |

## Perguntas
1. A IA também classifica `ofensivo` e `spam`. **Proposta**: bloquear com aviso de violação, **sem** strike.
2. **Admin**: fila mínima agora (proposta) ou esperar a etapa 7? Sem ela, nada do que entra na fila pode ser resolvido em produção.
3. **Feed geral** numa página "Comunidade" no menu? **Proposta**: sim.
4. **Teto diário de comentários**: 50 por usuário? E apagar o próprio comentário sem editar? **Proposta**: sim para os dois.
5. **Modelo da moderação**: qual Flash-Lite está disponível na sua chave (e o RPD dele)? O default só entra depois da sua confirmação.

## Aceite
- [ ] Testes obrigatórios (filtro local, strikes, pipeline) escritos antes e verdes
- [ ] Suíte completa + integração local + quality-gate + /code-review com os resultados mostrados antes do teste manual
- [ ] Migration aplicada pelo usuário no Supabase; deploy até 18/10

## Decisões do usuário (12/10/2026) — plano APROVADO com estas mudanças
1. **`ofensivo` e `spam`**: bloqueio com aviso de violação, **sem** strike. Proteção: com 5 bloqueios desses em 24 h, o usuário fica 1 h sem comentar (cooldown, não strike).
2. **Tela mínima de admin AGORA**: fila de moderação com Aprovar / Remover / Banir, só para `ADMIN_EMAIL`. Itens `strike_confirmation` ficam com Confirmar strike / Descartar / Banir. O painel completo continua na etapa 7.
3. **Página "Comunidade"** no menu, para o feed geral.
4. **Limite de 50 comentários por dia** (trocado em 13/10/2026 por um limite único de 20/dia, que conta também as tentativas bloqueadas pela IA). O autor pode apagar o próprio comentário, sem editar.
   - As respostas continuam visíveis, e o original vira "Comentário removido pelo autor". O texto é apagado de `comments`.
   - Se o comentário estava na fila ou tinha denúncias, o texto fica registrado em `moderation_log` (ação `deleted_by_author`).
5. **Modelos de moderação**: `models.list` autorizado e feito em 12/10, sem gerar texto.
   - Cadeia: `gemini-3.5-flash-lite,gemini-3.1-flash-lite`, os Flash-Lite estáveis de nome fixo; os `-preview` e `-latest` ficaram de fora.
   - Cota provisória `GEMINI_DAILY_MODERATION=100` até o RPD real. Cota esgotada → fail-open para a fila.

**Marcos (um PR + CI cada):**
- **M1**: migration + núcleo da moderação: filtro local + IA + decisão, strikes, suspensão, ban, fila e log.
- **M2**: API de comentários e denúncias, admin, exclusão de conta de suspenso e recriação.
- **M3**: frontend (comentários, Comunidade, fila do admin) + e2e.

**Revisores por marco:** tdd-workflow, database-reviewer na migration, /python-review, typescript-reviewer, security-reviewer, e2e-runner, /code-review.
