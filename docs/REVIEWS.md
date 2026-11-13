# Reviews completas, anonimizadas e classificadas por queixa

Extensão do kit: além dos 5 reviews do painel, coleta a lista completa de cada lugar, grava de forma anonimizada num SQLite e classifica por tema de queixa com um dicionário de palavras que você edita.

## Usar

```bash
python3 scripts/engine.py start
# abra http://localhost:8080/parametrizacao
```

Abas: **Busca** (palavras-chave, região, profundidade, proxies) · **Reviews** (quantas por lugar, janela de dias, filtros de lugar, ritmo, fila de coleta) · **Dicionário** (temas, termos, peso, solução que você vende, teste de texto) · **Resultados** (ranking por dor, busca por palavra-chave, CSV).

A coleta de reviews entra na fila e começa quando a busca de lugares termina. Os dois nunca falam com o Google ao mesmo tempo (`engine/gate.py`).

## O que é gravado (`.engine/data/reviews.db`)
| Campo | Observação |
|---|---|
| `review_url`, `url_exact` | link da review no Google; se o Google não trouxe o link exato, vale o do lugar e `url_exact` é 0 |
| `data`, `data_precisao` | data exata se o Google mandou o carimbo; senão a aproximada ("há 2 meses" → mês) |
| `nota`, `texto` | texto sem telefone, e-mail, CPF, CNPJ, links e @usuários |
| `autor_id` | hash com sal local (`reviews.salt`): conta autores distintos sem saber quem são |
| `resposta_dono` | se o dono respondeu |
| `temas`, `termos` | queixas achadas e a palavra que disparou |

Nunca são gravados nome, foto ou perfil do autor.

## API
`POST /api/v1/review-jobs` (`source_job` = id de um job de leads, `max_reviews`, `since_days`, `min_reviews`, `max_place_rating`, `max_places`, `pace`, `max_time`, `sort_newest`, `lexicon`) · `GET /api/v1/review-jobs[/<id>]` · `GET /api/v1/reviews/leaderboard|search|place|export` · `POST /api/v1/reviews/reclassify` · `GET/PUT/DELETE /api/v1/lexicons/<nome>` · `POST /api/v1/lexicons/test` · `GET/PUT/DELETE /api/v1/profiles/<nome>`. Com `SCRAPER_API_KEY` definido, mande `X-API-Key`.

## Dicionário
JSON editável na página ou por arquivo. Termos sem acento ou com; `*` no fim aceita variações (`demora*`); mais de uma palavra é uma expressão; "não demorou" não conta como queixa. Uma review só conta se a nota for até `nota_maxima_queixa` (padrão 3), a menos que `incluir_notas_altas` esteja ligado. Preset: `engine/presets/lexicon-ptbr-geral.json`.

## Se a coleta parar de trazer reviews
O Google muda o formato interno sem aviso. `REVIEWS_DEBUG_DIR=/tmp/lp` guarda as 3 primeiras respostas brutas; `python3 scripts/reviews_probe.py /tmp/lp/listugcposts-1.json` mostra onde cada campo está (sem imprimir texto) e `LAYOUT` em `engine/reviews.py` é onde se corrige. Apague a pasta depois: ela tem reviews reais.

## Avisos
- Coletar reviews em massa vai contra os Termos de Serviço do Google; use ritmo lento e proxies se escalar, e avalie o risco com um advogado.
- Dado pseudonimizado ainda pode ser dado pessoal (LGPD) se der para reidentificar; o link da review reidentifica. Use-o só para o cliente conferir.
- Os testes (`python3 engine/test_reviews.py`) usam respostas sintéticas: provam o código, não que o Google ainda usa essas posições.
