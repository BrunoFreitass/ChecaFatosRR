# Plano: Deploy do backend no Render (Cloud Run vira plano B)

**Data:** 10/10/2026 · **Base:** `main` @ `b3c9fbb` · **Prazo:** 2º turno em 25/10/2026
**Motivo:** o Google Cloud exige pré-pagamento de R$ 200 para ativar o faturamento. O backend vai para o **Render (Free, sem cartão)**; o frontend continua no **Cloudflare Pages**. O projeto GCP `checafatosrr` fica sem faturamento e mantém só o OAuth e a Fact Check; `gen-lang-client-…` mantém a chave do Gemini, sem faturamento.
**Complexidade:** Média. Status: **AGUARDANDO APROVAÇÃO.**

## Fatos do Render que o plano assume (conferidos em 10/10/2026)
| Fato | Valor | Fonte |
|---|---|---|
| Plano Free (web service) | 512 MB de RAM, **0,1 CPU**, uma instância só | docs "Compute plans" e fórum da Render |
| Dorme sem tráfego | após **15 min** sem requisição; acorda em **~1 min** | render.com/docs/free |
| Horas grátis | **750 h por workspace por mês** (sem acúmulo); acabou → **todos os Free suspensos até o mês seguinte** | render.com/docs/free |
| Banda de saída (workspace Hobby) | **5 GB/mês** desde 01/08/2026 (eram 100 GB); acima disso, sem forma de pagamento → **suspenso até o fim do mês** | artigo de 06/08/2026 (jwatte.com) e render.com/docs/free (regra da suspensão) |
| Reinício | o Render pode reiniciar um Free a qualquer momento; sem disco persistente, sem SSH | render.com/docs/free |
| Tempo máximo de requisição | 100 min (dito por funcionários do Render no fórum; **não achei na doc oficial atual**) | community.render.com |
| Regiões | Oregon, Ohio, **Virginia**, Frankfurt, Singapura (nenhuma na América do Sul) | render.com/docs/regions |
| Plano pago mais barato | Starter (`0.5c-512mb`): 0,5 CPU, 512 MB, US$ 7/mês | docs "Compute plans update" e resumos de preço de 2026 |

Consequência: **1 serviço Free ligado 24 h** gasta 744 h em outubro (31 dias) e cabe nas 750 h, **desde que seja o único serviço Free do workspace**. Não criar nenhum outro serviço Free (nem preview) até 26/10.

## Fase R0: código (branch `deploy-render`, TDD, PR com CI)
1. **Pool de conexões ao banco** (pedido 4).
   - **Hoje:** `NullPool`. Cada requisição abre uma conexão nova, com TCP, TLS (verify-full) e autenticação no Supavisor. São ~5 viagens de rede, e Virginia → São Paulo tem ~120 ms de ida e volta: **~0,6 s por requisição só para conectar**.
   - **Mudança:** pool pequeno e configurável (`AsyncAdaptedQueuePool`), com `DB_POOL_SIZE` (padrão **3**), `DB_POOL_MAX_OVERFLOW` (padrão **2**), `pool_recycle=300`, `pool_pre_ping=True`, **`pool_timeout=8`** (falha rápido com 503, em vez de enfileirar 30 s) e `pool_use_lifo=True` (conexões ociosas expiram). O pre-ping custa 1 viagem, contra ~5 de uma conexão nova. `DB_POOL_SIZE=0` mantém o `NullPool`; serve aos testes e ao plano B (Cloud Run).
   - **Compatibilidade com o pooler em transaction mode:** já está garantida. Statements preparados estão desligados (`statement_cache_size=0`, nomes únicos), e o código não usa estado de sessão (`SET`, advisory lock ou `LISTEN`; conferido com grep). Cada método do repositório é uma transação.
   - **Teto:** no máximo 5 conexões. O Supavisor do plano Free aceita bem mais.
   - **Testes:**
     - unidade: os parâmetros do engine conforme a configuração;
     - integração no Postgres local: muitas requisições seguidas reaproveitam a conexão; uma conexão derrubada no servidor é refeita (pre-ping); **uma tarefa cancelada no meio de uma query não deixa a próxima conexão quebrada**.
