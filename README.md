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

Fora do planejamento original, já funcionando: viewport 3D navegável, imagem de
satélite como fonte de cor, terreno com relevo real, e um renderizador offscreen em
software para gerar imagens sem abrir uma engine.

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
busca por nome ("Ouro Preto, MG" + Enter) e seleção da região: clique em *Selecionar
área* e arraste, ou segure Shift e arraste a qualquer momento. O retângulo mostra as
dimensões em metros e a área em km² enquanto você arrasta. Também dá para selecionar
um quadrado de raio fixo em volta do centro da vista.

**Aba 3D** — viewport navegável com a cena gerada, a mesma que vai para o GLB:

| Ação | Controle |
|---|---|
| Orbitar | arrastar com o botão esquerdo |
| Deslocar | arrastar com o botão do meio ou direito |
| Aproximar | roda do mouse |
| Enquadrar | tecla `F` ou botão *Enquadrar* |

**Aba Imagem** — planta 2D e renders salvos.

**Painel** — estilo, detalhe, seed, satélite, relevo, camadas, altura dos prédios e
densidade de vegetação. *Prévia 2D* roda só o parser (rápido); *Gerar 3D* roda o
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
--seed 42
--height-scale 1.5        # multiplica a altura dos prédios
--tree-density 2.0
--no-buildings --no-roads --no-vegetation --no-water --no-sidewalks --no-terrain
--no-windows              # maior redução de tamanho de arquivo
```

Fontes de edifícios:

```powershell
--footprints              # completa o OSM com os contornos abertos da Microsoft
--detect-buildings        # detecta telhados na imagem (exige --satellite)
--shadow-heights          # estima altura pela sombra na imagem (exige --satellite)
```

Satélite e relevo:

```powershell
--satellite               # cores reais: telhados, terreno, parques, bosques
--provider esri|mapbox|bing|custom
--roof-blend 0.75         # peso da foto nos telhados (0 = só estilo, 1 = só foto)
--area-blend 0.55         # idem para terreno e áreas verdes
--satellite-zoom 18       # força o nível (padrão: o maior com cobertura real)
--ground-texture          # experimental: cola a foto no terreno

--relief                  # terreno com relevo real
--exaggeration 1.5        # multiplica o desnível (1.0 = escala real)
--relief-zoom 15
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

## Cidades pequenas: de onde vêm os edifícios

O OpenStreetMap só tem prédio onde alguém desenhou. Em cidade pequena isso costuma
dar dois ou três prédios num quarteirão inteiro, e a cena sai vazia. Medindo em
quadrados de 800 m:

Três fontes, aplicadas em cascata — cada uma só preenche o que a anterior deixou:

| Região | OSM | `--footprints` | `--detect-buildings` |
|---|---|---|---|
| Santa Bárbara, MG | **5** | **1105** | — |
| Araioses, MA | **12** | 12 (sem cobertura) | **1103** |
| Tiradentes, MG | 407 | 502 | +98 |
| Centro de SP | 1816 | 1816 | — |

`--footprints` acrescenta o **GlobalMLBuildingFootprints** da Microsoft: 1,4 bilhão
de contornos extraídos de imagem de satélite por rede neural, publicados sob ODbL.
É literalmente "ler a imagem de satélite para criar os edifícios" — com a detecção
já feita, validada e distribuída, em vez de reimplementada aqui com resultado pior.
Detecção de contorno em raster por processamento clássico (limiar, morfologia,
componentes conexos) confunde sombra, piscina e carro com telhado; não compensa.

O OSM sempre vence: onde ele já desenhou o prédio, o contorno da Microsoft é
descartado. O critério é sobreposição de 30% relativa ao menor dos dois, que pega
tanto "mesmo prédio com contorno diferente" quanto "quarteirão mapeado como um
polígono só".

Particionamento por quadkey de zoom 9 (~78 km de lado): um arquivo cobre uma região
inteira. O primeiro download é grande (10–30 MB) e lento, depois fica em cache e o
custo é zero. O índice (7 MB) também é cacheado, por 60 dias.

### Quando nem isso cobre: `--detect-buildings`

O conjunto da Microsoft tem buracos. O quadkey `211000300`, que cobre Araioses-MA e
todo o delta do Parnaíba, simplesmente não existe no índice — embora São Luís,
Teresina e Fortaleza existam. Lá, `--footprints` não acrescenta nada.

