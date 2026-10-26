# Avaliação com gabarito

Mede a plataforma (o `main`) contra casos conferidos por pessoas. Ela roda a checagem de verdade (leitura do link, Fact Check, Gemini e salvaguardas), sem gravar nada no Supabase e sem cache.

- `casos.yaml`: os casos e o gabarito.
- `rodar_avaliacao.py`: o script.
- `resultados/`: os relatórios (`AAAA-MM-DD_<commit>.md` e `.json`).

## Vocabulário de `resultado_esperado`

Só estes valores, escritos exatamente assim:

| valor | quando usar |
|---|---|
| `verdadeiro` | a agência confirma o conteúdo. Pela política da plataforma, "Verdadeiro" exige checagem de agência que corrobore |
| `falso` | a agência classifica como falso |
| `enganoso` | enganoso, distorcido ou exagerado |
| `fora_de_contexto` | conteúdo real, mas usado fora do contexto (data, lugar ou assunto) |
| `nao_verificavel` | não há checagem nem fonte que permita concluir (comum em fato sem checagem e em boato sem checagem) |
| `checagem_de_agencia` | só para `link_agencia`: o link enviado é a própria checagem, e a página deve mostrar "Este link é uma checagem publicada por…" |

Qualquer outro valor (por exemplo `Falso`, com maiúscula, ou `mentira`) faz o script recusar o arquivo.

Tipos de caso (`tipo`): `boato_checado`, `fato_verdadeiro`, `link_agencia`, `noticia_legitima` e `boato_sem_checagem`.

## Como rotular

1. Abra a checagem da agência citada no comentário `candidato:` do caso. O comentário é só uma pista; confira na própria agência.
2. Preencha os campos:

   ```yaml
   resultado_esperado: falso               # um valor da tabela acima
   agencia_esperada: Aos Fatos             # opcional
   url_checagem_esperada: https://...      # opcional; com ele o relatório mede "checagem certa encontrada"
   fonte_do_gabarito: https://...          # obrigatório: o link que sustenta o gabarito
   rotulado_por: Caio                      # no lugar de PENDENTE
   data: 2026-10-27                        # dia da conferência
   ```

3. Regras do preenchimento:
   - Um caso só entra na avaliação quando `rotulado_por` deixa de ser `PENDENTE`.
   - Um caso rotulado pela metade é recusado: faltar `resultado_esperado` válido, `fonte_do_gabarito`, `data` ou a entrada.
   - Em `noticia_legitima`, troque `entrada: PREENCHER` por um link real. Nunca invente URL.
   - Em `boato_sem_checagem`, os textos são fictícios. Antes de rotular, confira que nenhuma agência checou o assunto.
4. Rode os testes, que validam o arquivo: `pytest tests/unit/test_eval_rodar_avaliacao.py`.

## Como rodar

Chamada real só com autorização. As travas são `CHECAFATOS_EVAL_REAL=1` e o orçamento obrigatório. As chaves vêm da configuração do app (`CHECAFATOS_DOTENV=1`) e nunca são impressas. A partir de `backend/`:

```bash
CHECAFATOS_EVAL_REAL=1 CHECAFATOS_DOTENV=1 python -m eval.rodar_avaliacao --modelos gemini-3.7-flash --orcamento 30
```

Rodada em 2 noites de 15 casos (com um modelo, o pior caso é 2 chamadas por checagem, então 15 casos cabem em 30):

```bash
# noite 1: boato_checado (10) + link_agencia (5)
CHECAFATOS_EVAL_REAL=1 CHECAFATOS_DOTENV=1 python -m eval.rodar_avaliacao --modelos gemini-3.7-flash --orcamento 30 --tipos boato_checado,link_agencia
# noite 2: fato_verdadeiro (5) + noticia_legitima (5) + boato_sem_checagem (5)
CHECAFATOS_EVAL_REAL=1 CHECAFATOS_DOTENV=1 python -m eval.rodar_avaliacao --modelos gemini-3.7-flash --orcamento 30 --tipos fato_verdadeiro,noticia_legitima,boato_sem_checagem
```

Outras opções:
- `--ids a,b,c`: só esses casos.
- `--orcamento-factcheck N`: teto de consultas à Fact Check.
- `--intervalo 21`: segundos entre checagens.

O script para:
- quando o próximo caso não cabe no orçamento;
- num 429;
- em 2 falhas seguidas de timeout, 503 ou 504.

Ele grava o relatório mesmo se a execução cair no meio.

**Atenção:** a avaliação usa a mesma chave do Gemini da produção. As chamadas gastam a cota diária do Google sem o contador do banco da produção saber. Rode fora dos horários de pico.

## O relatório

| seção | o que mostra |
|---|---|
| Consumo | modelo usado (e qual respondeu), chamadas ao Gemini gastas e consultas à Fact Check gastas (teto: as consultas enviadas) |
| Linhas vermelhas | **devem ser 0**. São elas: `verdadeiro_em_boato`, `falso_em_fato`, `verdadeiro_sem_agencia`, `link_de_agencia_nao_reconhecido` e `agencia_de_outro_assunto_sustentando` (a mesma regra da salvaguarda do #43) |
| Veredito só da IA, sem agência citada | métrica, não linha vermelha: quantos vereditos categóricos saíram sem nenhuma checagem de agência citada, e quais casos |
| Checagem certa encontrada | entre os avaliados com `url_checagem_esperada`, em quantos a checagem esperada apareceu (citada, relacionada ou como a própria página) |
| Concordância de veredito | acertos e %, e kappa de Cohen com 10 casos avaliados ou mais |
| Tempo | mediana e pior caso |
| Falhas da IA por modelo | `503`, `429`, `timeout` ou `sem_status` (resposta vazia ou bloqueio) |

Situação de cada caso: `avaliado`, `nao_avaliado` (a IA não respondeu) ou `erro:<código>` (por exemplo `erro:pagina_ilegivel`).