2. **Paralelismo do extrator** (pedido 5).
   - Com 0,1 CPU, dois parsers do lxml ao mesmo tempo dividem uma CPU já fraca.
   - O semáforo `_PARSERS` (hoje fixo em 2) passa a vir de `EXTRACT_CONCURRENCY` (padrão 2; no Render, **1**).
   - **Medir** o tempo de extração de páginas reais grandes (≈300 KB e 2 MB) e projetar para 0,1 CPU (×10, conservador). Se passar de ~8 s, baixar `FETCH_MAX_BYTES` para 1 MB no Render. O teto de leitura do link é de 20 s.
3. **Memória (512 MB)** (pedido 5).
   - Medir o RSS do processo parado e com 5 checagens simultâneas (páginas falsas de 2 MB e Gemini simulado), localmente.
   - Pôr `--limit-concurrency` no uvicorn (valor a definir pela medição, provável **20**): acima disso, ele responde 503 em vez de estourar a memória e reiniciar.
   - **Teste obrigatório:** sob carga, o health check do Render (`/health`) também recebe 503 e o serviço pode ser reiniciado. Escolher o limite para que isso não aconteça na carga esperada.
   - **`/health/ready` protegido** (security-reviewer H2): o resultado fica em cache no processo por 60 s. Quem chamar em massa recebe o cache e não ocupa conexões do pool. `/health` continua sem banco.
4. **Timeouts** (pedido 5).
   - Pior caso de uma checagem: ~92 s; com espera por outra checagem igual, até ~172 s. O Render não corta antes (100 min, ver tabela). O uvicorn não tem limite por requisição, e o navegador também não.
   - Nada a mudar no código. Só documentar, e conferir no teste da Fase R3 uma checagem lenta de verdade.
5. **Blueprint `render.yaml`** (infraestrutura como código, **sem segredos**):
   - web service, runtime Docker (`backend/Dockerfile`, contexto `backend/`), região `virginia`, plano `free`, health check `/health`;
   - auto-deploy só da `main`, e só **depois da CI passar** (opção `autoDeployTrigger: checksPass` — **confirmar o nome atual na doc antes de usar**);
   - variáveis não secretas com valor; segredos com `sync: false` (cadastrados no painel). `ADMIN_EMAIL` e `SUPABASE_URL` também com `sync: false`, para não ficarem no histórico do git;
   - **previews desligados** (gastariam as 750 h e herdariam os segredos; nome do campo a confirmar), `plan: free`, `branch: main`, sem `domains` nem outros serviços;
   - como o `render.yaml` na `main` vira infraestrutura: proteção da branch `main` (PR com CI obrigatória) e o app do Render no GitHub com acesso só a este repositório;
   - a troca de plano da contingência é feita **pelo painel**, não por commit.
6. **Dockerfile:**
   - continua igual: `PORT` vem do Render (padrão 10000), o `uvicorn --factory` já usa `${PORT}`, e o certificado está em `/srv/certs`;
   - só acrescentar `--limit-concurrency` (item 3).
7. **TSE (etapa 5, ainda não implementada)** (pedido 5).
   - O desenho aprovado é **refresh preguiçoso com single-flight no banco**: quem pede o resultado dispara uma busca se o cache tiver mais de 30 s, e os demais recebem o cache. O último snapshot bom fica em `tse_snapshots`.
   - Funciona igual no Render: o lock é no banco, não em memória, e o processo pode reiniciar a qualquer momento sem perder nada.
   - O loop em segundo plano (`TSE_BACKGROUND_POLL`) **continua desligado no Free**. Um reinício derruba o loop, e o 0,1 CPU é pouco. Só ligar se estivermos no Starter no dia 25/10.
8. **Verificação:** suíte completa (com integração no Postgres local), `/quality-gate`, `/code-review` e `security-reviewer`. Depois PR, CI verde e merge.

9. **Proteção da API contra consumo de banda** (security-reviewer H1). Item obrigatório, a decidir na pergunta 4:
   - **Com domínio próprio** (≈ R$ 40/ano): a API fica em `api.<dominio>` com o proxy grátis do Cloudflare.
     - Cache de 30 s nos GETs públicos (`Cache-Control: public, max-age=30` em `/checks/latest` e `/checks/{id}`): corta banda e CPU.
     - Regra grátis de rate limit por IP.
     - Header secreto `X-Origin-Secret`, injetado pelo Cloudflare e conferido num middleware (exceto `/health`). Sem ele, o endereço direto `*.onrender.com` contornaria o Cloudflare. O valor entra como segredo.
   - **Sem domínio:** o proxy do Cloudflare não funciona em `*.onrender.com`. O risco de suspensão por banda fica **aceito e registrado**. As mitigações são o cartão da contingência e o monitoramento do uso no painel.
   - **Rate limit por IP no app:** só depois de confirmar qual header de IP o Render preenche de forma confiável. Na R1, uma rota de diagnóstico temporária registra os headers recebidos. Até lá vale só o limite por usuário. Com `--forwarded-allow-ips='*'`, o início do `X-Forwarded-For` pode ser forjado.

