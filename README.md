# MapForge

Motor de geração procedural de cidades 3D a partir de dados do OpenStreetMap.
Escolha uma região no mapa, o sistema baixa os dados, gera a cena e exporta para GLTF.

Opcionalmente combina duas fontes: o **mapa vetorial** dá a estrutura (onde estão
ruas, casas, quadras, água) e a **imagem de satélite** informa as cores reais
daquele lugar. A foto não vira textura — as superfícies continuam chapadas, só que
tingidas pelo que existe de verdade ali. Sem IA. Tudo determinístico: mesma seed,
mesma cidade.

```powershell
py -3.11 app.py                                    # interface gráfica
py -3.11 run.py generate --place "Ouro Preto, MG" --radius 450 --satellite --render
```

## Estado atual

| Fase | Escopo | Situação |
|---|---|---|
| 1 | Interface, download OSM, parser, cache, visualizador 2D | pronto |
| 2 | Terreno, ruas, calçadas, prédios | pronto |
| 3 | Vegetação, água, exportação GLTF/OBJ/PLY | pronto |
| 4 | Estilos gráficos | paletas prontas; biblioteca de assets externos pendente |
| 5 | Editor | pendente |
| 6 | Otimizações (chunks, LOD, instancing, culling) | LOD por nível de detalhe; chunks e culling pendentes |
| 7 | IA (opcional) | fora do MVP, por escolha |

Fora do planejamento original, já funcionando: viewport 3D navegável com voo livre,
imagem de satélite como fonte de cor, terreno com relevo real, sistema de pontes,
malha de física para engine (`--colliders`) e um renderizador offscreen em software
para gerar imagens sem abrir uma engine.

## Instalação

Requer Python 3.11 (o 3.13 registrado nesta máquina está com o executável ausente).

```powershell
py -3.11 -m pip install -r requirements.txt
```

## Interface gráfica

```powershell
py -3.11 app.py
```

Três abas à esquerda, painel de controles à direita.

**Aba Mapa** — mapa deslizante embutido (OpenStreetMap ou satélite, botão alterna),
busca por nome ("Ouro Preto, MG" + Enter) e duas formas de marcar a região:

| Ferramenta | Como usar |
|---|---|
| *Retângulo* | arraste sobre o mapa; Shift arrasta sem trocar de modo |
| *Desenhar área* | clique em cada canto; a área aparece em km² enquanto você desenha |

No modo de desenho, **duplo clique** ou **Enter** fecha o contorno, clicar de volta no
primeiro ponto também fecha, **Backspace** desfaz o último ponto e **Esc** cancela.

O contorno desenhado recorta a cena inteira — ruas, calçadas, água, vegetação e o
próprio terreno, que ganha parede lateral acompanhando o desenho. Os downloads
continuam usando a caixa envolvente, porque tile e consulta Overpass são sempre
retangulares.

**Aba 3D** — viewport navegável com a cena gerada, a mesma que vai para o GLB:

| Ação | Controle |
|---|---|
| Orbitar | arrastar com o botão esquerdo |
| Deslocar | arrastar com o botão do meio ou direito |
| Aproximar | roda do mouse |
| **Voar** | setas ou `WASD` |
| **Subir / descer** | `Q` e `E`, ou `PageUp` / `PageDown` |
| **Acelerar** | segurar `Shift` |
| Enquadrar | tecla `F` ou botão *Enquadrar* |

No voo livre a câmera continua orbital: o que o teclado move é o **alvo**, e a câmera
vai junto. Assim o voo não briga com o mouse — gira-se a vista arrastando e anda-se
para onde ela aponta com o teclado. O avanço é horizontal de propósito: usar o vetor de
visão cheio faria a câmera mergulhar no chão ao andar para a frente com a vista
inclinada, que é a posição normal aqui.

O passo acompanha a distância da câmera ao alvo. De longe se cobre a cidade, de perto
se anda pela rua; com passo fixo, o que é útil a 50 m atravessa o mapa inteiro a 5 km.
Quem move a câmera é um relógio de 16 ms, não a repetição do teclado — senão a
velocidade dependeria da configuração de repetição de cada máquina.

**Aba Imagem** — planta 2D e renders salvos.

**Painel** — estilo, detalhe, seed, satélite, fontes de edifícios, relevo, física,
camadas, altura dos prédios e densidade de vegetação. *Prévia 2D* roda só o parser
(rápido); *Gerar 3D* roda o
pipeline completo e joga o resultado no viewport. *Salvar render PNG* usa o ângulo
atual da câmera do viewport.

A geração roda numa thread separada: a barra de progresso anda e a janela continua
respondendo. O render offscreen saiu do caminho crítico — quem mostra o resultado é
o viewport, então gerar uma cena leva os mesmos ~1,5 s da CLI em vez de esperar os
20 s do rasterizador.

O mapa é uma página HTML própria, sem biblioteca externa — só os tiles vêm da rede.
O viewport é ModernGL dentro de um `QOpenGLWidget`, com a mesma iluminação do
renderizador offscreen para que a prévia e o arquivo exportado combinem.

## Linha de comando

```powershell
# Por nome do lugar (Nominatim)
py -3.11 run.py generate --place "Paraty, RJ" --radius 700 --style cartoon

# Por bbox: sul,oeste,norte,leste
py -3.11 run.py generate --bbox -25.44,-49.28,-25.42,-49.26 --detail high

# Por centro + raio (use --center=... por causa do sinal negativo)
py -3.11 run.py generate --center="-20.3857,-43.5036" --radius 300
```

Saídas:

```powershell
--out cidade.glb          # formato pela extensão: .glb .gltf .obj .ply .stl
--preview                 # planta 2D em PNG (rápido, bom para conferir o parser)
--render                  # render 3D offscreen em PNG
--perspective --elevation 18 --azimuth 200
--save-project centro     # guarda os parâmetros no banco local
```

Geração:

```powershell
--style lowpoly|cartoon|futurista
--detail low|medium|high
--region tropical|subtropical|temperada|boreal   # padrão: pela latitude
--seed 42
--height-scale 1.5        # multiplica a altura dos prédios
--tree-density 2.0
--no-buildings --no-roads --no-vegetation --no-water --no-sidewalks --no-terrain
--no-windows              # maior redução de tamanho de arquivo
```

Fontes de edifícios:

```powershell
--overture                # completa o OSM com o Overture Maps (recomendado)
--footprints              # reserva: contornos abertos da Microsoft
--detect-buildings        # detecta telhados na imagem (exige --satellite)
--detect-vegetation       # detecta mata e gramado na imagem (exige --satellite)
--canopy-shell            # miolo da mata vira uma superficie unica (desligado por padrao)
--shadow-heights          # estima altura pela sombra na imagem (exige --satellite)
--urban-scale povoado|pequena|media|grande   # forca o porte do assentamento
```

Satélite e relevo:

```powershell
--satellite               # cores reais: telhados, terreno, parques, bosques
--provider esri|mapbox|bing|custom
--roof-blend 0.75         # peso da foto nos telhados (0 = só estilo, 1 = só foto)
--area-blend 0.55         # idem para terreno e áreas verdes
--satellite-zoom 18       # força o nível (padrão: o maior com cobertura real)
--ground-texture          # textura propria: repinta cada cobertura com a cor dela
--ground-texture-mode foto  # em vez de pintar, cola a foto crua (modo antigo)
--texture-variation 0.55  # 0 = chapado, 1 = quase a foto
--redraw-ground           # redesenha o chao como desenho chapado (exige --ground-texture)

--relief                  # terreno com relevo real
--exaggeration 1.5        # multiplica o desnível (1.0 = escala real)
--relief-zoom 15
```

Física:

```powershell
--colliders               # malha de colisão + JSON com as formas analíticas
--collider-naming godot|unreal|plain
```

`--relief` e não `--elevation` porque `--elevation` já é o ângulo da câmera do render.

Outros comandos:

```powershell
py -3.11 run.py gui                                          # mesma coisa que app.py
py -3.11 run.py satellite --place "Tiradentes, MG" --radius 400   # só baixa a imagem
py -3.11 run.py providers                                    # fontes de imagem
py -3.11 run.py preview --place "Tiradentes, MG" --radius 500
py -3.11 run.py styles
py -3.11 run.py cache list | clear
py -3.11 run.py projects
```

### Como biblioteca

```python
from mapforge.core.geo import BBox
from mapforge.generation import GenerationSettings
from mapforge.pipeline import generate

bbox = BBox.from_center(-25.4284, -49.2733, radius_m=600)
result = generate(
    bbox,
    GenerationSettings(seed=7, style="cartoon", satellite=True),
    output="curitiba.glb",
)
print(result.scene.triangle_count, result.map_data.summary())
```

## Cidade inteira: geração em blocos

O limite de 25 km² por cena não é capricho — uma consulta Overpass daquele tamanho é
recusada, a imagem de satélite estoura o orçamento de tiles e a malha não cabe na
memória. Uma cidade como Parnaíba (~36 km²) passa longe disso.

