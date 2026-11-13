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


## Sinais novos de gênero (#88)

**Data:** 2026-11-06 · **Decisão:** ADR-15 · **Scripts:** `contrib/avaliacao/sinais_genero.py` (todos os sinais) e `contrib/avaliacao/genero_pai.py` (as duas réguas). Mesmo gabarito da #83. Medido numa cópia do banco, com os extras calculados sobre os áudios guardados pela #90. A primeira medição (noite de 05/11) tinha o MAEST em todo o rap faixa a faixa, mas só em parte do gabarito por artista. Os números abaixo são da segunda (manhã de 06/11), com o MAEST nas 388 faixas do gabarito por artista. A terceira, no banco real depois do deploy, tem a biblioteca inteira e vem logo depois da decisão.

**Rap faixa a faixa** (43 rap × 21 não rap; AUC [IC 95%]; Δ = IC pareado contra o motor v2, nas mesmas faixas):

| Sinal | AUC | Δ vs motor v2 |
|---|---|---|
| Motor v2 (Discogs + Jamendo) | 0,946 [0,884; 0,989] | — |
| MAEST, média dos trechos | 1,000 [1,000; 1,000] | +0,011 a +0,116 |
| MAEST, maior trecho | 0,994 [0,980; 1,000] | +0,006 a +0,107 |
| YAMNet, classes de gênero (média) | 0,911 [0,833; 0,968] | −0,106 a +0,028 |
| YAMNet "Rapping" − "Singing" (média) | 0,966 [0,913; 0,997] | −0,037 a +0,082 |
| Tags do MSD-MusiCNN | 0,975 [0,939; 0,997] | −0,022 a +0,090 |
| Externos (MusicBrainz + Deezer; 55 faixas com dado) | 1,000 [1,000; 1,000] | +0,013 a +0,137 |
| Fusão motor + MAEST + YAMNet + MSD | 0,992 [0,975; 1,000] | +0,010 a +0,102 |
| **Régua com o MAEST** (`relativa_com_maest`: motor + MAEST, réguas relativas) | **0,993 [0,977; 1,000]** | +0,011 a +0,100 |

**Por artista** (AUC; entre parênteses, as faixas com externos; os outros sinais cobrem as 388):

| Pai | Motor v2 | Com o MAEST | MAEST | YAMNet | Tags MSD | Externos |
|---|---|---|---|---|---|---|
| Hip Hop | 0,997 | 1,000 | 1,000 | 0,990 | 0,998 | 1,000 (31) |
| Rock | 0,996 | 0,996 | 0,992 | 0,991 | 0,969 | 1,000 (122) |
| Pop | 0,848 | 0,872 | 0,875 | 0,683 | 0,898 | 0,996 (174) |
| Electronic | 0,866 | 0,937 | 0,960 | 0,744 | 0,802 | 1,000 (32) |