### Resultado da R0 (medido em 10/10/2026, máquina local, 1 CPU)
| Medida | Valor | Projeção a 0,1 CPU |
|---|---|---|
| RSS do app parado | 85 MB | igual |
| Extração: matéria do g1 (1,2 MB de HTML) | 0,29 s | ~2,9 s |
| Extração: página grande da Wikipédia (1,1 MB) | 0,83 s | ~8,3 s |
| 5 extrações grandes ao mesmo tempo | pico de 151 MB | — |
| Suíte com integração (pool reaproveitando conexão) | 7,6 s (antes ~15 s) | — |

Decisões: `FETCH_MAX_BYTES` fica em **2 MB** (1 MB cortaria a matéria do g1); `EXTRACT_CONCURRENCY=1`;
`--limit-concurrency` **100** (a memória não é o gargalo; o uvicorn conta conexões abertas, inclusive
keep-alive, e responde 503 também ao health check do Render; revisão de segurança H1). Sem `rootDir` no
Blueprint (ambíguo para os caminhos do Docker): `buildFilter` em `backend/**`. Cache do `/health/ready`: 60 s se ok, 10 s se falha,
uma verificação por vez; o WARNING sai uma vez por verificação real (revisão M2).

## Fase R1: Render (usuário cria no painel; Claude confere)
1. Conta no Render, **sem cartão**. *New → Blueprint* apontando para o repositório (lê o `render.yaml`), ou *New → Web Service* com runtime Docker. Região **Virginia**.
2. Variáveis no painel (lista abaixo). O Render mostra os valores a quem tem acesso ao workspace: **não adicionar outros membros**. Fica de fora da lista quem usa o valor padrão do `config.py` (`GEMINI_THINKING_BUDGET`, limites `CHECK_*`, `FETCH_TIMEOUT_SECONDS` etc.).
3. Primeiro deploy. Conferir:
   - `/health` → 200;
   - `/health/ready` → `database: ok` (verify-full com o certificado);
   - `/docs`, `/redoc` e `/openapi.json` → 404;
   - logs JSON sem segredo;
   - tempo de `/health/ready` antes e depois de aquecido (efeito do pool).

### Variáveis do Render
| Nome | Segredo? | Valor |
|---|---|---|
| `DATABASE_URL` | **Sim** | do `backend/.env` (pooler, transaction mode, porta 6543) |
| `SUPABASE_SECRET_KEY` | **Sim** | do `backend/.env` (`sb_secret_…`, já rotacionada) |
| `BAN_HASH_SALT` | **Sim** | do `backend/.env` (**o mesmo valor**: o banco é o mesmo) |
| `GEMINI_API_KEY` | **Sim** | do `backend/.env` (chave do `gen-lang-client-…`, nível gratuito) |
| `GOOGLE_FACTCHECK_API_KEY` | **Sim** | do `backend/.env` (chave do projeto `checafatosrr`) |
| `SUPABASE_JWT_SECRET` | **Sim** | **só se** não estiver vazia no `backend/.env` |
| `APP_ENV` | Não | `production` (o Dockerfile já define; repetir não faz mal) |
| `LOG_LEVEL` | Não | `INFO` |
| `CORS_ORIGINS` | Não | `https://<projeto>.pages.dev` (provisório: `http://localhost:5173`) |
| `SUPABASE_URL` | Não | `https://<project_ref>.supabase.co` |
| `ADMIN_EMAIL` | Sensível (identifica o admin) | seu e-mail de admin |
| `DATABASE_SSL_ROOT_CERT` | Não | `/srv/certs/supabase-ca.crt` |
| `GEMINI_MODEL_FALLBACKS` | Não | `gemini-3.8-flash,gemini-3.7-flash` (no Render, vírgula no valor não é problema) |
| `GEMINI_DAILY_CHECKS` / `GEMINI_DAILY_MODERATION` | Não | pelo RPD (pendente) |
| `DB_POOL_SIZE` / `DB_POOL_MAX_OVERFLOW` | Não | `3` / `2` (novas, fase R0) |
| `EXTRACT_CONCURRENCY` | Não | `1` (nova, fase R0) |
| `FETCH_MAX_BYTES` | Não | não definir (padrão 2 MB, pela medição da R0) |
| `ORIGIN_SECRET` | **Sim** | só com domínio próprio + Cloudflare (item R0.9) |
| `PORT` | — | **não definir**: o Render injeta |