```powershell
py -3.11 run.py region --place "Parnaíba, PI" --radius 3000 --tile-km 1.5 --relief
py -3.11 run.py region --place "Parnaíba, PI" --radius 3000 --dry-run   # só o plano
```

A região é cortada em blocos, gerados um de cada vez, e ao lado sai um **manifesto**
com a lista de blocos e o deslocamento de cada um em metros. É a "informação de
montagem": o arquivo pesado vira N arquivos que a engine posiciona por translação, e
o manifesto guarda também a seed e as configurações — então **a região inteira é
reconstruível a partir de parâmetros**, sem guardar malha. Os blocos podem ser
apagados e regerados idênticos.

Cada bloco tem seu próprio centro local, então as coordenadas ficam pequenas e a
precisão de float não sofre. O manifesto anota a convenção de eixo (`x, z = leste,
−norte`), que é o que falta para a montagem sair torta.

**Um download por super-bloco, não por bloco.** Os downloads dominam o relógio: seis
blocos de 1,5 km levavam **583 s**, quase tudo esperando o Overpass responder seis
vezes a mesma vizinhança. Agrupando os blocos vizinhos num único download, dentro do
limite de área de uma consulta, e recortando localmente: **14,3 s**.

`--resume` pula blocos já gravados, para retomar uma região grande sem refazer tudo.

## Cidades pequenas: de onde vêm os edifícios

O OpenStreetMap só tem prédio onde alguém desenhou. Em cidade pequena isso costuma
dar dois ou três prédios num quarteirão inteiro, e a cena sai vazia. Medindo em
quadrados de 800 m:

Quatro fontes, aplicadas em cascata — cada uma só preenche o que a anterior deixou:

| Região | OSM | `--overture` | `--footprints` | `--detect-buildings` |
|---|---|---|---|---|
| Santa Bárbara, MG | **5** | — | **1105** | — |
| Araioses, MA | **12** | **1539** | 12 (sem cobertura) | 1103 |
| Tiradentes, MG | 407 | — | 502 | +98 |
| Centro de SP | 1816 | — | 1816 | — |

#### `--overture` — a melhor fonte disponível

O **Overture Maps Foundation** publica a conflação de OpenStreetMap, Microsoft,
Esri Community Maps e **Google Open Buildings**. Esse último é o mesmo conjunto que
o Google Maps serve no celular — é por isso que ele acerta casa por casa em cidade
pequena brasileira, e por isso que sites como o `maps3d.io` produzem contornos
idênticos aos do Maps: usam o mesmo dado.

Em Araioses-MA, numa janela de 640 m: **332 contornos do Overture** (330 do Google,
2 do OSM) contra **8 do OSM**. Na cidade inteira, 5.382 contra 15.

Não é detecção em tempo de execução: a rede neural rodou offline, o resultado foi
validado e publicado como vetor. É a mesma lógica do `--footprints`, com cobertura
melhor e atributos a mais — `roof_shape`, `roof_color`, `roof_material`, `class` e
`num_floors` entram nas tags do prédio quando existem, e valem mais que qualquer
palpite procedural.

Acesso: GeoParquet no S3 aberto, com estatísticas de bbox por grupo de linhas, o que
permite consultar só a janela pedida sem baixar o planeta. Exige `duckdb`. A primeira
consulta de cada região leva de 20 s a 2 min; depois fica em cache por 30 dias e o
custo é zero. Na geração em blocos a consulta é feita uma vez para a região inteira,
não por bloco. O release mais recente é descoberto sozinho no S3 e cacheado por 7 dias.

#### `--footprints` — reserva

O **GlobalMLBuildingFootprints** da Microsoft: 1,4 bilhão de contornos extraídos de
imagem de satélite por rede neural, sob ODbL. Continua útil como reserva para quando
o Overture estiver fora do ar ou o `duckdb` não estiver instalado — e em algumas
regiões ele cobre onde o Overture ainda não chegou.

Em ambas, o que já existe sempre vence: onde o prédio já foi desenhado, o contorno
novo é descartado. O critério é sobreposição de 30% relativa ao menor dos dois, que
pega tanto "mesmo prédio com contorno diferente" quanto "quarteirão mapeado como um
polígono só". Rodar as duas fontes juntas não duplica nada.

Particionamento por quadkey de zoom 9 (~78 km de lado): um arquivo cobre uma região
inteira. O primeiro download é grande (10–30 MB) e lento, depois fica em cache e o
custo é zero. O índice (7 MB) também é cacheado, por 60 dias.

### Quando nem isso cobre: `--detect-buildings`

O conjunto da Microsoft tem buracos. O quadkey `211000300`, que cobre Araioses-MA e
todo o delta do Parnaíba, simplesmente não existe no índice — embora São Luís,
Teresina e Fortaleza existam. Lá, `--footprints` não acrescenta nada. Foi para esse
caso que a detecção por imagem foi escrita, antes de o Overture entrar.

**Meça antes de usar.** Com o Overture como verdade de campo em Araioses (332
contornos reais numa janela de 640 m), a detecção própria produz 637 retângulos com:

| | |
|---|---|
| precisão | 49,6% |
| cobertura | 44,0% |
| IoU de área | 25,2% |
| falso positivo | 61,4% do que desenha — 3,72 ha de areia e solo exposto |

Dos 10,47 ha classificados como telhado, só 39,2% acabam cobertos por algum
retângulo. A perda se divide entre o teste de sombra rejeitando telhado real (62
manchas, 1,58 ha) e a aproximação retângulo-mais-grade não acompanhando o contorno.

Ou seja: use `--overture` quando houver cobertura. A detecção é para o resto.

`--detect-buildings` procura telhados na própria imagem. Sozinha,
classificação de pixel por cor confunde telhado com solo exposto. O que torna o
resultado utilizável é usar **o mapa 2D do OSM como filtro**:

- onde há rua, água ou mata, não há telhado;
- casa fica perto de rua — candidato a mais de 70 m de qualquer via é descartado;
- o que já existe como edifício (OSM ou Microsoft) sai da disputa antes.

Duas famílias de pixel são aceitas, porque no Brasil o telhado ou é cerâmica
(R > G > B, saturado) ou é laje/fibrocimento (claro e dessaturado). Vegetação é
recortada pelo excesso de verde, que é o discriminador mais confiável em banda
visível. Depois vêm abertura e fechamento morfológicos, componentes conexos e um
retângulo orientado ajustado por PCA sobre os pixels de cada mancha.

O contorno devolvido é esse retângulo, não o telhado exato — para uma cena low-poly
isso é melhor do que um blob irregular ruidoso, e é o que mais se parece com uma casa
vista de cima.

### Vegetação: `--detect-vegetation`

Mesmo problema dos edifícios, resposta oposta. O OSM só tem mata onde alguém
desenhou o polígono: **Araioses tem zero matas mapeadas**, e uma mancha de floresta
de 13 ha saía completamente pelada no modelo.

Aqui a detecção por imagem *é* a resposta certa, e não existe um "Overture de
árvores". Vegetação é o caso fácil da visão computacional em banda visível: o
excesso de verde (`2G − R − B`) resiste à variação de iluminação e não confunde com
solo exposto, asfalto ou telha — ao contrário do telhado, onde areia e laje disputam
a mesma faixa de cor.

O que separa copa de gramado não é a cor, é a **textura**: copa é um aglomerado de
formas que sombreiam umas às outras, então o desvio padrão local é alto; pasto e
campo são lisos. O brilho entra como segundo sinal, porque copa fechada é escura.

Em Araioses, janela de 1,4 km: **83 ha de copa em 65 manchas e 26 ha de vegetação
rasteira**, contra 0 matas e 10 parques no OSM. As árvores plantadas foram de 22
para 6.479.

Três detalhes que decidiram o resultado, cada um valendo milhares de árvores:

- **A união das células precisa fechar.** A máscara é vetorizada agregando em células
  de 5 m e unindo as cheias. Escrever `x1 = x0 + lado` faz o lado direito de uma
  célula diferir do lado esquerdo da vizinha no último bit — a união não funde, e a
  mata sai como um cardume de tiras finas. As duas bordas têm de vir da mesma
  fórmula. Antes: 933 manchas listradas; depois: 72 manchas sólidas.
- **O piso de área é o tamanho de uma copa, não o do espaçamento.** Descartar mancha
  menor que o espaçamento apagava justamente a árvore de quintal — que é a maioria
  da vegetação de uma cidade pequena.
- **O jitter tem de encolher em mancha pequena.** Com deslocamento fixo de 70% do
  passo, um quintal de 15 m jogava quase todo ponto para fora do polígono.

A mancha vinda da imagem já nasce recortada contra telhado, via e água — recortá-la
de novo contra os edifícios, agora com a folga de 1,5 m, corroía a borda de cada
quintal.

