# Avaliações

Medições feitas com a biblioteca real. Cada seção diz quando foi medida e com quantas faixas analisadas, porque os números mudam conforme a análise de áudio avança.

## Estabilidade dos pedidos via MCP (#34)

**Data:** 2026-11-03 · **Faixas analisadas:** ~316 de 3417 · **Critério:** Jaccard ≥ 0,8 entre duas traduções do mesmo pedido.

**Método:** cada pedido rodou em duas conversas novas e independentes do Claude (`claude -p`, só com o servidor MCP do curator, script em `planejamento/estabilidade/`). O modelo montou a receita, executou e devolveu o JSON final, sem salvar, para o cache por pedido não interferir. As duas receitas de cada pedido foram executadas no fim **sobre o mesmo estado do banco**, porque o worker continuava gravando análises durante o teste.

| Pedido | Receitas idênticas | Faixas | Jaccard |
|---|---|---|---|
| "…dirigir, músicas que me deem vontade de sair fazendo drift" | sim | 50 / 50 | 1,00 |
| "Quero uma playlist de rap" | sim | 7 / 7 | 1,00 |
| "Quero uma playlist de rock geral" | sim | 58 / 58 | 1,00 |
| "…para dormir, mas não inclua nenhuma da Hoyoverse" (1ª rodada) | não | 8 / 11 | **0,58** |
| idem, depois do ajuste nas instruções | sim | 8 / 8 | 1,00 |

Os pedidos "onde colocar as não alocadas" e "sugira playlists" usam ferramentas determinísticas (`classificar_nao_alocadas`, `sugerir_reorganizacao`): o mesmo banco dá o mesmo resultado por construção.

**O que causou o 0,58:** com poucas faixas analisadas, a receita calma devolveu só 8 músicas. Numa das conversas o modelo afrouxou os filtros por conta própria (BPM até 140, `min.relaxed`, mais humores) e na outra não. A variação veio dessa iniciativa, não da tradução do pedido.

**Correção:** as instruções do servidor MCP passaram a proibir afrouxar filtros sem o usuário pedir. Com poucas faixas, o modelo mostra o resultado, diz quantas estão sem análise e oferece o ajuste. Também passou a partir do exemplo mais parecido em `exemplos_de_receitas`. Depois disso, as duas conversas deram a mesma receita.

**Observação:** nas duas conversas, o pedido "sem Hoyoverse" achou sozinho, via `buscar_artistas`, um segundo artista além do HOYO-MiX ("HoYoFair") e excluiu os dois.

## Classificação nas filhas do Rock - All (#28)

Ainda sem dados suficientes: o leave-one-out precisa de pelo menos 5 membros analisados por playlist. Rodar `curator avaliar-classificacao --mae "Rock - All"` quando a análise cobrir as filhas.
