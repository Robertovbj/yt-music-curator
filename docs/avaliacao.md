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

**Data:** 2026-11-04 · **Faixas analisadas:** 3407 de 3417 (fila completa) · **Escopo:** as 5 filhas do Rock - All, 160 faixas analisadas, 23 delas em duas filhas.

**Ressalva que decide a leitura:** as filhas do Rock foram montadas por uma IA, a pedido do usuário, com critério desconhecido. Elas vão ser recategorizadas (#74). Então os números abaixo medem **concordância com essa divisão**, não acerto. Por isso nenhum parâmetro do motor mudou por causa deles.

**Método:** leave-one-out. Cada membro sai de todas as filhas, os perfis (centroide, vizinhos e limiar τ) são recalculados sem ele, e a regra decide para onde ele iria. Precisão e revocação são por filha; o resumo é o F1 macro das 5. Duas ferramentas:
- `curator avaliar-classificacao --mae "Rock - All"`: a regra do motor de ponta a ponta, com o filtro da mãe.
- `contrib/avaliacao/grade_classificacao.py --mae "Rock - All"`: a grade de espaços, k, α e regras, e a heurística antiga. Aqui a mãe não filtra, porque toda faixa avaliada já é dela.

**Regra atual do motor** (effnet, k = 10, α = 0,6, τ no percentil 10, multi-rótulo):

| Filha | Membros | Precisão | Revocação |
|---|---|---|---|
| Arena Rock & Melodic Alternative | 43 | 0,25 | 0,88 |
| Epic, Folk & Battle Metal | 46 | 0,38 | 0,89 |
| Industrial, Cyberpunk & Electronic Metal | 19 | 0,18 | 0,90 |
| J-Rock, Kawaii Metal & Eastern Energy | 29 | 0,24 | 0,86 |
| Modern Core & Dark Atmospheric | 46 | 0,55 | 0,89 |

A revocação perto de 0,9 vem da construção do limiar (90% dos membros passam no próprio τ). A precisão baixa vem de a faixa passar no limiar de várias filhas (3,35 das 5, em média, sem o filtro da mãe): o som não separa essas filhas o bastante para limiares independentes.

**Grade** (F1 macro; k = 10 e α = 0,6 em todas as linhas):

| Espaço | Limiar (atual) | Só a filha de maior score | Maior score + artista (β = 0,75) |
|---|---|---|---|
| EffNet-Discogs | 0,45 | 0,70 | 0,81 |
| Interpretável | 0,44 | 0,69 | 0,79 |
| MusiCNN | 0,42 | 0,49 | 0,74 |

- **Heurística antiga** (a filha cujo top-15 de artistas tem o artista da faixa, sem a própria faixa): F1 macro 0,64, com precisão de 0,56 a 1,00, mas 41% das faixas sem destino.
- **k e α** quase não mexem na regra atual: entre k ∈ {5, 10, 20} e α ∈ {0; 0,6; 1}, o F1 fica entre 0,45 e 0,46. Na escolha entre irmãs, só k = 20 com α = 1 (vizinhos puros, sem centroide) piora claramente (0,52 sem artista, 0,76 com). O percentil do limiar (10, 25, 50) troca revocação por precisão sem passar de 0,50.
- **Espaços:** o EffNet concorda mais com a divisão que o espaço interpretável (por 0,01 a 0,03) e bem mais que o MusiCNN.

**Leitura:**
- O artista principal explica boa parte da divisão: ele sozinho concorda mais (0,64) que a regra atual pelo som (0,45), e somado ao som leva a concordância de 0,70 a 0,81. Isso sugere que a IA dividiu o Rock em boa parte por artista, o que confirma que a divisão atual não é uma boa régua.
- Calibrar o motor nela (adotar o bônus de artista, por exemplo) só ensinaria o critério daquela IA. A regra "maior score entre as irmãs, mais um bônus de artista" fica registrada como hipótese, para medir de novo sobre filhas em que o usuário confia.
- O sinal que vale para qualquer divisão: com limiares independentes por filha, irmãs de som parecido aceitam a mesma faixa quase sempre. Se isso se repetir nas filhas novas, a escolha entre irmãs pelo maior score é a correção natural.

**Parâmetros:** ficam k = 10, α = 0,6, τ no percentil 10, espaço EffNet e decisão por limiar. Nenhum dado confiável indicou outra coisa.

**Próximo passo:** #74, recategorizar as filhas do Rock pelo áudio (`curator reorganizar --mae "Rock - All" --reagrupar`), revisar com o usuário e repetir esta avaliação. A nova medição vai ser em parte circular, porque as filhas novas saem do mesmo embedding.

### As outras playlists (montadas pelo usuário)

Para ter uma medida sobre rótulos confiáveis, o mesmo leave-one-out rodou em todas as folhas, com a regra atual (`curator avaliar-classificacao`, sem `--mae`; 2.574 faixas analisadas, 10 min). Fora do Rock, todas são raízes.

- **Revocação ~0,9 em quase todas**, como manda a construção do limiar. A exceção é Madoka (5 membros, 0,00): com o mínimo de membros, o perfil não segura.
- **Precisão baixa em quase todas:** de 0,01 (Genshin Absolute Best) a 0,51 (Anime). Só Valorant Plays chega a 1,00. Exemplos: Calm and Chill 0,36, Genshin OST 0,33, Japanese music 0,23, Pop 0,12, Clair Obscur 0,11, Wait, its all Nier? 0,08, Basically, osu! 0,02.

Dois motivos, ambos visíveis nos dados:
- **Boa parte das playlists é por franquia ou origem** (Genshin, Honkai, Octopath, FFXIV, Nier, Clair Obscur, Madoka, HoloMusic), não por som. O áudio não diz de que jogo é a trilha.
- **Playlists grandes e variadas ficam com limiar baixo** (Genshin OST tem 451 faixas), porque τ é o percentil 10 dos próprios membros. Elas aceitam quase qualquer faixa.

Com essa regra, `curator classificar` sugere playlists demais para cada faixa não alocada nas raízes. Isso pede outra calibração (limiar mais alto, escolha entre as raízes ou deixar de fora as playlists por franquia), registrada na #75. O teste não diz qual: a precisão também cai quando a faixa cabe de verdade numa playlist em que não está.

## Gênero-pai (#83)

**Data:** 2026-11-05 · **Faixas analisadas:** 3.322 · **Decisão:** ADR-14 · **Script:** `contrib/avaliacao/genero_pai.py` (o gabarito é pessoal e fica fora do repositório).

**Gabarito, sem nenhuma playlist do usuário:**
- **Por artista:** artistas de gênero consistente, rotulados pela IA e revisados pelo usuário. São 40 faixas de Hip Hop, 140 de Rock, 176 de Pop e 32 de Electronic.
- **Faixa a faixa, rap:** 43 rap e 21 não rap, os casos difíceis da sessão em que o problema apareceu ("Without Me", Tauz e NF contra Heat Waves, Stressed Out, Alec Benjamin).
- **Ressalva:** o gabarito por artista favorece qualquer sinal que use o artista ou vizinhos do mesmo artista (Flexer, 2007). Por isso, esses sinais foram julgados só no gabarito faixa a faixa e com intervalo de confiança.

**AUC** (chance de uma faixa do gênero ficar acima de uma de fora; IC 95% por bootstrap no gabarito faixa a faixa):

| Sinal | Rap faixa a faixa | Hip Hop | Rock | Pop | Electronic |
|---|---|---|---|---|---|
| Maior estilo (antigo) | 0,788 [0,670; 0,897] | 0,977 | 0,984 | 0,752 | 0,892 |
| Fração do Discogs | 0,931 [0,862; 0,983] | 0,995 | 0,992 | 0,803 | 0,891 |
| Afinidade (fração Discogs + Jamendo, o motor) | 0,946 [0,884; 0,989] | 0,997 | 0,996 | 0,848 | 0,866 |

- **Fração contra máximo:** +0,14 (IC +0,06 a +0,23), a mudança que de fato resolve. Na ordem nova, "Without Me" é a 19ª de Hip Hop na biblioteca.
- **Jamendo:** +0,014 (IC 0,000 a +0,037). Com o mesmo embedding, as frações concordam muito (ρ de 0,66 a 0,95).
- **Concordância entre os dois modelos, sem gabarito** (ρ de Spearman entre o sinal do Discogs e o do Jamendo, na biblioteca toda): a fração foi a melhor ou empatou nos 12 pais com equivalente no Jamendo. Electronic subiu de 0,58 (máximo) para 0,79.

**Régua relativa** (revocação / falsos positivos em cada nível):

| Gabarito | ≥ 0,7 (amplo) | ≥ 0,8 (normal) | ≥ 1,0 (estrito) |
|---|---|---|---|
| Rap faixa a faixa | 0,91 / 0,33 | 0,91 / 0,14 | 0,84 / 0,05 |
| Hip Hop por artista | 0,90 / 0,01 | 0,82 / 0,00 | 0,78 / 0,00 |
| Rock por artista | 0,90 / 0,02 | 0,84 / 0,01 | 0,51 / 0,00 |
| Pop por artista | 0,37 / 0,10 | 0,21 / 0,04 | 0,05 / 0,01 |
| Electronic por artista | 0,75 / 0,19 | 0,59 / 0,12 | 0,41 / 0,05 |

- O mesmo nível funciona para Hip Hop e Rock. O exemplo "Rap" usa 0,8; o "Rock geral" usa 0,7.
- Pop e Electronic não têm um bom corte em nenhum nível.
- Os falsos positivos do rap no 0,8 são Heat Waves (duas versões) e Stressed Out. Nenhum sinal de áudio os tira; `examinar_genero` mostra a fronteira para a IA decidir.

**Testados e deixados de fora** (detalhes no ADR-14):

| Variante | Rap faixa a faixa | Por quê ficou fora |
|---|---|---|
| + 15 vizinhos no EffNet | 0,945 | não acrescenta nada (as cabeças já são desse embedding) |
| + 15 vizinhos no MusiCNN (peso 0,3) | 0,958 [0,909; 0,992] | ganho de +0,012 com IC de −0,003 a +0,037; 24% dos vizinhos são do mesmo artista |
| + puxão de 25% para a média do artista | 0,970 | ganho vindo do gabarito por artista; na HOYO-MiX tirou 6 das 11 faixas de Rock |
| Noisy-OR dos estilos | 0,93 | infla os pais com muitos estilos (985 faixas de Electronic e 65 de Hip Hop no mesmo limite) |

Vizinhos no MusiCNN: k de 5 a 30 e peso de 0,3 a 0,7 deram AUC entre 0,93 e 0,97 no rap. O resultado é estável, e nenhuma configuração sai do ruído.