**Orçamento em vez de corte.** Com a vegetação vindo da imagem, uma região de mata
pode pedir dezenas de milhares de árvores. Truncar a lista no teto deixaria metade da
mata pelada, então em vez de cortar árvores no fim, o espaçamento cresce no começo
até a conta fechar — e a copa cresce junto, para o dossel continuar fechando. Uma
mata rala de árvores grandes lê como mata; meia mata cheia e meia mata vazia não lê
como nada.

#### O que custa caro é modelar árvore que ninguém vê

Uma árvore detalhada custa 96 triângulos. Com a detecção ligada, Araioses pediu 6.479
árvores e **a vegetação virou metade da malha inteira** — desperdício, porque o miolo
de uma mata fechada nunca é visto como árvore individual. De qualquer ângulo se vê
duas coisas: o topo do dossel e a silhueta da borda.

Duas mudanças, medidas em Araioses (1,4 km, com relevo e satélite):

| | árvores | triângulos | arquivo |
|---|---|---|---|
| árvore individual em toda parte | 6.479 | 987k | 24,6 MB |
| + modelo barato dentro da mata | 6.597 | 638k | 18,2 MB |
| + dossel no miolo | **3.794** | **577k** | **18,4 MB** |

**Detalhe por contexto.** Árvore de rua e de parque é vista de perto e mantém a copa
cheia; árvore dentro de mata aparece como uma mancha no meio de outras mil e resolve
com a copa de 20 faces (33 triângulos com o tronco). Só isso corta 35% da malha, sem
diferença perceptível de cima. É o mesmo princípio do dossel, levado ao indivíduo.

**Dossel.** Mancha de mata acima de 2.500 m² é separada em miolo e faixa de borda de
13 m. A borda continua recebendo árvore, para a silhueta e a transição para o chão
lerem certo. O miolo vira **uma superfície ondulada única** na altura das copas, a 2
triângulos por célula de 8 m. A ondulação é a soma de duas senoides com fase sorteada
— não é ruído de verdade, mas duas frequências já quebram qualquer alinhamento
visível de cima.

Três correções depois da primeira versão, que saiu com cara de mesa verde:

- **A borda sobe do chão** ao longo de 16 m, com curva suave (`3t² − 2t³`), em vez de
  ter topo reto e parede vertical. A saia lateral deixou de ser necessária.
- **O contorno é arredondado** antes de virar geometria. União de células quadradas
  deixa uma borda em degrau que aparece de longe; fechar e abrir com junta redonda na
  escala da célula tira a escada, e o `simplify` depois só enxuga os vértices.
- **A cor vem da foto**, quantizada em 4 tons por k-means sobre a cor real amostrada
  em cada célula. Mata de várzea, capoeira e mata fechada têm verdes bem diferentes, e
  uma cor só para tudo era o que mais denunciava o dossel como superfície pintada.

O dossel troca 60 mil triângulos por praticamente o mesmo tamanho de arquivo (ele não
é instanciado, então cada célula custa vértice próprio). O ganho real dele é outro:
**fecha a copa**, que com árvore solta a 12,5 m ficava esburacada, e sustenta a
escala — numa região de vários km² de mata, o orçamento de árvores rarefaria a
floresta até sumir, enquanto o dossel custa o mesmo por hectare sempre.

**O dossel está desligado por padrão**, e a economia média de 10% da cena não pagou os
defeitos: a cor por célula de 8 m produzia um xadrez verde que não existe em mata
nenhuma, o afunilamento da borda dobrava a superfície e abria buracos escuros, e o
miolo ficava vazio justamente onde há árvore de verdade. Árvore individual com o
protótipo barato custa 10% a mais e não tem nenhum desses problemas. Ligue com
`--canopy-shell` para experimentar.

### Coerência de porte: o perfil urbano

Araioses não tem prédio de dez andares. A regra que escolhia pavimentos só olhava a
área do contorno — e área grande virava prédio alto —, então o hospital de 4.339 m²
nascia com **doze pavimentos e 42,6 m de altura** no meio de um povoado do delta do
Parnaíba. Numa metrópole a mesma regra está certa.

Faltava o contexto: *que tipo de lugar é este?* Não há essa informação pronta e
confiável (o `place=*` do OSM falta justamente onde o mapeamento é ralo), mas ela
está no próprio conjunto de contornos. Medido em janelas de 1,4 km com edifícios do
Overture:

| região | n | ocup.% | mediana | p99 |
|---|---|---|---|---|
| Araioses-MA (povoado) | 1543 | 9,7 | 96 | 531 |
| Tiradentes-MG (histórica) | 1533 | 9,8 | 84 | 719 |
| Parnaíba-PI (média) | 2862 | 27,6 | 137 | 1085 |
| São Paulo-SP (centro) | 3619 | 45,6 | 162 | 1508 |
| Belo Horizonte-MG (centro) | 1632 | 28,8 | 187 | 2761 |

O corte é o **p99 da área do contorno**: existe prédio grande aqui? É monotônico e
resiste ao galpão isolado — Araioses tem um contorno de 4.339 m² e ainda assim p99 de
531.

A taxa de ocupação parece um sinal melhor — 9,7 / 9,8 / 27,6 / 45,6 — até Belo
Horizonte, onde a janela pega o Parque Municipal e cai para 28,8, no mesmo patamar de
Parnaíba. Como corte principal ela rebaixaria um centro de metrópole a cidade
pequena; como piso para núcleo saturado (≥ 42%), funciona.

| porte | teto de pavimentos |
|---|---|
| povoado / vilarejo | 2 |
| cidade pequena | 4 |
| cidade média | 8 |
| cidade grande | 14 |

O teto vale só para prédio **sem** altura em tag; `building:levels` do OSM continua
mandando. Force com `--urban-scale povoado|pequena|media|grande` quando a medição
errar.

### Ver o que o sistema enxerga: `segment`

```powershell
py -3.11 run.py segment --place "Araioses, MA" --radius 320 --with-buildings
```

Converte a foto num desenho 2D classificado por cobertura — telha cerâmica, laje,
telhado escuro, vegetação densa, vegetação rasteira, solo exposto, água, sombra e via
— com paleta fixa e legenda, lado a lado com a original. `--with-buildings` desenha
por cima os contornos que o detector extraiu.

Existe para tornar a percepção **observável**. Antes disso o detector era caixa preta:
entrava foto, saía polígono, e quando errava não dava para saber se o erro estava na
cor, na forma ou no filtro.

A classificação trabalha em **HSV, não em RGB cru**: o matiz separa telha de vegetação
de forma estável mesmo com a iluminação mudando. O caso difícil no Brasil é telha
cerâmica contra solo laterítico — a terra é vermelha e ocupa a mesma família de matiz.
O que os separa é a **textura**: telhado é superfície fabricada, lisa; terra batida é
ruidosa. O desvio padrão local resolve o que a cor sozinha não resolve.

**Água e vias vêm do mapa vetorial, não da cor.** Rio de planície carrega sedimento e
aparece marrom-esverdeado — a regra de cor o classificava como vegetação densa, o que
o desenho deixou evidente na primeira rodada.

### Qualidade medida, não estimada

Os limiares saem de uma varredura contra os **407 edifícios que o OSM tem mapeados em
Tiradentes**, que servem de verdade de campo: o que a detecção acha sobre um edifício
conhecido é acerto, o que acha sobre nada é falso positivo.

| | Precisão | Revocação | F1 |
|---|---|---|---|
| Antes | 58,2% | 45,2% | 50,9% |
| **Agora** | **59,6%** | **65,1%** | **62,3%** |

Duas descobertas da varredura:

- **O filtro de forma era o vilão da quantidade.** Exigir 55% de preenchimento do
  retângulo orientado matava casa de verdade junto com o solo exposto. A 42%, a
  revocação salta de 45% para 65%.
- **A prova de volume compra precisão.** Um prédio projeta sombra do lado oposto ao
  sol e destoa da vizinhança; solo exposto, areia e quadra de terra batida falham nas
  duas coisas. A direção da sombra é medida nas próprias manchas candidatas — em
  cidade sem edifício mapeado não há de onde tirá-la de outro jeito.

Uma tentação que os números desmentiram: reduzir a abertura morfológica *parecia* bom
para achar casa pequena, e derruba a precisão. Ficou em 0,9 m.

Duas armadilhas que custaram uma rodada em Araioses:

1. **Casas geminadas viram uma mancha só.** Uma mancha de 1100 m² classificada por
   área virava um prédio de dez andares numa cidade térrea. Manchas grandes passaram a
   ser fatiadas numa grade de unidades do tamanho de uma casa — mas só quando o
   preenchimento do retângulo é baixo; telhado limpo e cheio é galpão de verdade e
   fica inteiro.

   **Essa grade é hoje o maior defeito visual do modo detecção**, e o `segment` deixou
   isso explícito: em Araioses, 214 manchas viram 637 retângulos, e o resultado 3D é um
   xadrez de casas idênticas que não corresponde ao loteamento real. A correção certa é
   cortar pelas emendas escuras entre telhados, que aparecem na foto, em vez de por uma
   grade regular — ainda não feita.
