# EVAL: checagem-veredito

**Origem:** teste manual de 09/10/2026. Uma notícia real do g1 do mesmo dia virou "Não verificável" com o ponto suspeito "acontecimentos datados de outubro de 2026, projetando fatos futuros". O modelo não sabia a data atual.

## Métricas (grader de código: `backend/evals/checagem.py`)
- **Falso "Não verificável":** o caso esperava um veredito firme e veio `nao_verificavel`.
- **Falso "Falso":** veio `falso` num caso que não aceita `falso` (o erro mais grave para notícia real).
- **IA ainda errou a data:** a resposta crua do modelo, antes do filtro, tem o erro de data. Mede se o prompt (data de hoje e regra 9) funciona.
- **Revisar à mão:** pontos com "futuro" ou "ainda não ocorreu" que o filtro manteve. O filtro é conservador de propósito, então uma pessoa decide se são avisos legítimos ou erros que passaram.
- **Acertos:** o veredito está na lista `aceitos` do caso.

## Casos (`backend/evals/casos_checagem.json`)
| Categoria | Casos | O que mede |
|---|---|---|
| noticia_real | 3 | falso "Não verificável" e falso "Falso" em notícia verdadeira; inclui a reprodução do bug |
| boato_checado | 3 | boato recorrente sendo apontado como falso ou enganoso |
| whatsapp | 3 | corrente sem fonte; mensagem correta (não pode virar "Falso"); data recente |

Todos os casos atuais são **sintéticos**: escritos para o eval, com fatos estáveis ou boatos recorrentes e sem URL inventada. Para medir de verdade, troque-os por notícias reais e por checagens reais da Lupa e do Aos Fatos, com a URL real da checagem.

## Como rodar
- **Replay (grátis, sem chamar nada):** `python -m evals.checagem --replay evals/gravacoes/<arquivo>.json`. Reavalia respostas gravadas com a pós-validação atual.
- **Real (só com autorização, no máximo 3 chamadas):** `CHECAFATOS_EVAL_REAL=1 python -m evals.checagem --real --casos id1,id2,id3`. Usa um modelo só e nenhuma nova tentativa; a cota é travada em 3 reservas. A gravação vai para `evals/gravacoes/`.

## Critério de aceite (com casos reais)
- falso "Não verificável" em `noticia_real` = 0
- falso "Falso" em `noticia_real` e `zap-informacao-correta` = 0
- IA ainda errou a data em `noticia_real` = 0 (o filtro é rede de segurança, não a correção)

## Histórico
- 09/10/2026: harness criado. Rodada real pendente: a cota diária do Gemini ainda não tinha zerado, e a autorização é de no máximo 3 chamadas.