`--detect-buildings` é a última linha: procura telhados na própria imagem. Sozinha,
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

Duas armadilhas que custaram uma rodada em Araioses:

1. **Casas geminadas viram uma mancha só.** Uma mancha de 1100 m² classificada por
   área virava um prédio de dez andares numa cidade térrea. Agora manchas grandes são
   fatiadas numa grade de unidades do tamanho de uma casa — mas só quando o
   preenchimento do retângulo é baixo. Telhado limpo e cheio é galpão ou escola de
   verdade e fica inteiro.
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

`--ground-texture` existe para colar a foto no terreno com UV planar, mas fica
**desligado por padrão**: a foto crua briga com a leitura low-poly do resto da cena.

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

Contornos de edifícios: [Microsoft GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints),
ODbL — atribuição obrigatória, igual ao OSM.

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
  data/         downloader Overpass, cache SQLite, parser OSM
  generation/   terreno, ruas, prédios, vegetação, água, curvas, orquestrador
  imagery/      tiles de satélite e de elevação, mosaico, amostragem de cores
  styles/       paletas e parâmetros por estilo
  export/       Scene -> GLB/GLTF/OBJ/PLY
  render/       rasterizador offscreen (numpy)
  ui/           janela PySide6, mapa de seleção (HTML/JS próprio), viewport ModernGL
  preview.py    planta 2D (matplotlib)
  pipeline.py   API de alto nível
  cli.py        linha de comando
```

Pipeline: `bbox → Overpass → parser → MapData → (satélite, relevo) → geradores →
Scene → viewport / exportador`.

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
  Chaikin, o que transforma o traçado quebrado do OSM em meandro. Onde uma via
  cruza a água, a pista continua no nível do solo e ganha uma laje por baixo — como
  quem está rebaixado é o rio, a ponte não precisa de rampa.
- **Vegetação** — sete espécies (copa arredondada, cônica, pinheiro em camadas,
  cipreste, palmeira, copa de guarda-chuva e arbusto), cada uma com tronco e copa
  próprios porque a proporção muda muito — palmeira é quase só estipe, arbusto quase
  não tem. A mistura depende do contexto: bosque puxa conífera e pinheiro, parque
  urbano puxa copa larga e palmeira, e a árvore de rua sai mais baixa porque é
  podada. Distribuição em grade jitterada, mais as árvores mapeadas individualmente.

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
tiles de satélite sem prazo) e o fallback entre servidores Overpass. Chunks de
256 m, culling e streaming são a Fase 6 e ainda não existem.

O renderizador offscreen é software puro: ~9 s para 77 mil triângulos, ~90 s para
830 mil. Serve para conferência e miniaturas, não para navegação.

## Testes

```powershell
py -3.11 -m pytest tests -q
```

182 testes, sem rede: geometria e winding das malhas, parser (com fixture Overpass),
suavização de eixos, classificação viária, escavação da água, matemática de tiles e
quadkeys, detecção do tile-placeholder, decodificação Terrarium, exatidão do campo de
altura, drapejamento, fusão de contornos (incluindo a rejeição de duplicatas),
classificação de pixel e divisão de manchas do detector, todas as formas de telhado,
todas as espécies de árvore, amostragem de cores, quantização, determinismo da seed,
combinações de estilo × detalhe, e exportação com reimportação.

## Limitações conhecidas

- O relevo vem de um DEM de ~4,5 m/pixel: pega encosta e vale, não corte de rua nem
  talude de muro. Sem `--relief` o terreno continua plano.
- Prédio muito comprido em encosta forte fica com um degrau visível na base, porque
  assenta no ponto mais baixo do contorno em vez de acompanhar o talude.
- Viadutos tagueados ficam suspensos sem pilares.
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
  entra como genérico, então a altura procedural dele cai na regra por área.
- A detecção por imagem devolve retângulos orientados, não contornos exatos, e não
  distingue casa de galpão. Serve para povoar a cena com volumes plausíveis onde não
  há dado nenhum — não para levantamento cadastral.
- A detecção não funciona em área grande: o zoom do satélite cai conforme a região
  cresce e abaixo de ~1,2 m/pixel ela se desliga sozinha.
- A estimativa por sombra precisa de imagem com pelo menos ~1 m/pixel, prédio
  isolado o bastante para a sombra não cair no vizinho, e prédios de referência com
  altura conhecida para calibrar. Onde isso não existe, ela não é aplicada.