2. **A área de um contorno detectado não diz nada sobre altura.** Prédios vindos da
   detecção são tratados como térreos ou sobrados, independentemente da área; só tag
   do OSM ou sombra confiável os levantam.

Exige imagem com pelo menos ~1,2 m/pixel. Como o zoom cai conforme a região cresce,
áreas muito grandes desligam a detecção automaticamente — para cidade pequena, use
raios de até ~600 m.

### Altura

O conjunto da Microsoft não traz altura no Brasil (vem `-1`). A altura sai, em ordem
de confiança:

1. **Tag do OSM** (`height`, `building:levels`) — exata onde existe. Em São Paulo,
   96% dos prédios têm; em Ouro Preto, 5%.
2. **Sombra na imagem** (`--shadow-heights`) — `h = L · tan(elevação solar)`.
3. **Procedural** — classificação por área, como antes.

Como não sabemos a data nem a hora da captura do tile, a elevação solar não é
calculável; o estimador se **auto-calibra**: a direção da sombra sai da própria
imagem (a direção em que a vizinhança dos prédios escurece, varrida em 36 ângulos)
e a escala sai dos prédios que já têm altura no OSM. Sem prédios de referência
suficientes o resultado é **descartado** com um aviso, e a altura volta a ser
procedural — um chute sobre a elevação solar produz altura pior do que a regra por
área.

## Imagem de satélite

O comando `satellite` existe para conferir a qualidade **na sua região** antes de
gastar tempo gerando:

```powershell
py -3.11 run.py satellite --center="-20.3857,-43.5036" --radius 200
```

**O zoom não é o da visualização no navegador.** O downloader escolhe o maior nível
que cabe no orçamento de pixels e depois *sonda a cobertura real*: o Esri responde
`200 OK` com um retângulo cinza escrito "Map data not yet available" onde não tem
imagem, em vez de erro. Sem essa detecção o mapa inteiro sairia texturizado de
cinza. Quando o nível pedido não existe, cai um degrau até achar imagem de verdade.
Em Ouro Preto, por exemplo, z19 não existe e o resultado real é z18 (0,56 m/pixel).

### A foto informa a cor, não vira textura

A geometria continua vindo do OSM e o acabamento continua low-poly. O que a imagem
faz é responder "de que cor é *este* lugar":

- **Telhados** — a mediana dos pixels dentro de cada contorno, recuado para não pegar
  sombra de fachada. Mediana e não média: resiste a caixa d'água e antena.
- **Terreno** — a mediana da imagem inteira vira a cor de fundo, no lugar de um tom
  arbitrário do estilo.
- **Parques, bosques e estacionamentos** — cor amostrada por área.

As cores passam por realce de croma e clamp de luminância (senão a cidade sai
cinza-lama, porque imagem de satélite é naturalmente dessaturada e cheia de sombra)
e depois são quantizadas por k-means numa paleta pequena — um material por edifício
viraria uma primitiva glTF por edifício. Cada cor da paleta é misturada com a do
estilo segundo `--roof-blend` / `--area-blend`: é aí que a foto e o desenho se
encontram.

### `--ground-texture`: uma textura própria, pintada da foto

Cor chapada por polígono resolve edifício, mas não resolve chão: cada mancha vira um
bloco de cor única com degrau duro na divisa, e o terreno fica com cara de mapa de
bloco. Colar a foto crua também não resolve — ela traz carro, sombra de poste, ruído
de compressão e a própria rua, que o modelo já desenha em 3D.

O caminho do meio: **classificar cada pixel e repintá-lo com a cor daquela cobertura
naquele lugar**. Mata recebe o verde que a mata tem ali, gramado o verde do gramado
dali, solo exposto o ocre do solo dali — cor medida, não cor de tabela. Não há padrão
nem paleta fixa: a textura sai diferente em cada região porque as cores vêm da região.
Em Araioses, medido:

```
cobertura: mata 44%, telha 29%, grama 12%, via 7%, agua 6%, solo 3%
grama  rgb(125, 122,  91)     <- capim de fim de estação, não o verde de tabela
mata   rgb( 68,  80,  50)
solo   rgb(156, 124,  95)
```

Três decisões que fazem a diferença:

- **Mediana, não média.** Uma sombra ou um telhado invadindo a máscara puxa a média e
  não mexe na mediana.
- **O que a malha já desenha em 3D não entra na textura.** Telhado, via e água herdam
  o rótulo da cobertura válida mais próxima (uma `distance_transform_edt` resolve
  todos de uma vez). O chão continua embaixo — só estava escondido.
- **Parte da variação interna volta.** Cada pixel recebe o desvio de luminância dele
  em relação à mediana da sua classe (`--texture-variation`, padrão 0,55). Em 0 a
  classe vira tinta chapada; em 1 volta a ser a foto. O meio é manchado o bastante
  para ter relevo e liso o bastante para continuar estilizado.

Com a textura ligada, os retalhos chapados de parque e bosque deixam de ser
desenhados — eles já estão na textura, e é justamente a sobreposição dos dois que
produzia o degrau. `--ground-texture-mode foto` volta ao comportamento antigo de colar
a imagem crua.

#### O "8 ou 80": separação de frequências

Repintar por classe resolve a cor, mas o chão continuava binário — ou tinta chapada,
sem vida, ou a foto de satélite, que a 1,19 m/px está borrada e cheia de ruído de
compressão. Faltava o meio.

O meio tem nome: **uma foto de satélite só tem informação de frequência baixa.** A
cor macro — onde muda de mata para pasto — ela tem, e é verdadeira. A frequência
alta — o grão da terra, o agregado do asfalto, o recorte de copa — ela não tem, e
nunca vai ter, porque o pixel dela é maior que o detalhe.

Então cada banda vem de onde faz sentido: **a cor macro do dado, o grão do
procedimento**, com escala e aspereza próprias de cada cobertura.

| cobertura | escala do grão | amplitude |
|---|---|---|
| mata | 6,5 m | 0,21 (+ desvio de cor) |
| solo | 2,6 m | 0,15 |
| grama | 1,8 m | 0,10 |
| via | 0,6 m | 0,06 |
| água | 9,0 m | 0,04 |

A textura sai a **0,35 m/px** — três vezes a resolução da foto — sem inventar foto
nenhuma: o que foi acrescentado é material, não imagem. Os rótulos sobem por vizinho
mais próximo, para a divisa entre coberturas continuar nítida; ampliá-los por
interpolação criaria uma faixa de cobertura inexistente na borda de cada mancha.

A calibração foi feita olhando a textura ao lado da foto: o primeiro ajuste tinha o
dobro dessas amplitudes e o chão parecia **sujo**, não texturado. O multiplicador do
grão existe como parâmetro da biblioteca (`texture_grain`, 0 = cor chapada, 2 =
exagerado); na linha de comando o controle exposto é `--texture-variation`.

#### `--redraw-ground`: o chão como desenho, não como textura

O modo acima ainda tenta parecer material. `--redraw-ground` faz outra coisa: repinta
o chão como um **desenho chapado** — poucas cores por cobertura, manchas grandes,
contorno curvo e limpo, sem grão nem ruído de alta frequência. A alta resolução serve
para a *borda* ficar lisa, não para caber detalhe, pelo mesmo motivo que um desenho
vetorial é nítido em qualquer tamanho. As cores continuam saindo dos percentis da
própria cobertura na foto, então areia rosada sai rosada e mata de várzea sai escura.

Exige `--ground-texture`.

### Água: o rio nunca é "azul"

O Negro é preto de tanino, o Solimões é barro, o Parnaíba é esverdeado de sedimento,
uma lagoa costeira é quase turquesa. Cada corpo d'água recebe **a cor medida nele**,
misturada com o estilo — em Araioses o rio saiu `rgb(84, 122, 125)`, e não o `5b9dc9`
da paleta.

A amostragem é feita numa faixa estreita no eixo do rio: a borda tem margem, banco de
areia e sombra de mata, que puxariam a cor para longe da água.

Três tipos são tratados à parte:

- **Intermitente** (`intermittent=yes`). No semiárido é a maioria. Pintar de azul um
  rio que só tem água na cheia é erro grosseiro de leitura da paisagem — o que se vê
  de cima é areia. Vira leito seco no nível do chão.
- **Canal e vala** não meandram: são obra. O suavizador de eixo deixou de ser aplicado
  neles, porque produzia uma curva que não existe no terreno.
- **Córrego** é raso e **rio** é fundo, com fatores de profundidade próprios.

### O leito: três defeitos medidos

Um rio errado é o defeito mais visível que este gerador produz, porque ele atravessa
o mapa inteiro. Os três que existiam foram medidos, não estimados:

**1. Ilhas de terra dentro do rio.** A escavação usava uma rampa que dava peso 0,5 na
borda *interna* do polígono e crescia até 1 no miolo, "para suavizar dos dois lados".
O resultado é que um vértice logo dentro da margem descia só metade do caminho — e num
terreno 2 m acima do leito ele parava **acima da lâmina**. Medido em Araioses, num
único rio: **245 vértices de terra acima da água**, o mais alto por 1,82 m. Quem tem
de ser gradual é o lado de fora, que é o talude; o leito é leito. Depois da correção:
**0**.

**2. O nível saía da caixa envolvente.** O nível da água vinha do percentil baixo das
alturas amostradas na *bounding box* do corpo. Um rio diagonal preenche uma fração
pequena da própria caixa — medido em Araioses, a caixa tem **cinco vezes a área do
rio** —, então a amostra era sobretudo a encosta em volta e a água nascia alta demais.
Amostrando por dentro, o mesmo rio caiu de 3,64 m para 2,33 m.

**3. Um nível só para o rio inteiro.** Funciona em lago e falha em rio: numa descida de
900 m a lâmina plana ou flutua acima do terreno na cabeceira ou fica enterrada na foz.
O corpo passou a ser fatiado ao longo do próprio eixo em trechos de ~140 m, cada um
com o seu nível, suavizados e **obrigados a não subir para jusante** — sem isso um
pico de ruído do DEM levanta uma represa que não existe. Lago continua inteiro: água
parada tem um nível só, e fatiá-lo inventaria desnível.

O nível por trecho trouxe um defeito próprio, que também precisou de medição: escavar
cada trecho até o **seu** nível produz um açude em cada emenda. O leito do trecho de
cima fica uma profundidade abaixo do nível dele, o que ainda pode estar acima da
lâmina do trecho de baixo — num vale com 1,3 m de queda por trecho e 0,5 m de lâmina
sobram 0,8 m de leito seco atravessado no meio do rio. Medido: **106 de 328 vértices**
de leito acima da água, já com a escavação corrigida dentro de cada trecho. A correção
é escavar cada trecho até o nível do **vizinho mais baixo**. Depois: **0**.

### Montagem por blocos: o rio na emenda

Dois defeitos só aparecem quando os blocos são postos lado a lado, e ambos se
manifestam no rio, que é a feição mais longa a atravessar a divisa:

- **A referência de altura era por bloco.** Cada campo de altura normaliza subtraindo
  o mínimo do próprio recorte — certo numa cena única, errado em blocos: dois vizinhos
  têm mínimos diferentes, o mesmo ponto do terreno recebe z diferente nos dois, e o rio
  aparece em duas alturas. A região agora mede a referência uma vez e a passa a todos
  os blocos.
- **A cor da água era agrupada por bloco.** O k-means agrupa *as amostras que
  recebeu*: cada bloco via só os seus corpos d'água, achava centros diferentes e
  batizava todos de `water_sat_00`, `water_sat_01`… O mesmo rio saía com uma cor num
  bloco e outra no vizinho. Agora a cor cai num **reticulado fixo** de 15 níveis por
  canal e o nome do material sai da célula — dois blocos que amostram o mesmo rio
  produzem o mesmo material.

## Pontes

Havia um sistema de ponte antes, e ele fazia uma coisa só: fechar o vão por baixo com
uma laje. A premissa estava escrita no código — *"a pista permanece no nível do solo,
quem está rebaixado é o rio"* — e ela é falsa quando há relevo.

Com relevo a pista é **assentada no terreno**, e o terreno debaixo do rio acabou de ser
escavado alguns metros pela geração da água. A pista então acompanhava a escavação e
**mergulhava no leito**: descia dentro do rio, atravessava o fundo e subia do outro
lado, com a laje descendo junto. Medido em Ouro Preto (700 m, 303 m de desnível, 30
travessias), a pista sobre a água ficava **0,37 m abaixo da lâmina na mediana e 4,16 m
no pior caso**. Não era ponte; era vau.

Uma ponte precisa de três coisas que a laje sozinha não dava:

1. **Vão plano.** O trecho de pista sobre a água **sai** da superfície assentada e é
   substituído por geometria de altura absoluta, no nível dos dois encontros.
2. **Guarda-corpo.** Sem mureta a ponte lê como uma tira de asfalto flutuando. O anel
   é cortado onde a ponte encontra o resto da pista — senão as entradas fecham e a
   ponte vira caixa.
3. **Apoio.** Vão acima de 26 m recebe pilar, do leito escavado até o fundo do
   tabuleiro. Isso também resolve os **viadutos tagueados**, que ficavam suspensos no
   ar sem nada embaixo.

Três medidas que custaram uma medição cada:

- **O vão cobre o talude.** Se o tabuleiro parasse na beira da água, a pista assentada
  ainda desceria a rampa de escavação inteira antes de encontrá-lo, e sobraria um
  degrau na entrada.
- **O encontro é amostrado além do talude.** Dentro do talude o terreno já foi
  rebaixado pela própria água; medir ali dava um tabuleiro na altura do rio em vez da
  altura da margem — a ponte nascia afundada e o pilar era descartado por não caber
  embaixo dela.
- **O encontro é a entrada do próprio tabuleiro, não um anel em volta.** A primeira
  correção media a altura num anel de pista de 14 m ao redor da travessia, e isso é um
  erro grosseiro em cidade de montanha: em Ouro Preto o anel pega a ladeira que passa
  perto do córrego, e o tabuleiro sobe com ela. Medido: **19 de 27 tabuleiros mais de
  6 m acima do próprio vão**, dois deles a 99 e 121 m no ar. A altura passou a sair da
  faixa onde o tabuleiro encosta no resto da pista — a única amostra que descreve por
  onde a pista de fato chega na ponte —, com um teto de 5 m acima da margem como rede
  de segurança.

Resultado em Ouro Preto, na malha final: **nenhum triângulo de pista ou calçamento
abaixo da lâmina**, mediana do tabuleiro a 4,6 m acima da água e p90 a 7,7 m.

O limiar de travessia é baixo de propósito (2 m²). A tentação é reservar a ponte para o
rio de verdade, mas a vala também é escavada — o leito desce 1,05 m seja rio ou valeta
—, então sem tabuleiro a pista mergulha nela do mesmo jeito. Com o limiar em 10 m²
sobravam 39 vértices de pista dentro da água, todos em travessias pequenas; um
tabuleiro de 3 m² custa doze triângulos.

Em Ouro Preto: **23 tabuleiros para 30 travessias** (as vizinhas se fundem num vão só),
e 1,48 m² de 1338 m² de travessia sem ponte — as três lascas abaixo do limiar, onde uma
rua apenas roça a esquina de um córrego.

A sinalização horizontal recebeu o mesmo veto. Ela é assentada no relevo como a pista,
então sobre o rio ela descia com o terreno e reaparecia no fundo, debaixo da lâmina,
enquanto a pista já tinha subido para a ponte: **47 vértices de pintura submersos**, a
0,47 m em média da superfície da água. Agora ela não é desenhada sobre água.

## Física: `--colliders`

A malha bonita não serve como colisor. Um prédio tem parede, telhado de quatro águas,
beiral, janela e platibanda — centenas de triângulos que a física teria de testar a
cada quadro para responder uma pergunta simples: *"bati na casa?"*. Uma engine responde
isso com **uma caixa**.

Então a física sai em paralelo, com as formas que ela quer:

| elemento | forma | por quê |
|---|---|---|
| edifício | caixa orientada | casa raramente é paralela ao norte; a caixa alinhada ao eixo sobraria |
| árvore | cilindro no tronco | ninguém esbarra em folha; cercar a copa dobraria o espaço ocupado |
| chão | campo de altura | formato nativo de terreno em toda engine, e o mais barato que existe |
| água | volume | serve de gatilho: nadar, afogar, frear |

As formas são anotadas **pelos próprios geradores enquanto constroem o visual**, e não
por uma segunda leitura do mapa. É o que garante que o colisor bate com o que se vê: a
caixa do prédio usa a altura que o gerador de fato escolheu.

Duas saídas, porque as engines não concordam:

1. **Malha no próprio GLB**, em nós separados com sufixo configurável — Godot entende
   `-colonly`, Unreal usa `UCX_`. `glTF não tem física no padrão`, então convenção de
   nome é o único canal que existe.
2. **JSON ao lado** (`mapa.colisores.json`) com as formas *analíticas* — centro,
   tamanho, raio, giro. É o que permite criar colisor **primitivo** na engine em vez de
   trimesh, que é onde está a diferença de desempenho: uma caixa custa quase nada, uma
   malha de mil triângulos custa mil vezes mais.

O sidecar de física é um arquivo separado por tamanho: o campo de altura é uma matriz,
e numa região de alguns km² ela sozinha é maior que todo o resto dos metadados somado.

O material de colisão é **totalmente transparente**, e o viewport e o renderizador o
ignoram pelo nome. A primeira versão usava vermelho semitransparente "para
identificar", e o resultado foi a física aparecer por cima do mapa inteiro — que é
exatamente o que um colisor não deve fazer.