## Fase R2: Cloudflare Pages
Como no plano anterior, com `VITE_API_URL=https://<servico>.onrender.com`. A CSP é gerada no build com essa URL.

## Fase R3: amarrar URLs e testar
- CORS no Render, depois Site URL e Redirect no Supabase, origem no Google OAuth (projeto `checafatosrr`) e app OAuth "Em produção".
- Teste do usuário: login, checagem de link e de texto (uma delas com página grande, para medir o tempo no 0,1 CPU), link público e "Últimas".

## Keep-alive (pedido 3)
- **UptimeRobot (grátis, sem cartão), monitor HTTP em `/health` a cada 5 min.** Mantém o serviço acordado (5 < 15 min) e avisa por e-mail se cair. Custo de banda desprezível (~200 bytes × 8.900 por mês ≈ 2 MB).
- **Segundo monitor, a cada 60 min, em `/health/ready`:** toca o banco e mantém o Supabase Free ativo, que pausa depois de 7 dias sem uso.
- **GitHub Actions cron: não recomendo.**
  - O repositório é privado, e cada execução conta pelo menos 1 min dos 2.000 grátis por mês. A cada 10 min seriam ~4.460 min/mês, acima da cota.
  - O agendamento do GitHub atrasa ou pula execuções em horário de pico.
  - Fica só como reserva manual no dia 25/10, se o UptimeRobot falhar.
- **Horas:** 744 h em outubro < 750 h, com 1 serviço só. Em novembro (30 dias), 720 h.
- Alertas do UptimeRobot por e-mail **e** push, com uma queda simulada para testar. O plano grátis dele é para uso não comercial; o projeto é sem fins lucrativos (anúncios só cobrem custos), mas confira os termos.
- **Política do Render sobre ping de keep-alive:** a doc não proíbe nem permite explicitamente. Risco baixo, registrado.

## Risco de cold start em 25/10 (pedido 3)
- Com o keep-alive, o serviço não dorme. Mas o Render pode **reiniciar o Free a qualquer momento**, e cada reinício custa ~1 min fora do ar. Num pico de apuração, isso é o pior momento.
- O 0,1 CPU também limita a vazão: o JSON é barato, mas várias checagens novas ao mesmo tempo disputam a CPU (parse e TLS).

## Contingência para 25/10 (pedido 6)
1. **Até 20/10:** cadastrar um **cartão virtual com limite baixo** (ou pré-pago) no Render, ligar os **alertas de gasto** do Render e **testar a mudança de plano uma vez** (Free → Starter → Free) fora do pico, medindo quanto tempo o serviço fica fora na troca. Sem cartão não existe contingência paga.
2. **24/10 à noite:** subir para **Starter** (0,5 CPU, sempre ligado, sem as 750 h e sem suspensão por tráfego; US$ 7/mês, cobrado proporcionalmente — **confirmar a forma de cobrança na página de preços**).
   - Ligar a forma de pagamento também tira o risco de **suspensão por banda** (5 GB/mês): o excedente vira cobrança de US$ 0,15/GB, em vez de suspender.
3. **Se mesmo assim faltar:** Standard (1 CPU, 2 GB, US$ 25/mês) só no dia 25. Com o pool, o limite do banco (Supabase Free) passa a ser o gargalo provável. Monitorar.
4. **26/10:** voltar para o Free e **remover o cartão**.
   - Com cartão cadastrado, a suspensão por banda vira cobrança sem teto (US$ 0,15/GB): o limite do cartão virtual é o teto (security-reviewer H3).
   - **2FA** no Render, GitHub, Supabase, Cloudflare e UptimeRobot: uma conta invadida com cartão gera cobrança.
5. **Plano B independente:** Cloud Run, com o PR #2 já pronto (exige o pré-pagamento no GCP).