- Os externos ganham no Pop e no Electronic, mas são gêneros do artista medidos num gabarito por artista. A ressalva de Flexer (2007) vale inteira aqui (ver ADR-15 e a #91).

**As duas réguas** (revocação / falsos positivos):

| Gabarito | Régua | ≥ 0,7 | ≥ 0,8 | ≥ 1,0 |
|---|---|---|---|---|
| Rap faixa a faixa | v2 (receitas) | 0,91 / 0,33 | 0,91 / 0,14 | 0,84 / 0,05 |
| | com o MAEST | 0,95 / 0,05 | 0,88 / 0,00 | 0,77 / 0,00 |
| Hip Hop por artista | v2 | 0,90 / 0,01 | 0,82 / 0,00 | 0,78 / 0,00 |
| | com o MAEST | 0,93 / 0,00 | 0,78 / 0,00 | 0,70 / 0,00 |
| Rock por artista | v2 | 0,90 / 0,02 | 0,84 / 0,01 | 0,51 / 0,00 |
| | com o MAEST | 0,87 / 0,00 | 0,73 / 0,00 | 0,46 / 0,00 |
| Pop por artista | v2 | 0,37 / 0,10 | 0,21 / 0,04 | 0,05 / 0,01 |
| | com o MAEST | 0,36 / 0,08 | 0,26 / 0,06 | 0,03 / 0,01 |
| Electronic por artista | v2 | 0,75 / 0,19 | 0,59 / 0,12 | 0,41 / 0,05 |
| | com o MAEST | 0,88 / 0,18 | 0,78 / 0,11 | 0,56 / 0,03 |

- No rap, os falsos positivos da v2 no 0,8 eram Heat Waves (0,95; 0,62 com o MAEST), Stressed Out (1,03 → 0,72) e Thunder (0,83 → 0,50). Com o MAEST, abaixo de 0,7 ficam Can't Hold Us (0,55) e Fight Back (0,62), e entre 0,7 e 0,8, The Monster, Tauz ("Call of Duty") e Houdini.
- **O rock japonês:** das 24 faixas de Rock do gabarito que caem abaixo de 0,8 com o MAEST, 22 são de kessoku band, the peggies, Houkago Teatime e BABYMETAL. O MAEST dá a elas de 0,5 a 0,8 em "Pop---J-pop". No Discogs, J-pop é uma etiqueta de mercado (música popular japonesa) que cobre rock, idol e anime. Nas 96 faixas da biblioteca com J-pop ≥ 0,5 no MAEST (cópia de 06/11), a régua com ele leva o Rock (≥ 0,8) de 34 para 12 faixas e o Pop de 34 para 58.
- **J-pop neutro (testado, não adotado):** zerar J-pop e K-pop no MAEST conserta o Rock (0,86 / 0,00 no 0,8) e sobe o Pop (AUC 0,968). Em troca, tira do Pop o pop idol e de anime (Hoshimachi Suisei, ClariS, Ado): das 96, as faixas sem nenhum pai ≥ 0,8 vão de 16 para 36. O gabarito não tem pop japonês, então não enxerga esse custo.
- **Decisão:** a régua das receitas segue a v2, e a com o MAEST fica ao lado, no `examinar_genero` (ADR-15).

**No banco real** (noite de 06/11, depois do deploy):
- **Cobertura:** a biblioteca inteira tem os extras, 3.324 faixas. O MAEST falha em 5 faixas de ≤ 30 s, e elas ficam só com a v2.
- **Núcleo:** o núcleo de cada pai passa a ser medido na biblioteca toda.
- **Régua v2:** os mesmos números da cópia.
- **Régua com o MAEST:** mudou, porque a mediana do MAEST no núcleo vinha de só uma parte da biblioteca.

Revocação / falsos positivos no corte 0,8:

| Gabarito | v2 | Com o MAEST (cópia) | Com o MAEST (banco real) |
|---|---|---|---|
| Rap faixa a faixa | 0,91 / 0,14 | 0,88 / 0,00 | 0,91 / 0,00 |
| Hip Hop por artista | 0,82 / 0,00 | 0,78 / 0,00 | 0,80 / 0,00 |
| Rock por artista | 0,84 / 0,01 | 0,73 / 0,00 | 0,79 / 0,00 |
| Pop por artista | 0,21 / 0,04 | 0,26 / 0,06 | 0,12 / 0,04 |
| Electronic por artista | 0,59 / 0,12 | 0,78 / 0,11 | 0,78 / 0,11 |

- **AUC com o MAEST:** praticamente a mesma da cópia. No rap faixa a faixa, 0,994 [0,980; 1,000]. Por artista: Hip Hop 1,000, Rock 0,996, Pop 0,871 e Electronic 0,937.
- **Segmento japonês** (790 faixas com título ou artista em escrita japonesa), da v2 para a com o MAEST, no 0,8:
  - Rock: de 111 para 85 (28 saem, 2 entram);
  - Pop: de 227 para 252;
  - Electronic: de 144 para 87;
  - o pai dominante muda em 17,5% delas, mais de Electronic para Pop (35) e de Rock para Pop (8).
- **Biblioteca toda:** o pai dominante muda em 19,2% das faixas, quase sempre entre Classical e Stage & Screen (127 e 27) ou de Electronic para Pop (96).
- **A decisão se mantém:** com o núcleo completo, a régua com o MAEST melhora no Rock, mas ainda tira rock japonês dele, e piora no Pop.

**Aprendido com os rótulos** (viabilidade para a #93): regressão logística (C = 0,05, sem ajuste) sobre EffNet + MAEST, treinada no gabarito por artista e validada deixando o artista inteiro de fora (10 dobras por artista, 65 artistas):

| AUC | v2 | Com o MAEST | Aprendido |
|---|---|---|---|
| Hip Hop | 0,997 | 1,000 | 0,999 |
| Rock | 0,996 | 0,996 | 0,993 |
| Pop | 0,848 | 0,872 | **0,982** |
| Electronic | 0,866 | 0,937 | 0,920 |
| Rap faixa a faixa | 0,946 | **0,993** | 0,704 |

- As 46 faixas de rock japonês foram todas para o Rock, com o próprio artista fora do treino.
- No rap faixa a faixa, cai para 0,704. Treinado com rótulos por artista, o modelo aprende o som do artista (Flexer, 2007), e os casos difíceis são justamente faixas fora do padrão do artista. Para essas fronteiras, precisa de rótulos por faixa.
- Só conhece os quatro pais rotulados. Os outros (trilha sonora, clássica) ficam com a régua.

**Vizinhos sem rótulo (testado, não adotado):** a média da régua com o MAEST e a dos 10 vizinhos no EffNet deu a mesma AUC (Rock 0,997, Electronic 0,929). O rock japonês continuou com 41% das faixas ≥ 0,8, porque os vizinhos dele são outro rock japonês, com a mesma nota.

**Cabeças do Essentia ainda não usadas** (AUC; as 411 faixas dos dois gabaritos, com o EffNet recalculado sobre o áudio guardado; fração dos rótulos de cada pai, com o mapa definido antes de olhar; "—" = a cabeça não tem o pai):

| Cabeça | Hip Hop | Rock | Pop | Electronic | Rap faixa a faixa |
|---|---|---|---|---|---|
| Motor v2 | 0,997 | 0,996 | 0,848 | 0,866 | 0,946 |
| MAEST (fração) | 1,000 | 0,992 | 0,875 | 0,960 | 1,000 |
| FMA small | 0,998 | 0,998 | 0,829 | 0,798 | 0,960 |
| MagnaTagATune | — | 0,998 | 0,895 | 0,771 | — |
| MTG-Jamendo top50tags | 0,997 | 0,997 | 0,912 | 0,843 | 0,940 |
| MTG-Jamendo sobre o embedding "multi" | 0,997 | 0,996 | 0,856 | 0,765 | 0,956 |
| Dortmund | 0,997 | 0,991 | 0,883 | 0,853 | 0,967 |
| Rosamerica | 0,995 | 0,998 | 0,888 | 0,826 | 0,915 |
| GTZAN | 0,992 | 0,991 | 0,889 | — | 0,878 |

- Nenhuma passa o MAEST no Rock, no Electronic ou no rap. A melhor no Pop (top50tags, 0,912) fica longe do modelo aprendido (0,982).
- Todas rodam sobre o mesmo EffNet do núcleo, então trazem pouca diversidade (a lição do ADR-14). As antigas (Dortmund, Rosamerica, GTZAN, subgêneros de Electronic) saíram da documentação do Essentia e foram treinadas com 250 a 1.820 trechos.
- Ficam como candidatas a atributo do modelo aprendido (#93), não como régua.

**Custo** (faixa de ~3 min 30 s, CPU de 8 núcleos): núcleo ~7 s; tags do MSD ~2,4 s; YAMNet ~0,9 s; MAEST ~12 s. A RAM do worker vai de 1,5 GB para 2,8 GB. Metadados: ~1,5 s por faixa no Deezer e ~4,4 s no MusicBrainz (1 pedido/s). Na amostra, 88% casaram no Deezer e 90% no MusicBrainz, antes do casamento mais tolerante a títulos "original - tradução".

### Os extras guardados valem o custo? (13/11)

O `msd_tags` e o `yamnet` ficaram guardados desde a #88 para esta conferência. Custam 2,65 s por faixa (o MAEST, 10 s; o núcleo, 6,5 s) e 28 MB dos 88 MB de vetores. Medido numa cópia do banco, cada um como sinal a mais:

| Uso | Só o que já é usado | Melhor resultado com os extras |
|---|---|---|
| Acertar a master da faixa (leave-one-out, 2.550 faixas) | 0,949 (EffNet) | 0,951 (+ YAMNet); + tags do MSD: 0,925 |
| Gênero aprendido, AUC sem o artista (Hip Hop / Pop / Rock / Electronic) | 0,976 / 0,965 / 0,972 / 0,921 (EffNet + MAEST) | diferenças de até 0,005 |
| Prever as marcações, AUC sem o artista (k-pop / pop-ocidental / vtuber) | 0,905 / 0,971 / 0,974 (EffNet + MAEST) | 0,906 / 0,971 / 0,979 |
| Cantada × trilha (masters como gabarito aproximado), AUC | 0,972 (`instrumental`) | 0,988 (tag "instrumental" do MSD) |
| Rap × "não é rap" rotulado (53 × 21), AUC | 0,99 (régua aprendida, #101) | 0,907 ("Rapping" do YAMNet, sozinho) |

Nada passa do ruído, fora a tag "instrumental" do MSD, com ganho pequeno. Por isso `[analise] extras` passou a trazer só o `maest`; os outros dois continuam disponíveis no config e, com os áudios guardados, podem ser calculados depois. O que já foi calculado fica no banco.

Do núcleo, também guardados e sem uso: `bpm_rhythm` e `beat_confidence` (o RhythmExtractor concorda com o TempoCNN em 72% das faixas, difere por uma oitava em 15% e diverge em 13%; a confiança mediana é 1,63 / 0,91 / 0,28 nesses grupos), `tom`, `escala`, `key_strength` e `effnet_emb_std`. Seguem no núcleo, porque tirá-los reabriria a fila inteira.

## Gênero-pai aprendido com os rótulos (#93)

**Data:** 2026-11-07 · **Decisão:** ADR-17 · **Script:** `contrib/avaliacao/genero_aprendido.py`.

**Como foi medido:**
- **Base:** cópia do banco real de 07/11, com 3.322 faixas analisadas, todas com o MAEST.
- **Rótulos:** o gabarito da #83 importado com `curator generos importar-gabarito`: 388 faixas pelo artista e 64 faixas de rap uma a uma.
- **Validação:** deixando o artista inteiro de fora, com 10 dobras por artista. O modelo de cada dobra nunca viu uma faixa do artista que prevê.
- **Modelo:** regressão logística L2 (λ = 20) sobre o EffNet + MAEST. A escala relativa ao núcleo aprendido é a mediana de p nas faixas com p ≥ 0,5.

AUC, e revocação / falsos positivos no corte 0,8:

| Gabarito | v2 (receitas) | Com o MAEST | Aprendida |
|---|---|---|---|
| Hip Hop por artista (50 × 361) | 0,999 · 0,43 / 0,00 | 1,000 · 0,29 / 0,00 | 0,996 · 0,86 / 0,01 |
| Rap faixa a faixa (43 × 21) | 0,946 · 0,91 / 0,14 | 0,994 · 0,91 / 0,00 | **1,000 · 1,00 / 0,05** |
| Rock por artista (140 × 258) | 0,996 · 0,84 / 0,01 | 0,996 · 0,79 / 0,00 | 0,991 · 0,94 / 0,01 |
| Rock japonês ≥ 0,8 (46 faixas) | 0,87 | 0,54 | **0,98** |
| Pop por artista (176 × 222) | 0,854 · 0,21 / 0,04 | 0,877 · 0,12 / 0,04 | **0,966 · 0,88 / 0,07** |
| Electronic por artista (32 × 366) | 0,869 · 0,59 / 0,11 | 0,938 · 0,78 / 0,10 | 0,966 · 0,53 / 0,01 |

- **Critérios da #93:** os três passam no gabarito. São rap faixa a faixa ≥ 0,99, Pop ≥ 0,95 e ≥ 80% do rock japonês ≥ 0,8.
- **Atributos a mais:** YAMNet e as tags do MSD ficaram dentro do ruído (Pop 0,975–0,979, Electronic 0,957–0,969).
- **Escala:**
  - pela mediana dos positivos do treino, o Hip Hop por artista cai para 0,57 de revocação;
  - por "p ≥ 0,5" (sem núcleo), sobe para 0,86, mas com o Pop em 0,99 / 0,15.
- **Média da aprendida com uma régua:** Pop cai para 0,36–0,46 de revocação no 0,8, e o rock japonês volta para 87–93%.

**Fora dos quatro gêneros rotulados (biblioteca inteira).** Sem portão, o modelo decide entre os pais que conhece até para o que não é nenhum deles. Das faixas que a v2 põe em primeiro lugar em pais sem rótulo, uma fração passa no 0,8 do Electronic e do Hip Hop:

| Primeiro lugar na v2 | Faixas | Electronic ≥ 0,8 | Hip Hop ≥ 0,8 |
|---|---|---|---|
| Classical | 588 | 84% | 30% |
| Stage & Screen | 246 | 63% | 60% |

O Lago dos Cisnes ficou com 1,24 no Hip Hop. Duas correções foram testadas:
- **Portão:** só julga a faixa que a v2 põe num pai com modelo. Zera esses casos sem mudar nenhuma linha da tabela acima.
- **Negativos de fundo:** o núcleo dos pais sem rótulo como "não". Derruba o Electronic para 0,908.

O ADR-17 fica com o portão.

**Por que ainda ao lado.** Com o portão, o que a aprendida muda na biblioteca, entre as faixas sem rótulo, numa amostra:
- **Hip Hop:** entram Flo Rida e funk brasileiro, e sai "Heat Waves". Também entram trilhas de HOYO-MiX.
- **Rock:** entram o rock de anime (ZAQ, 鈴木このみ), mas também o pop de idol e de personagem (Petit Rabbit's, 七森中☆ごらく部, ヒャダイン).
- **Pop:** saem YOASOBI, ClariS e GARNiDELiA.

O gabarito só tem rock japonês, então o modelo aprendeu "produção japonesa de anime = Rock". As primeiras perguntas do aprendizado ativo (`curator generos rotular Rock` / `Pop`) são exatamente essas faixas, com a v2 em 0,1 e a aprendida em 1,0 no Rock, ou o contrário no Pop.

**Custo:** montar os quatro modelos leva ~0,3 s. Eles são refeitos quando mudam os rótulos ou as análises.

## Guarda e decisão por gênero (#101)

**Data:** 2026-11-09 · **Decisão:** ADR-17 (itens 5 e 8) · **Script:** `contrib/avaliacao/genero_aprendido.py`.

**Como foi medido:**
- **Base:** cópia do banco real de 09/11, com 3.319 faixas analisadas na biblioteca, 2.751 delas sem rótulo.
- **Rótulos:**
  - o gabarito ampliado com o J-pop no Pop e o pop eletrônico no Pop e no Electronic;
  - as 64 faixas de rap uma a uma;
  - uns 40 rótulos dados pela conversa. Esses são os mais difíceis, porque o aprendizado ativo pergunta onde as réguas discordam.
- **Validação:** com o artista inteiro de fora, em 10 dobras. O modelo e a guarda de cada dobra só conhecem as faixas rotuladas dos outros artistas.
- **Guarda:** 5 vizinhas e corte no percentil 5 das rotuladas. O corte ficou em 0,371.
- **Segmentos:** "trilhas" é uma lista de 13 artistas de trilha de jogo, com 1.566 faixas. "Japonesas" são as faixas com kana ou kanji no título ou no artista, fora as trilhas.

**Quem o modelo julga.** Ele julga 39% das faixas sem rótulo: 10% das trilhas, 87% das japonesas e 72% das outras. Nas outras, vale a v2.

AUC, e revocação / falsos positivos no corte 0,8:

| Gênero | v2 | Aprendida (só o modelo) | Aprendida com a guarda |
|---|---|---|---|
| Hip Hop por artista (52 × 505) | 0,998 · 0,43 / 0,00 | 0,992 · 0,86 / 0,02 | 0,995 · 0,86 / 0,01 |
| Rap faixa a faixa (43 × 21) | 0,946 · 0,91 / 0,14 | 0,997 · 1,00 / 0,10 | **0,990 · 0,95 / 0,10** |
| Pop por artista (287 × 264) | 0,890 · 0,36 / 0,04 | 0,979 · 0,84 / 0,03 | **0,977 · 0,82 / 0,03** |
| Pop, rótulos da conversa (10 × 6) | 0,767 · 0,10 / 0,00 | 0,367 · 0,40 / 0,83 | 0,600 · 0,30 / 0,33 |
| Rock por artista (153 × 401) | 0,993 · 0,84 / 0,01 | 0,992 · 0,87 / 0,02 | 0,992 · 0,88 / 0,02 |
| Rock, rótulos da conversa (11 × 9) | 0,657 · 0,45 / 0,11 | 0,374 · 0,36 / 0,56 | 0,687 · 0,55 / 0,22 |
| Rock japonês ≥ 0,8 (46 faixas) | 0,87 | 0,80 | 0,80 |
| Electronic por artista (97 × 450) | 0,930 · 0,85 / 0,12 | 0,939 · 0,70 / 0,06 | 0,938 · 0,71 / 0,06 |

Faixas sem rótulo com nota ≥ 0,8, no total e entre as trilhas:

| Gênero | v2 | Aprendida (só o modelo) | Aprendida com a guarda |
|---|---|---|---|
| Hip Hop | 15 (0 trilhas) | 390 (288) | 59 (1) |
| Pop | 264 (1) | 560 (28) | 503 (7) |
| Rock | 239 (52) | 276 (65) | 260 (55) |
| Electronic | 599 (281) | 1.194 (974) | 496 (310) |

- **Critérios da #101:**
  - as trilhas no Hip Hop aprendido caem de 288 para 1, e 87% das japonesas continuam julgadas;
  - os critérios da #93 continuam valendo com a guarda: rap faixa a faixa com AUC de 0,990, Pop com 0,977 e 80% do rock japonês ≥ 0,8.
- **O que mudaria no 0,8**, entre as faixas sem rótulo:
  - **Hip Hop:** entram 47, quase todas lofi (Ambition, the bootleg boy). Isso segue a regra do usuário de que lofi conta como Hip Hop. Saem "Heat Waves", "Lalala" e um cover de piada.
  - **Pop:** entram 252, quase todas pop ocidental (Hayd, Mark Ronson, Lauv). Saem 13, entre elas músicas de personagem do K-On! e "Hikaru Nara".
  - **Rock:** entram 29 e saem 8.
  - **Electronic:** entram trilhas (HOYO-MiX, Lorien Testard) e saem 149, entre elas pop de idol e Katy Perry.
- **Decisão do usuário:** a régua aprendida entra nas receitas no Hip Hop e no Pop (motor v3). Rock e Electronic seguem na v2.
- **Custo:** a avaliação inteira, com os 4 modelos, as 10 dobras e a guarda, leva ~5 s.

## Playlists master (#105)

Pedido de 12/11: as curtidas em 6 a 8 playlists amplas. Cópia do banco de 12/11, 2.643 curtidas analisadas (fora as ignoradas). Scripts de exploração fora do repositório; o resultado final sai de `curator playlists sugerir --quantas 6`.

**Gerais da #80 e gênero-pai.** As gerais (HDBSCAN "leaf", mínimo 40) dão 14 playlists, três delas "Soundtrack / Ambient · calmo", e deixam 405 curtidas de fora. Com "eom" e mínimo 80, 120 ou 200: 2, 3 e 2 grupos. Pela régua de gênero das receitas, 765 curtidas não passam de 0,8 em nenhum pai e 365 passam em dois ou mais; as trilhas se dividem entre Classical, Electronic e Stage & Screen.

**N fixo.** Ward sobre o UMAP de 10-d (o mesmo das gerais), N = 6, 7 e 8:

| | estabilidade (ARI) | silhueta (cos, EffNet) |
|---|---|---|
| Ward no UMAP, outra semente (k = 6 / 7 / 8) | 0,94 / 0,94 / 0,86 | — |
| Ward no UMAP, tirando 1% das faixas (k = 6, 4 sorteios) | 0,76–0,96 | 0,195 |
| Ward direto no EffNet, tirando 1% (k = 6) | 0,64–0,78 | 0,173 |
| **Consenso de 5 sementes, tirando 1% (k = 6, 3 sorteios)** | **0,97–0,99** | 0,187 |

Com 7, sai um grupo de ~105 faixas de J-pop mais enérgico; com 8, também um de ~154 faixas orquestrais. O usuário escolheu 6.

**Sobreposição.** A regra das gerais (percentil 10 do próprio grupo) incha grupos amplos: metade das curtidas em dois ou mais, e o J-pop de 410 para 1.085. A da fronteira (segunda master só se cos ≥ cos da própria − δ e acima do τ dela), com um UMAP só: δ = 0 → 7%, 0,01 → 8%, 0,02 → 10%, 0,03 → 12%, 0,05 → 15%. Com a partição por consenso e δ = 0,02: 347 faixas (13%) em duas.

**As 6 (núcleo + fronteira).** Trilhas calmas e orquestrais (HOYO-MiX, Lorien Testard, Yu-Peng Chen; energia 0,11, 100 BPM) 708 + 45 · pop e rap ocidentais (Melanie Martinez, Alec Benjamin, Sasha Alex Sloan) 608 + 77 · J-pop e VTubers (Hoshimachi Suisei, YOASOBI, Reol, Ado) 407 + 62 · trilhas com mais movimento (HOYO-MiX, Masayoshi Soken, MONACA; 133 BPM) 369 + 70 · J-rock e anime (kessoku band, ClariS, TrySail) 273 + 54 · rock e metal (BABYMETAL, Sabaton, Linkin Park) 278 + 39. Os nomes automáticos erram (o pop ocidental sai como "K-pop", pelos estilos do Discogs), então os nomes são do usuário.