## Peso da cena

Antes de otimizar, medir. O orçamento de Araioses (1,4 km, satélite + relevo +
vegetação detectada) tinha um item que não devia estar lá:

```
215.601  41,1%  curb        <- embasamento dos prédios
 44.028   8,4%  window
 39.746   7,6%  trunk
 38.642   7,4%  ground_pintado
```

O embasamento é uma faixa de **18 cm** rente ao chão. Custava 41% da cena inteira e
oito vezes o que custavam todos os prédios — porque `poly.buffer(0.18)` usa junta
redonda por padrão, e cada canto do contorno virava 16 vértices. Com
`join_style=2` (esquadria) o embasamento tem exatamente os cantos do prédio, e a
18 cm ninguém vê a diferença. Uma palavra, 215 mil triângulos.

Somado a parar de desenhar os retalhos de landuse sobre a textura pintada
(25 mil triângulos que só reintroduziam o degrau duro), a escada de detalhe ficou:

| detalhe | triângulos | arquivo | uso |
|---|---|---|---|
| `high` | 565k | 16,8 MB | close-up |
| `medium` | 281k | 9,2 MB | padrão — era 524k / 17,6 MB |
| `low` | 90k | 4,1 MB | anel intermediário |
| `distante` | 64k | 3,4 MB | anel externo do carregamento dinâmico |

Nove vezes entre o mais caro e o mais barato, com a mesma região.

## Carregamento dinâmico: `streaming.py`

### O Brasil inteiro cabe num modelo?

Não, e não é questão de disco. Com o custo medido acima:

| | triângulos | arquivo |
|---|---|---|
| Brasil em `medium` | 1,22 **trilhão** | 40 TB |
| Brasil em `distante` | 277 **bilhões** | 14,5 TB |

Nenhuma engine carrega 277 bilhões de triângulos e nenhuma placa desenha isso. Um
modelo único do Brasil não é caro — é inútil.

Mas a **matéria-prima** cabe: o OSM do Brasil tem ~2 GB em PBF, o relevo a 30 m cerca
de 19 GB, e os contornos do Overture algumas dezenas de GB. Ou seja: *o modelo do
Brasil não existe, mas o Brasil existe* — como dado de entrada de onde qualquer
pedaço é construído em segundos.

### Como funciona

Nada é pré-gerado. O mundo é uma grade fixa indexada por `(nível, coluna, linha)`;
um bloco só passa a existir quando a câmera chega perto; o detalhe cai com a
distância, em anéis; e o que sai do alcance é descartado por um teto de memória.

| anel | até | detalhe | lado do bloco |
|---|---|---|---|
| 0 | 1,5 km | `medium` | 1 km |
| 1 | 4 km | `low` | 2 km |
| 2 | 12 km | `distante` | 4 km |

```python
from mapforge.streaming import WorldStreamer

streamer = WorldStreamer(settings, cache=cache, workers=3)
streamer.move(-2.8909, -41.9050, radius_m=900)
streamer.wait()                    # ou consulte streamer.ready() a cada quadro
for tile in streamer.loaded():     # do mais perto para o mais longe
    ...
```

**A memória depende do raio de visão, não do tamanho do mundo:** uma vista pede de 18
a 57 blocos, seja em Araioses ou no meio da Amazônia. Medido em Araioses, raio de
900 m: 15 blocos, 1,64 M triângulos, 139 s a frio, zero falhas. Andando 1 km ao
norte, **10 dos 16 blocos são reaproveitados** e só 6 são gerados.

Dois defeitos que só apareceram rodando de verdade:

- **O cache SQLite não era seguro entre threads.** Os blocos são gerados em paralelo
  e todos consultam o cache; o SQLite recusa uma conexão criada em outra thread, e
  **45 de 46 blocos falhavam** com uma mensagem que não parecia ter nada a ver com
  geração de mapa. A conexão passou a ser serializada por lock, com os resultados
  materializados dentro dele (devolver o cursor não bastaria: ele é posicional).
- **Uma consulta Overpass por vista estoura o limite de 25 km².** Uma vista de 12 km
  de raio tem ~98 km². O download é agrupado por nível — um bloco de nível 2 tem
  16 km² sozinho, então quantos cabem por consulta depende do nível.

O gargalo continua sendo a rede, não a malha: um bloco leva de 15 a 42 s, quase tudo
esperando imagem de satélite. O bloco `distante` já baixa a foto num zoom mais baixo
e pula a detecção de vegetação — sozinho isso o levou de 140 s para 30 s.

## Relevo

```powershell
py -3.11 run.py generate --place "Ouro Preto, MG" --radius 500 --relief --satellite
```

O modelo digital de elevação vem dos **AWS Terrain Tiles** (esquema Terrarium,
dados públicos SRTM/NED), pelo mesmo transporte de tiles da imagem de satélite — o
que muda é a leitura dos pixels: `altura = R*256 + G + B/256 − 32768`. Chega a z15,
ou ~4,5 m por pixel: resolução de encosta e vale, não de calçada.

O terreno deixa de ser um retângulo e vira uma malha regular. Mas o que importa mais
é que ele vira um **campo de altura consultável**: ruas, calçadas, prédios, água e
vegetação perguntam a altura do chão antes de se posicionar.

- **Superfícies drapeadas** (ruas, calçadas, sinalização, áreas verdes) deslocam cada
  vértice pela altura do terreno.
- **Objetos rígidos** (prédios) usam um deslocamento único: assentam no ponto mais
  baixo do contorno e enterram 60 cm, senão numa encosta o prédio ficaria sobre
  palafitas de um lado.
- **Água** escava a própria grade do terreno: cada corpo d'água rebaixa os vértices
  ao seu nível (percentil baixo das alturas sob ele — a média faria a água subir a
  encosta em vales estreitos) e a lâmina fica plana por cima.

Um detalhe que não é cosmético: a consulta de altura interpola **exatamente sobre os
mesmos triângulos** da malha desenhada, não por bilinear. Se as duas divergissem, as
ruas afundariam ou flutuariam alguns centímetros em toda encosta. Há um teste que
fixa esse invariante: um plano inclinado tem de ser reproduzido com erro zero.

Ouro Preto, 1 km²: 237 m de desnível.

### Fontes e licenciamento

| Fonte | Chave | Zoom | Observação |
|---|---|---|---|
| `esri` | — | ≤19 | Padrão. Atribuição obrigatória. |
| `mapbox` | `MAPBOX_TOKEN` | ≤22 | Melhor resolução onde existe. |
| `bing` | `BING_KEY` | ≤20 | |
| `custom` | `MAPFORGE_TILE_URL` | configurável | Template XYZ próprio. |
| `terrarium` | — | ≤15 | Elevação (AWS Terrain Tiles), usado por `--relief`. |

Contornos de edifícios:

