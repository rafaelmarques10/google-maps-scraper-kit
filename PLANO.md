# Plano: achar queixas nas reviews e transformar em abordagem de venda

Objetivo: dado um nicho e uma região, listar os negócios com mais queixas que a sua solução resolve, e chegar na conversa com **provas que o cliente pode conferir** (data + link da review no Google).

## Fluxo

```
Busca de lugares ──► Reviews completas ──► Anonimização ──► Dicionário de palavras ──► Indicadores ──► Abordagem
 (kit original)       (reviews.py)         (privacy.py)     (complaints.py)             (indicators.py)   (CSV + link)
```

Tudo é parametrizado na página `http://localhost:8080/parametrizacao`: onde buscar, quantas reviews, janela de datas, nota máxima do lugar, ritmo, e o dicionário de palavras por tema.

## Fases

**Fase 0 · Validar a coleta no Google real (1 hora, antes de tudo)**
O parser lê posições do formato interno do Google e foi testado só com dados sintéticos. Em um lugar com muitas reviews:
1. `REVIEWS_DEBUG_DIR=/tmp/lp python3 scripts/engine.py start`, crie uma coleta com 1 lugar e 30 reviews.
2. `python3 scripts/reviews_probe.py /tmp/lp/listugcposts-1.json` mostra onde ficam nota, data, texto, link, id e resposta do dono, sem imprimir texto de review.
3. Ajuste `LAYOUT` em `engine/reviews.py` se alguma posição divergir, rode `python3 engine/test_reviews.py` e apague `/tmp/lp`.
Critério de saída: 30 reviews coletadas, com data, nota, texto e link da review preenchidos.

**Fase 1 · Dicionário v1 por nicho**
- Parta do preset `ptbr-geral` (12 temas) e crie um dicionário por nicho (restaurante, clínica, petshop...), salvo na aba Dicionário.
- Cada tema tem a **solução que você vende** ao lado (ex.: demora → agendamento online; "não responde" → atendimento automático no WhatsApp). O ranking passa a dizer o que oferecer a cada lead.
- Termos novos saem lendo as reviews de 1 e 2 estrelas do ranking: o que se repete e ainda não está no dicionário entra.

**Fase 2 · Busca por palavra-chave e indicadores**
- Aba Resultados: ranking por pontuação de dor (queixas por 100 reviews na janela, ponderadas pelo peso do tema), com % por tema, tendência de 12 meses e taxa de resposta do dono às queixas.
- Busca livre por palavras (qualquer/todas, nota máxima, tema) para investigar uma hipótese ("quantos reclamam de estacionamento?").
- Mudou o dicionário? **Aplicar nas reviews já coletadas** reclassifica sem novo pedido ao Google.

**Fase 3 · Calibração (antes de abordar alguém)**
1. Sorteie 100 reviews classificadas como queixa e 100 não classificadas, de lugares diferentes.
2. Marque à mão: é queixa do tema? Calcule precisão (acertos entre as classificadas) e cobertura (queixas reais achadas entre as não classificadas).
3. Meta: precisão ≥ 85%. Abaixo disso, mexa nos termos que mais erram (ironia, negação, palavra ambígua como "caro" ou "fila").
Só leve ao cliente indicadores de temas que passaram nessa conferência.

**Fase 4 · Abordagem**
- Por lead: 1 frase com o número ("12 das 48 reviews do último ano falam de demora, 25%"), 2 a 3 links de review como prova, e a solução do tema.
- Nunca copie o texto das reviews para o e-mail ou a proposta: mande o link, o cliente vê no Google.
- Registre quem foi abordado e o resultado, para medir a taxa de resposta por tema e cortar os temas que não convertem.

**Fase 5 · Escala e manutenção**
- Lotes pequenos e ritmo lento; proxies se for passar de algumas centenas de lugares.
- Reexecutar a coleta por região a cada 30 a 60 dias para ver tendência.
- Se o Google mudar o formato e a coleta parar de trazer reviews, repetir a Fase 0.

## Métricas de sucesso do piloto (20 a 30 lugares de um nicho)
- Reviews coletadas por lugar ≥ 70% do total publicado
- Precisão do dicionário ≥ 85% (Fase 3)
- Pelo menos 5 leads com score alto e contato (telefone ou e-mail) do kit
- Respostas positivas à abordagem: acompanhar por tema

## Riscos e cuidados
- **Termos do Google:** coletar em massa contraria os termos da plataforma (o README do kit já avisa). Risco de bloqueio de IP e de conta; avalie com um advogado antes de escalar.
- **LGPD:** nada de nome e foto do autor; texto sem telefone/e-mail/CPF/links; autor só como hash. Ainda assim o link da review reidentifica, então use apenas para conferência pelo cliente.
- **Fragilidade técnica:** o parser depende do formato interno do Google; use a sonda (Fase 0) sempre que a coleta falhar.
- **Falsos positivos:** o dicionário erra com ironia e contexto. A Fase 3 existe por isso.