## Banda (5 GB/mês) — estimativa
- Resposta típica: `/checks/{id}` ≈ 3 KB, "Últimas" (20 itens) ≈ 5 KB, `/health` ≈ 0,2 KB, mais ~0,5 KB de cabeçalhos.
- 5 GB ≈ **1 milhão de respostas médias** no mês. Cabe com folga no tráfego esperado. Um ataque de leitura em massa pode esgotar a banda e **suspender o serviço até o fim do mês**.
- Mitigação:
  - a forma de pagamento da contingência (item 1 acima);
  - cache de 30 s em memória para `/checks/latest` (reduz CPU e banco, não banda);
  - opcional: pôr a API atrás do Cloudflare (grátis) no futuro.

## Atualizações de documentação e memória (pedido 7)
- `docs/DEPLOY.md`:
  - nova seção "Backend no Render" como **principal**: blueprint, variáveis, região, keep-alive e contingência;
  - o Cloud Run vira "Plano B" (as instruções continuam, inclusive a conta de serviço dedicada);
  - o antigo "Plano B — Render" é absorvido.
- `.claude/plans/deploy.plan.md`: as fases 1 e 2 apontam para este plano.
- Memória: decisão Render + contingência + prompt congelado em 22/10 (já existe).

## Riscos
| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| 0,1 CPU lento para parse de página grande | Alta | Média | `EXTRACT_CONCURRENCY=1`; medir; `FETCH_MAX_BYTES` menor; Starter no dia 25 |
| Reinício do Free no pico | Média | Alta | Starter em 24–26/10 |
| Suspensão por banda (5 GB) ou por horas (750 h) | Baixa | **Muito alta** (fora do ar até o fim do mês) | Forma de pagamento até 20/10; um único serviço Free; monitorar o uso no painel |
| Keep-alive interpretado como abuso | Baixa | Média | Intervalo de 5 min, só `/health`; Starter na reta final |
| Pool + Supavisor (transaction mode) | Baixa | Média | Teste de integração com conexão derrubada; pre-ping; `pool_recycle` |
| Latência Virginia → São Paulo (~120 ms) | Certa | Baixa | Pool (economiza ~0,5 s por requisição) |
| OOM em 512 MB | Média | Média | Medição de RSS + `--limit-concurrency` |
| Supabase Free pausar | Baixa | Alta | Monitor horário em `/health/ready` |

## Respostas do usuário (10/10/2026)
- Cartão: decisão em aberto; as duas contingências estão em `docs/DEPLOY.md` §3.5.
- `render.yaml` no repositório: **sim**.
- UptimeRobot: o usuário cria a conta (com 2FA); monitores em `docs/DEPLOY.md` §3.4.
- Domínio: decisão em aberto; se registrar `checafatosrr.com.br`, DNS em `docs/DEPLOY.md` §3.6.

## Perguntas para o usuário (originais)
1. Aceita cadastrar um cartão no Render **até 20/10**, só para a contingência (Starter de 24 a 26/10 e proteção contra suspensão por banda)?
2. `render.yaml` no repositório (infraestrutura como código), ou configurar só pelo painel? Recomendo o `render.yaml`.
3. UptimeRobot: você cria a conta (é rápido), ou prefere outra ferramenta de monitoramento grátis?
4. **Domínio próprio** (≈ R$ 40/ano) para pôr a API atrás do Cloudflare (cache, rate limit por IP, proteção da banda)? Sem ele, o risco de suspensão por banda fica aceito.

## Revisão de segurança do plano (security-reviewer, 10/10/2026)
Nenhum CRITICAL. Os 3 HIGH (banda como negação de serviço, esgotamento do pool via `/health/ready`, cartão sem teto) e os MEDIUM (pool em transaction mode, header de IP, blueprint, keep-alive) estão incorporados acima. Pontos LOW registrados:
- tirar o `http://localhost:5173` do CORS assim que o Pages existir;
- **dev local e produção usam o mesmo banco do Supabase**: os testes de integração continuam só no Postgres local, nunca no Supabase;
- a cifra de 5 GB de banda vem de um blog: confirmar na página oficial de preços do Render.

## Aceite
- [ ] R0 mergeada com CI verde; medições (pool, extração, memória) registradas
- [ ] `/health/ready` 200 no Render com verify-full; `/docs` 404
- [ ] Keep-alive ativo; nenhum outro serviço Free no workspace
- [ ] Teste de produção do usuário OK
- [ ] Contingência testada até 20/10 (se a pergunta 1 for sim)