| Fonte | Licença | Atribuição |
|---|---|---|
| [Overture Maps](https://overturemaps.org/) (`--overture`) | mistura por edifício: CC BY 4.0 no trecho Google/Esri, ODbL no trecho OSM/Microsoft | obrigatória |
| [Microsoft GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints) (`--footprints`) | ODbL | obrigatória |

`mapforge.data.attribution()` monta o texto exigido a partir das fontes que a cena
realmente usou. Ambas as licenças são compatíveis com uso acadêmico e aberto; a ODbL
tem cláusula de compartilhamento pela mesma licença sobre a base derivada.

**Onde o Google Maps mobile acerta casa por casa, é este dado.** Não há detecção no
aparelho: a rede neural rodou offline e o resultado é servido como vetor. Reimplementar
essa detecção com processamento clássico de imagem rende metade da precisão — está
medido acima, na seção de `--detect-buildings`.

```powershell
$env:MAPFORGE_TILE_URL = "https://servidor/{z}/{x}/{y}.jpg"
$env:MAPFORGE_TILE_ATTRIBUTION = "Ortofoto municipal 2024"
```

O provedor `custom` existe para quem tem acesso licenciado a outra imagem — ortofoto
de prefeitura, convênio institucional, chave própria de um serviço. **Imagens do
Google Maps/Earth não são uma opção**: os Termos de Serviço proíbem raspar tiles e
gerar produtos derivados, independentemente de o uso ser comercial ou acadêmico. O
caminho licenciado para Google seria a API oficial com chave própria, apontada em
`MAPFORGE_TILE_URL`.

Dados do OpenStreetMap são ODbL — atribuição obrigatória no que for publicado.

## Arquitetura

```
mapforge/
  core/         BBox, projeção local, objetos do mapa, MeshBuilder
  data/         downloader Overpass, Overture, cache SQLite, parser OSM
  generation/   terreno, ruas, pontes, prédios, vegetação, água, colisores, orquestrador
  imagery/      tiles de satélite e de elevação, mosaico, amostragem de cores
  styles/       paletas e parâmetros por estilo
  export/       Scene -> GLB/GLTF/OBJ/PLY
  render/       rasterizador offscreen (numpy)
  ui/           janela PySide6, mapa de seleção (HTML/JS próprio), viewport ModernGL
  preview.py    planta 2D (matplotlib)
  pipeline.py   API de alto nível
  tiling.py     geração em blocos + manifesto de montagem
  streaming.py  carregamento dinâmico por anéis de detalhe
  cli.py        linha de comando
```

Pipeline: `bbox → Overpass → parser → MapData → (satélite, relevo) → geradores →
Scene → viewport / exportador`.

### O que a cidade declara ser

O OSM não diz só "aqui tem um prédio": diz que ali é o hospital, a delegacia, o
corpo de bombeiros, a igreja, a escola. Cada `amenity`/`emergency`/`building`
conhecido vira um **arquétipo** com faixa de pavimentos, forma de telhado, cor e
marca própria. Medindo em quadrados de 1,2 km:

| Região | Prédios | Com arquétipo |
|---|---|---|
| Ouro Preto, MG | 195 | 52 (11 hotéis, 10 igrejas, 6 escolas, 5 museus, quartel, fórum) |
| Araioses, MA | 13 | 9 (hospital, polícia, prefeitura, 2 bancos, igreja, escola) |

O quartel sai vermelho, o hospital branco, o galpão com telhado de uma água, e a
igreja ganha campanário — que é o marco visual de qualquer cidade pequena
brasileira; sem ele a igreja fica indistinguível de um galpão.

### Estruturas verticais

`man_made`, `power` e `tower:type` viram torre de telefonia, caixa d'água elevada,
silo, chaminé industrial, torre de transmissão, poste, cata-vento e outdoor. A
posição vem do OSM — onde o mapa diz que há uma antena, há uma antena. Ouro Preto
tem 12 mastros mapeados na área de 1,2 km.

A separação entre urbano e rural não é uma regra escrita no código: cai do dado.
Silo e cata-vento só existem onde alguém mapeou lavoura; antena e outdoor só
existem onde há cidade.

**Iluminação pública** é a exceção: quase nenhuma cidade mapeia poste, então ela é
deduzida do próprio traçado, com espaçamento por classe viária e alternando de lado
como na rua de verdade.

### Perfil regional

Casa brasileira não tem chaminé, mas tem caixa d'água no telhado. Casa alemã tem o
contrário. Gerar os dois em todo lugar deixa a cena errada nos dois casos.

A latitude do centro escolhe o perfil — tropical, subtropical, temperada ou boreal —
e ele define a probabilidade de chaminé e de caixa d'água, além de inclinar a mistura
de telhados (telhado plano é raro onde neva) e de espécies de árvore (palmeira no
trópico, conífera no norte). `--region` força um perfil específico.

### Sistemas de coordenadas

Três, e a conversão acontece sempre no mesmo lugar:

1. **Geográfico** (graus): só até o parser.
2. **Local** (metros, X=leste, Y=norte, Z=altura): tudo do parser em diante.
   Projeção equirretangular centrada na bbox — erro abaixo de 0,1% na escala de
   uma cidade, o que também deixa o mapeamento para os pixels do satélite linear.
3. **glTF** (Y-up): só no exportador, por rotação de −90° em X.

### Camadas verticais

`generation/layers.py` concentra as alturas de cada superfície. A folga entre elas
evita z-fighting sem depender de polygon offset no renderizador. Com `--relief` esses
valores viram folgas *acima do terreno* em vez de alturas absolutas — o resto do
código não muda.

### Assentar no relevo sem quebrar as ruas

O problema não é óbvio e custou uma medição para achar. Um polígono de rua vem do
OSM com poucos vértices — 11 para 800 m. Deslocar cada vértice pela altura do terreno
não basta: o earcut liga vértices distantes, produzindo triângulos com arestas de até
410 m que passam retos por cima dos morros. Medido num terreno ondulado, **a rua
afundava 6,46 m abaixo do terreno**.

Densificar a borda também não resolve — o earcut continua ligando pontos distantes
(erro caiu para 16 m, pior).

A solução é recortar a geometria pelos **próprios triângulos do terreno** antes de
triangular, usando a mesma grade e a mesma diagonal que `TerrainField.height()` usa
para interpolar. Assim cada peça cai inteira dentro de um triângulo do terreno, onde
a altura é exatamente linear. O erro passou a ser **exatamente a folga deliberada de
8 cm**, uniforme.

Custo: mais triângulos e alguns segundos a mais na geração. As células do terreno e o
índice espacial delas são construídos uma vez por cena, de forma vetorizada — num
laço Python isso sozinho levava dezenas de segundos.

A grade do terreno tem **teto de células independente do nível de detalhe**. Sem ele,
uma região de 11 km² em detalhe alto pedia 615 mil triângulos só de terreno e a
geração passava de dois minutos; o passo é afrouxado até caber. Também não adianta
uma grade mais fina que o próprio DEM (~4,5 m), então esse é o piso.

| Região de 11 km², com relevo | Antes | Depois |
|---|---|---|
| Detalhe médio | 53,6 s / 493 mil tri | **16,4 s / 312 mil tri** |
| Detalhe alto | 132,6 s / 1,04 milhão tri | **17,6 s / 317 mil tri** |

### Dois gargalos quadráticos

Ambos foram medidos, não estimados, e ambos eram meus:

**Recorte célula a célula.** A primeira versão intersectava cada célula da grade
contra a união inteira das vias. Cada uma das milhares de operações percorria uma
geometria com dezenas de milhares de vértices — a etapa de ruas levava **290 s** numa
região de 0,6 km². Trocado por uma **sobreposição planar única**: noda o contorno da
geometria com as linhas de grade de uma vez e remonta as faces com `polygonize`.
Resultado: 6 s, com desvio do terreno **0,000 m** e área preservada em 100,00%.

**Resolução de sobreposição entre edifícios.** Eu acumulava a união de tudo já
posicionado e subtraía essa união inteira a cada prédio. Com os 1100 contornos que a
detecção produz em Araioses, isso sozinho passava de dois minutos:

| Prédios | Antes | Depois |
|---|---|---|
| 300 | 7,1 s | 0,02 s |
| 600 | 31,3 s | 0,06 s |
| 1200 | **132,6 s** | **0,18 s** |

Agora cada candidato só disputa com os vizinhos que um STRtree aponta. Mesmo
resultado (1140 mantidos dos 1200), 700× mais rápido.

### O que é procedural

Do OSM vêm as formas: contorno dos prédios, traçado das ruas, limites de parques e
água. O resto é gerado:

- **Prédios** — classificados pela área (casa → sobrado → prédio → centro comercial).
  Cinco formas de telhado: plano (com platibanda), duas águas, piramidal, mansarda e
  uma água, vindas da tag `roof:shape` quando existe e do estilo quando não. A forma
  é filtrada pelo contorno: duas águas só fecha bem em planta retangular e piramidal
  só em planta convexa, então uma planta em L nunca sai com cumeeira torta. Somam-se
  janelas por pavimento, porta na fachada mais longa, embasamento, chaminé nas casas
  de telhado inclinado e sacadas nos prédios altos.
- **Ruas** — eixo suavizado por arredondamento de esquina com raio proporcional à
  largura (os trechos retos continuam retos, então não se gasta triângulo à toa);
  largura pela classificação viária ou pelas tags `width`/`lanes`; pavimento pela
  tag `surface` (asfalto, calçamento de pedra, terra); calçada como faixa elevada
  cujo degrau já é o meio-fio, recortada pelos footprints dos prédios; eixo
  tracejado, faixa de bordo e faixa de pedestre. As vias são unidas por tipo de
  pavimento e subtraídas em ordem de prioridade antes de virar malha: acaba com as
  costuras nos cruzamentos e impede que uma calçada flutue sobre uma avenida.
- **Água** — o canal é escavado: o terreno é recortado, as margens descem em talude
  até o leito e a lâmina fica abaixo do solo. Cursos d'água são suavizados por
  Chaikin, o que transforma o traçado quebrado do OSM em meandro. Rio comprido é
  fatiado em trechos com nível próprio, que nunca sobe para jusante. Onde uma via
  cruza a água, o trecho de pista sai da superfície assentada e é substituído por um
  tabuleiro plano com guarda-corpo e pilares.
- **Vegetação** — sete espécies (copa arredondada, cônica, pinheiro em camadas,
  cipreste, palmeira, copa de guarda-chuva e arbusto), cada uma com tronco e copa
  próprios porque a proporção muda muito — palmeira é quase só estipe, arbusto quase
  não tem. A mistura depende do contexto: bosque puxa conífera e pinheiro, parque
  urbano puxa copa larga e palmeira, e a árvore de rua sai mais baixa porque é podada.

  A densidade **não é uniforme dentro do polígono**: com a imagem de satélite ligada,
  o excesso de verde de cada ponto decide se ali nasce árvore. Dentro de um parque há
  campo de futebol, pátio e mata, e plantar igual nos três é o que dá cara de carimbo.
  A distribuição também não é uma grade regular — amostra mais denso que o alvo,
  desloca até 70% do passo e rejeita parte, porque fileira alinhada vista de cima é o
  que mais denuncia vegetação gerada por computador.

  Árvore não nasce dentro de casa: os footprints entram na máscara de exclusão junto
  com o asfalto, inclusive para as árvores mapeadas no OSM — árvore em cima de telhado
  é erro de posição, não realidade.

Tudo derivado da seed via `GenerationContext.rng(osm_id)`: cada objeto tem seu
próprio gerador estável, então acrescentar prédios não muda os que já existiam.

### Formato de projeto

`projects` no SQLite guarda bbox, seed, estilo e configurações — nunca a malha.
Arquivos minúsculos, e a cidade é regenerada a partir dos parâmetros.

## Estilos

`lowpoly` (tons naturais), `cartoon` (saturado) e `futurista` (noturno, janelas
emissivas). Um estilo é paleta + parâmetros (`styles/presets.py`); o mesmo mapa muda
por completo trocando o nome.

Os estilos Medieval e Japonês do planejamento pedem substituição de assets, não só
de cores — dependem da biblioteca de assets da Fase 4.

## Desempenho

| Região | Fonte | Malha | Geração | GLB |
|---|---|---|---|---|
| Ouro Preto, 900 × 900 m | 155 prédios, 269 vias | 77 mil tri | 0,6 s | 1,9 MB |
| Centro de SP, 1 × 1 km | 1801 prédios, 768 vias | 830 mil tri | 5,5 s | 28,5 MB |
| Centro de SP, `--no-windows` | idem | 259 mil tri | 3,4 s | 7,9 MB |

As janelas dominam a contagem de triângulos em cidade densa: são uma placa por
pavimento por vão. `--no-windows` ou `--detail low` cortam o arquivo em ~4×.

O download é o gargalo, não a geração; por isso o cache SQLite (OSM por 30 dias,
tiles de satélite sem prazo). Chunks de 256 m, culling e streaming são a Fase 6 e
ainda não existem.

**Espelhos do Overpass.** Responder `504` quando está ocupado é comportamento normal
desses servidores — o principal chega a gastar 17 segundos só para recusar. O espelho
que respondeu por último é tentado primeiro na vez seguinte, o que economiza esse
tempo e distribui a carga por quem está saudável em vez de fixar uma preferência no
código. Cair para o espelho seguinte não é erro: só aparece como aviso se todos
falharem.

**Só entra na lista espelho com o planeta inteiro.** Várias instâncias públicas
servem apenas um país. `overpass.osm.ch` responde rápido e com sucesso para uma bbox
brasileira — e devolve zero elementos, porque só tem a Suíça. Como "sucesso rápido"
era o critério da preferência, ele foi memorizado e passou a esvaziar todo mapa sem
levantar erro nenhum. Três salvaguardas agora impedem isso:

1. resposta vazia não conta como espelho bom e cede a vez para o próximo;
2. resposta vazia não vai para o cache;
3. um vazio já gravado no cache é descartado e a consulta é refeita — assim uma
   instalação envenenada se cura sozinha.

O vazio só é aceito quando **todos** os espelhos concordam, que é o caso legítimo de
região deserta.

O renderizador offscreen é software puro: ~9 s para 77 mil triângulos, ~90 s para
830 mil. Serve para conferência e miniaturas, não para navegação.

## Testes

```powershell
py -3.11 -m pytest tests -q
```

482 testes, sem rede: geometria e winding das malhas, parser (com fixture Overpass),
suavização de eixos, classificação viária, escavação da água, matemática de tiles e
quadkeys, detecção do tile-placeholder, decodificação Terrarium, exatidão do campo de
altura, **regressão do assentamento no relevo**, fusão de contornos, classificação de
pixel e divisão de manchas do detector, recorte de edifícios contra vias e
sobreposições, arquétipos urbanos, todas as estruturas verticais, perfis regionais,
todas as formas de telhado, todas as espécies de árvore, amostragem de cores,
determinismo da seed, combinações de estilo × detalhe, fallback entre espelhos do
Overpass, recorte por contorno desenhado, plano e manifesto da geração em blocos, e
exportação com reimportação.

Cada defeito medido virou um invariante:

| teste | fixa |
|---|---|
| `test_pontes.py` | leito sem ilhas, nível por trecho não sobe para jusante, pista sobre o rio acima da água, tabuleiro plano e na altura da margem, guarda-corpo com as entradas abertas |
| `test_colliders.py` | a caixa usa a altura que o gerador escolheu, a física não é desenhada, o sidecar sai em arquivo próprio |
| `test_navegacao.py` | o avanço é horizontal, o passo acompanha a distância, teclas opostas se cancelam |
| `test_tiling.py` | a referência de altura é a mesma em todos os blocos, a mesma cor de água gera o mesmo material |
| `test_gaps.py` | a detecção por imagem só vale onde nenhuma fonte de contorno chegou |

## Limitações conhecidas

- O relevo vem de um DEM de ~4,5 m/pixel: pega encosta e vale, não corte de rua nem
  talude de muro. Sem `--relief` o terreno continua plano.
- Prédio muito comprido em encosta forte fica com um degrau visível na base, porque
  assenta no ponto mais baixo do contorno em vez de acompanhar o talude.
- Com `--relief`, o recorte pelos triângulos do terreno multiplica os triângulos das
  superfícies assentadas e acrescenta alguns segundos à geração. É o preço de a rua
  não atravessar o morro.
- Os arquétipos cobrem o que o OSM declara. Onde ninguém marcou `amenity=*`, o prédio
  continua genérico — e é o caso da maioria.
- O tabuleiro do viaduto tagueado continua acompanhando o relevo (fica a uma altura
  constante acima do terreno) em vez de vencer o vão em linha reta; os pilares agora
  o apoiam, mas um viaduto sobre terreno muito acidentado ainda ondula.
- Travessia de água abaixo de 2 m² não ganha tabuleiro: ali a pista continua assentada
  e desce o pouco que o leito foi escavado. Em Ouro Preto isso é 1,48 m² de 1338 m².
- Telhado de duas águas usa a caixa orientada mínima do contorno: fica correto em
  plantas retangulares, aproximado nas demais (por isso a troca automática para
  piramidal ou plano quando a forma é irregular).
- A cor amostrada do satélite vem da imagem vista de cima, então prédios altos podem
  contaminar a amostra com a sombra projetada sobre os vizinhos.
- A imagem informa cor, não dimensão: as alturas continuam vindo das tags do OSM ou
  da regra procedural. Estimar altura por sombra seria um projeto à parte.
- O viewport pede OpenGL 3.3; em máquina sem aceleração o resto do app funciona, mas
  a aba 3D fica vazia. Nesse caso use *Salvar render PNG* ou abra o GLB externamente.
- Área limitada a 25 km² por download (`config.MAX_AREA_KM2`) e 400 tiles por
  imagem, para não abusar dos serviços públicos.
- Sem editor: não dá para mover, apagar ou trocar objetos individualmente (Fase 5).
- Os contornos da Microsoft não têm classificação de uso: todo prédio acrescentado
  entra como genérico, então a altura procedural dele cai na regra por área. O
  Overture traz `class` e `subtype`, mas em cidade pequena vêm quase sempre vazios.
- Nenhuma das duas fontes traz altura em cidade pequena brasileira (a Microsoft vem
  `-1`, o Overture vem nulo). A altura continua saindo das tags do OSM, da sombra ou
  da regra procedural, agora limitada pelo teto do perfil urbano.
- O perfil urbano é medido na janela pedida, não na cidade. Recortar só o centro de
  uma cidade média pode classificá-la acima do que ela é — use `--urban-scale` nesse
  caso.
- A vegetação detectada não distingue espécie nem mede altura de copa: a espécie sai
  do perfil regional por latitude e a altura é sorteada na faixa da espécie.
- O dossel é uma superfície, não árvores: visto de perto e de baixo ele denuncia que
  é uma casca. Ele existe para a vista de cima e média distância, que é para o que
  este gerador serve — por isso ele fica desligado por padrão (`--canopy-shell` liga).
- A primeira consulta ao Overture numa região leva de 20 s a 2 min, dependendo da
  latência do S3. Só a primeira: depois é cache.
- A detecção por imagem devolve retângulos orientados, não contornos exatos, e não
  distingue casa de galpão nem telhado de areia — 61,4% do que ela desenha em
  Araioses é falso positivo. Serve para povoar a cena com volumes plausíveis onde não
  há dado nenhum — não para levantamento cadastral.
- A detecção não funciona em área grande: o zoom do satélite cai conforme a região
  cresce e abaixo de ~1,2 m/pixel ela se desliga sozinha.
- A estimativa por sombra precisa de imagem com pelo menos ~1 m/pixel, prédio
  isolado o bastante para a sombra não cair no vizinho, e prédios de referência com
  altura conhecida para calibrar. Onde isso não existe, ela não é aplicada.

