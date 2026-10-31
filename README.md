# GeoFAT

Plataforma de inteligência geoespacial da CAEMA (Prêmio Inova): identifica imóveis em fotos de fachada, classifica o uso
(residencial, comercial, industrial, institucional, misto, terreno vazio ou inconclusivo) e estima unidades independentes com IA local
(Bonsai), localiza a fachada sobre polígonos de edificações e só no fim cruza com o GSAN para apontar oportunidades de
revisão cadastral.

## Como funciona

```
fotos (EXIF: GPS, direção, focal) ─► detectar (Grounding DINO / YOLO) ─► classificar (Bonsai 2 9B local)
  existentes ou captura própria         1 imóvel por fachada              categoria, unidades, evidências, limitações
                                              │
OSM + Google Open Buildings ─► localizar: trajetória (sentido, lado da via) + setor de visão no PostGIS
                                              │   → edificações candidatas com evidências; sugere a atingida pela
                                              │     linha de visada (sem nenhuma = fica na câmera, pendente)
                                              │   → grupos sugeridos: a mesma fachada vista em fotos diferentes
                                              ▼
                         revisão no mapa: caixa, classificação, localização (confiança alta)
                                              │
GSAN (CSV) ─────────────► associação: sugerida (ligações junto à edificação) ou confirmada pelo revisor
                                              ▼
                              situação comercial + relatório CSV
```

| Pasta / arquivo            | O que é                                                                 |
| -------------------------- | ----------------------------------------------------------------------- |
| `db/schema.sql`            | Tabelas PostGIS e a view `imovel_analise` (associação e situação)       |
| `geofat/fotos.py`          | Lê GPS, direção, focal e data do EXIF (ou `coordenadas.csv`)            |
| `geofat/detectar.py`       | Marca fachadas (Grounding DINO ou YOLO treinado) e filtra duplicatas    |
| `geofat/classificar.py`    | Envia cada recorte ao Bonsai e guarda categoria/unidades/evidências     |
| `geofat/edificacoes.py`    | Importa polígonos do OSM e do Google Open Buildings                     |
| `geofat/trajetorias.py`    | Percursos de captura: ordem, sentido do deslocamento, lado da via       |
| `geofat/localizar.py`      | Edificações candidatas de cada observação (PostGIS); posição manual     |
| `geofat/multivisual.py`    | Grupos de observações do mesmo imóvel (hoje só evidência geográfica)    |
| `geofat/enderecos.py`      | Números lidos (OCR ou revisor) e endereço candidato no CNEFE/OSM        |
| `geofat/publicos.py`       | Importa CNEFE (IBGE) e pontos de uso do OpenStreetMap                   |
| `geofat/gsan.py`           | Importa a exportação do GSAN                                            |
| `geofat/yolo_dataset.py`   | Gera dataset YOLO a partir das caixas revisadas                         |
| `geofat/provedores.py`     | Adaptadores de IA: qual modelo/provedor faz cada etapa                  |
| `geofat/tarefas.py`        | Fila de tarefas em segundo plano, com progresso real e cancelamento     |
| `geofat/datasets.py`       | Datasets de anotação (caixas por classe) e exportação YOLO              |
| `geofat/treino.py`         | Treino YOLO (Ultralytics, CPU) com métricas por época                   |
| `geofat/catalogo.py`       | Download retomável de modelos locais, com SHA-256                       |
| `geofat/servidor_local.py` | Inicia/para o llama-server do Bonsai pela interface                     |
| `geofat/mapillary.py`      | Busca e download de fotos do Mapillary, com licença e projeção da câmera |
| `geofat/planilha.py`       | Lê planilhas de imóveis (.xlsx/.csv) só com os campos que localizam     |
| `geofat/direcionada.py`    | Análise direcionada: alvo → fotos candidatas → detecção → associação → Bonsai |
| `gpu/`                     | Serviço GPU remoto (Runpod): Dockerfile, FastAPI, Grounding DINO, llama.cpp CUDA |
| `geofat/api*.py`, `web/`   | API e interface web (8 páginas)                                         |
| `db/migracoes/`            | Migrações do banco, aplicadas na subida da API ou com `migrar`          |
| `geofat/__main__.py`       | Linha de comando (`python -m geofat --help`)                            |

**Independência do GSAN:** detecção, classificação, localização, grupos e endereços nunca leem `gsan_ligacao`. A análise
direcionada usa da planilha só a coordenada, para saber onde procurar, e nunca a categoria. A API só devolve os dados
do GSAN de um imóvel (inclusive na Análise comercial) depois que a classificação dele foi revisada, e o mapa não mostra
ligações do GSAN. O GSAN é só lido: as decisões do analista ficam no GeoFAT e saem no relatório.

## Instalação (Windows)

Pré-requisitos: Python 3.13 e Docker Desktop.

```bash
docker compose up -d
```

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements.txt
```

Detecção e treino (CPU; ~1 GB, mais ~700 MB do Grounding DINO no primeiro `detectar`):

```bash
.venv\Scripts\pip install -r requirements-ia.txt
```

### Bonsai (classificação local)

Modelo: **Ternary Bonsai 2 9B** (visão + texto), arquivo `Ternary-Bonsai-2-9B-Q2_0.gguf` e projetor de visão
`Ternary-Bonsai-2-9B-mmproj-Q8_0.gguf` (~3,2 GB) de https://huggingface.co/prism-ml/Ternary-Bonsai-2-9B-gguf, em
`modelos/`. Runtime: llama.cpp oficial build b11555 ou mais nova (testado b11846), pacote **Vulkan** para Windows,
descompactado em `ferramentas/llama-vulkan/`. O Vulkan usa a GPU integrada (Intel Iris Xe): ~2,5–3 min por fachada,
contra ~7 min no pacote só de CPU.

```bash
ferramentas\llama-vulkan\llama-server.exe -m modelos\Ternary-Bonsai-2-9B-Q2_0.gguf --mmproj modelos\Ternary-Bonsai-2-9B-mmproj-Q8_0.gguf --no-mmproj-offload -c 8192 -ngl 99 -b 256 -ub 256 --port 1234 --alias Ternary-Bonsai-2-9B-Q2_0
```

`--no-mmproj-offload` (codificador de imagem no CPU) e `-b/-ub 256` (lotes curtos na GPU) evitam o erro
`ErrorDeviceLost`: o Windows reinicia a GPU integrada quando um cálculo passa de ~2 s (TDR).

O servidor também pode ser iniciado e parado pela interface (Modelos › Conexões › Servidor local), com os mesmos
argumentos. Ele roda como processo filho da API: reiniciar a API derruba o servidor.

O `classificar` usa o modo sem raciocínio com a amostragem recomendada pela PrismML. Também funciona com LM Studio,
Ollama ou qualquer servidor compatível com a API da OpenAI (página Modelos).

Variáveis opcionais: `DATABASE_URL`, `FOTOS_DIR` (padrão `data/fotos`), `DECLINACAO_MAGNETICA` (padrão `-21`, São Luís).
`BONSAI_URL` e `BONSAI_MODEL` só definem a configuração inicial do banco; depois vale o que estiver na página Modelos.

## Uso

1. Coloque as fotos em `data/fotos/<pasta>/`. Precisam de GPS: no EXIF (celular com localização ligada) ou num
   `coordenadas.csv` na mesma pasta (`arquivo;latitude;longitude;direcao;credito`, campos vazios = usar o EXIF).
   A direção da câmera (bússola do celular, `GPSImgDirection`) é o que permite localizar a fachada automaticamente.
   Colunas opcionais do CSV: `rumo` (sentido do deslocamento, graus), `lado` (`direito`/`esquerdo`: lado da via que
   a câmera fotografa), `trajeto` e `sequencia`. Do EXIF também são lidos `GPSTrack` (rumo) e o erro do GPS.
   Fotos com a posição no nome (`direita_0034_lat-2_540666_lng-44_215725.jpg`) em `exemplos/image`: rode
   `.venv\Scripts\python.exe ferramentas\importar_exemplos.py` (`--simular` só mostra o que faria). Ele copia para
   `data/fotos/exemplos/`, gera o CSV com lado, percurso e sequência e usa a importação acima; rodar de novo não duplica.
2. Processamento (cada comando só faz o que falta; `--limite N` para lotes):

```bash
.venv\Scripts\python -m geofat importar-fotos minha_pasta --fonte captura_propria
```

```bash
.venv\Scripts\python -m geofat importar-edificacoes osm
```

```bash
.venv\Scripts\python -m geofat importar-edificacoes open-buildings
```

```bash
.venv\Scripts\python -m geofat detectar
```

```bash
.venv\Scripts\python -m geofat localizar
```

```bash
.venv\Scripts\python -m geofat classificar
```

Opcionais, como evidência para o revisor: `importar-osm` (pontos de uso) e `importar-cnefe ARQUIVO.csv`.

3. Interface web (a API aplica as migrações do banco ao subir):

```bash
.venv\Scripts\uvicorn geofat.api:app --port 8000
```

Em http://localhost:8000:

| Página              | O que faz                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------ |
| Visão geral         | Indicadores do banco, andamento de cada etapa, tarefas em execução, situação comercial     |
| Mapa                | Fachadas, edificações (cor = associação sugerida/confirmada), lotes, pontos de captura,    |
|                     | trajetórias; candidatas de cada fachada; ficha da edificação (fotos, grupos, unidades,     |
|                     | endereços); confirmar ou corrigir a edificação                                             |
| Revisão de imagens  | Caixas sobre a foto: desenhar, mover, redimensionar, remover; tipo de cada imóvel; aprovar;|
|                     | mesmo imóvel em outras fotos (reunir, separar, confirmar); número do endereço              |
| Análise comercial   | Importar o CSV do GSAN, associar matrículas, decidir (confirmar/vistoria/descartar), CSV   |
| Modelos             | Modelo de cada etapa, configuração, catálogo com download, conexões e servidor local       |
| Treinamento         | Datasets, treinos YOLO com métricas por época, versões, "usar na detecção"                  |
| Editor de dataset   | Imagens enviadas ou do GeoFAT, caixas por classe, sugestão automática, aprovação, divisão   |
| Processamentos      | Fila: progresso real, logs, cancelar, tentar de novo, histórico, nova tarefa                |

Na Revisão, para cada foto: confira as caixas (mover ou redimensionar devolve a fachada à fila do classificador),
escolha o tipo de cada imóvel e aprove. Só depois disso o GSAN do imóvel aparece na Análise comercial.

4. GSAN e relatório:

```bash
.venv\Scripts\python -m geofat importar-gsan gsan.csv
```

```bash
.venv\Scripts\python -m geofat relatorio oportunidades.csv
```

### Associação com o GSAN e situação comercial

Nunca se assume "a matrícula mais próxima em 30 m". A associação é:

| Associação      | Quando                                                                                  |
| --------------- | --------------------------------------------------------------------------------------- |
| `confirmada`    | O revisor marcou as matrículas (ou confirmou que não há nenhuma)                        |
| `sugerida`      | Localização média/alta e ligações a até 8 m da edificação (ou 10 m do ponto da fachada) |
| `inconclusiva`  | Localização só pela câmera (confiança baixa): nada é sugerido                           |
| `sem_candidato` | Localização confiável, mas nenhuma ligação perto                                        |

Várias matrículas no mesmo imóvel têm as economias somadas e as categorias combinadas (mais de uma = misto).

| Situação                     | Significa                                                                    |
| ---------------------------- | ---------------------------------------------------------------------------- |
| `divergencia_confirmada`     | Classificação revisada + associação confirmada, e categoria ou unidades diferem |
| `sem_matricula_confirmada`   | Revisor confirmou a classificação e que nenhuma matrícula corresponde        |
| `possivel_divergencia`       | Difere, mas a classificação ou a associação ainda é automática               |
| `sem_correspondencia`        | Localização confiável e nenhuma ligação perto: verificar (não é alerta de ligação clandestina) |
| `associacao_inconclusiva`    | Localização só pela câmera: posicione a fachada no mapa                      |
| `classificacao_inconclusiva` | O Bonsai (ou o revisor) não conseguiu decidir o uso                          |
| `ok`, `nao_classificado`, `descartado` | —                                                                  |

`tipo_divergencia`: `categoria`, `economias` (mais unidades que economias) ou `categoria+economias`. A categoria
`institucional` corresponde à categoria Pública do GSAN; instituições privadas cadastradas como comerciais aparecem
como possível divergência para o revisor decidir.

Na Análise comercial, "Confirmar divergência" exige a classificação revisada e as matrículas confirmadas (ou
"nenhuma corresponde"). Toda alteração manual (revisão, caixa, localização, associação, decisão, anotação) fica na
tabela `historico`.

### Formato do CSV do GSAN

Separador `;` ou `,`, UTF-8 com ou sem BOM (`--encoding latin-1` se preciso). Linhas sem coordenada são ignoradas e
listadas. O GeoFAT só lê esse arquivo; o GSAN não é alterado.

```
matricula;latitude;longitude;endereco;economias_residencial;economias_comercial;economias_industrial;economias_publica
123456;-2.5301;-44.2803;Rua A, 10;1;1;0;0
```

`exemplos/gsan_FICTICIO_teste.csv` é um GSAN **fictício** (matrículas `FICT-...`) feito para o teste com as fotos do
Centro Histórico; serve só para demonstrar a comparação. Para removê-lo do banco:
`DELETE FROM gsan_ligacao WHERE matricula LIKE 'FICT-%'`.

## IA modular: etapas, modelos e provedores

Cada etapa usa o modelo escolhido na página Modelos, sem mudar código (`geofat/provedores.py`):

| Etapa                       | Tipos de modelo que fazem a etapa                                     | Padrão              |
| --------------------------- | --------------------------------------------------------------------- | ------------------- |
| Encontrar a fachada         | Grounding DINO, YOLO treinado, Roboflow                               | Grounding DINO tiny |
| Sugerir anotações (editor)  | Grounding DINO (busca por texto), YOLO e Roboflow (classes do modelo) | Grounding DINO tiny |
| Ler letreiros (OCR)         | Modelo de visão (Bonsai etc.), PaddleOCR                              | não usar            |
| Classificar o imóvel        | Modelo de visão por API compatível com a da OpenAI                    | Bonsai 2 9B local   |

Conexões (Modelos › Conexões): Bonsai local, Ollama, OpenRouter, Roboflow e Hugging Face (token opcional para
download). As chaves ficam no banco e nunca voltam para o navegador. Antes de classificar, o GeoFAT confere em
`/models` se o servidor está servindo o modelo escolhido, para não gravar o resultado de um modelo com o nome de
outro. Recortes enviados para a nuvem são JPEG recodificados, sem EXIF (sem GPS). O padrão continua 100% local.

Catálogo (Modelos › Catálogo): Bonsai 2 9B e 4B (GGUF), Grounding DINO tiny e base, YOLO11 n/s/m. O download vai
para `modelos/`, com progresso em bytes, retomada de onde parou e conferência do SHA-256 publicado no Hugging Face.

## Tarefas em segundo plano

Detecção, classificação, importações, downloads, sugestões e treinos rodam na fila (tabela `tarefa`), sem travar a
interface. Três filas: processamento (uma tarefa por vez, CPU/GPU), downloads/importações e interativa (sugestão no
editor). O progresso é o que a tarefa mede (fotos, fachadas, bytes, lotes de treino); sem total conhecido, a barra
fica indeterminada. Cancelar: na fila, na hora; em execução, no próximo ponto de verificação (entre fotos, blocos de
1 MB ou lotes de treino). Uma tarefa interrompida por reinício do servidor fica como falha e pode ser repetida.

## Datasets e treino YOLO

No Treinamento, crie um dataset (vazio, todas as fotos ou as fachadas já revisadas) com as classes desejadas e abra
o editor. Lá: envie imagens (recodificadas sem EXIF) ou escolha fotos do GeoFAT; desenhe e ajuste caixas (B, V, 1–9,
Del); peça sugestões ao detector (entram tracejadas e precisam ser aceitas); aprove cada imagem (sem caixas = imagem
de fundo); ajuste treino/validação/teste (padrão 70/20/10). As caixas são salvas automaticamente.

"Exportar YOLO" gera `data/exportacoes/<dataset>_yolo.zip` só com as imagens aprovadas. "Novo treinamento" roda o
Ultralytics no CPU (base do catálogo ou versão anterior), e as métricas de validação de cada época vão para o banco.
"Usar na detecção" registra a versão como modelo YOLO, que passa a aparecer nas etapas.

Pela linha de comando continua valendo o fluxo antigo, só com as fachadas revisadas:

```bash
.venv\Scripts\python -m geofat exportar-yolo data/yolo
```

Um detector útil precisa de algumas centenas de fotos revisadas; com dezenas, o Grounding DINO continua melhor.

### Mapillary e pré-rotulagem automática

Treinamento > **Pré-rotulagem automática**:

1. Cole o Client Token do Mapillary (mapillary.com/dashboard/developers) e teste. O token fica só no servidor
   (conexão "Mapillary", também em Modelos > Conexões) e vai no cabeçalho das chamadas, nunca na URL.
2. Posicione o mapa (São Luís por padrão) e busque: é uma consulta real, sem download. Confirme as condições de
   licença (CC BY-SA 4.0 e Termos de Uso) e baixe para um dataset. As imagens vão sem alteração para
   `data/fotos/mapillary/`, viram fotos do GeoFAT (posição, direção e sequência) e a procedência e a licença ficam
   em `foto_procedencia`. A verificação da licença fica no histórico, e a exportação YOLO inclui um `ATRIBUICAO.txt`.
3. Pré-rotule: detector e verificador (qualquer VLM de Modelos), número de tentativas (no máximo 5), critérios de
   aceite, revisão humana opcional e amostra de avaliação. Por imagem: o detector propõe caixas; o verificador julga
   cada recorte e conta, na imagem inteira com as caixas desenhadas, as fachadas que ficaram sem caixa. Se houver
   fachada sem caixa, o detector roda de novo com limiar menor; se houver caixa errada, com limiar maior. As imagens
   aceitas entram aprovadas (`aprovacao = 'automatica'`), e as demais vão para o editor como sugestões.

A aprovação do verificador **não** mede precisão. A amostra sorteada antes do processamento vai para o editor sem
sugestões. Quando uma pessoa a aprova, o painel compara as caixas automáticas com as dela (precisão e revocação,
IoU ≥ 0,5). Cada tentativa (parâmetros, caixas, respostas do verificador) fica em `verificacao`, e cada versão das
caixas em `anotacao_versao`. Cancelar não perde o que já foi processado, e "Retomar" continua das imagens que
faltam.

### Análise direcionada (planilha de imóveis)

Processamentos > **Análise direcionada**. Procura imóveis-alvo de uma planilha (`.xlsx` ou `.csv`, como
`exemplos/Imoveis Testes.xlsx`) nas fotos do Mapillary e passa só essas fotos pelo pipeline.

1. **Alvos.** A planilha é lida em fluxo (os 128 mil registros do exemplo levam ~10 s). Dela saem só **Matricula**,
   **Coordenada X/Y** (ou Longitude/Latitude), **Nome Localidade** e **Municipio**. Categoria, CPF/CNPJ, débito e
   faturamento são descartados na leitura e não vão para o banco nem para a IA. Ficam `quantidade` alvos (até 50 por
   vez): as matrículas pedidas, ou um sorteio reprodutível (semente), opcionalmente de uma localidade, só entre os que
   têm imagem do Mapillary a até 60 m. A cópia enviada é apagada depois da leitura.
2. **Polígonos.** Alvo sem polígono do Open Buildings a 150 m: o GeoFAT importa Open Buildings e OSM da área (o
   arquivo da célula S2, ~73 MB em São Luís, fica em `data/cache/open_buildings/`). Fora do Centro o OSM quase não tem
   edificações. A edificação do alvo é a que contém a coordenada ou a mais próxima até 15 m, marcada como **ambígua**
   se houver outra perto.
3. **Fotos candidatas.** Todas as imagens do Mapillary no raio são avaliadas **antes** do download: distância, direção
   e campo de visão da câmera (projeção olho de peixe do Mapillary quando informada) contra o contorno do alvo, e se
   outra edificação corta a linha até ele. Só entram `no_campo`, `na_borda` (dentro da margem de 15° da bússola) e
   `obstruido`, nessa ordem, até `fotos por alvo` (padrão 3), sem repetir o mesmo ponto de vista. Foto perto do alvo
   não basta: `fora_do_campo`, `longe`, `sem_direcao` e `camera_no_alvo` (GPS dentro do polígono) não são baixadas.
4. **Detecção** (etapa "Encontrar a fachada"). Na primeira vez a foto entra no pipeline normal (Revisão e mapa).
5. **Associação.** Para cada caixa, a linha de visada no PostGIS (a mesma lógica de `localizar`). **Provável**: a
   primeira edificação atingida é a do alvo. **Possível**: a do alvo está só no setor de visão (ou, sem polígono, a
   caixa cobre a coluna prevista para o alvo). Fica a melhor caixa de cada foto.
6. **Bonsai** (etapa "Classificar o imóvel"), só na caixa associada: primeiro confere se o recorte é **uma** fachada
   (caixa com várias casas vira `caixa_recusada` e não é classificada), depois classifica o uso. A classificação vai
   para a observação só se ela ainda não tinha uma.
7. **Resultado** por alvo: observado (provável), talvez observado, não observado ou sem foto útil. No mapa, camada
   "Alvos da análise direcionada"; na Revisão, a observação mostra o número do alvo (sem a matrícula).

Cada execução guarda caixas, associação, respostas do verificador, classificação e tempo de cada etapa em
`analise_foto`, sem sobrescrever a anterior. Por isso duas execuções dos mesmos alvos (por exemplo Local × Runpod,
com "reprocessar") podem ser comparadas lado a lado. Alvos já concluídos no mesmo ambiente não são refeitos, e uma
foto já processada para outro alvo no mesmo ambiente reaproveita a detecção e a resposta do modelo para a mesma caixa
(salvo com "reprocessar").
Cancelar não perde nada, e "Retomar" continua do que faltou. Limites por execução: tempo máximo e, no Runpod,
orçamento.

## Processamento na GPU remota (Runpod)

`gpu/` tem um serviço independente (FastAPI) para o Grounding DINO, o Bonsai (llama.cpp com CUDA) e o PaddleOCR
opcional. Já vem com o `ultralytics` instalado para o treino YOLO futuro, que ainda não tem rota. O banco PostGIS
continua neste computador. O serviço recebe uma imagem por vez (JPEG recodificado, sem EXIF/GPS) com os parâmetros da
inferência. Não grava nada em disco, não registra o conteúdo das requisições e não sabe a que imóvel a imagem se
refere: matrícula, planilha e dados comerciais não saem daqui.

**Imagem Docker** (multi-stage: compila o llama-server b11846 com CUDA; PyTorch 2.14.1 + CUDA 12.6; mesmas versões
de `requirements-ia.txt`):

```
docker build -t SEU_USUARIO/geofat-gpu:1 gpu/
docker push SEU_USUARIO/geofat-gpu:1
```

**GPU Pod** (Runpod > Pods > Deploy, ou um template com estes campos):

| Campo | Valor |
|-------|-------|
| Container image | `SEU_USUARIO/geofat-gpu:1` |
| GPU | 16 GB de VRAM sobram (ex.: RTX A4000/4000 Ada); com 8 GB use `CARGA_SEQUENCIAL=1` |
| Volume | 20 GB em `/workspace` (modelos e cache do Hugging Face sobrevivem ao desligar) |
| HTTP port | `8000` |
| Variáveis | `GEOFAT_GPU_TOKEN` = token longo (`python -c "import secrets; print(secrets.token_urlsafe(32))"`) |
| Opcionais | `CARGA_SEQUENCIAL=1`, `VLM_GGUF`, `VLM_MMPROJ`, `VLM_ALIAS` (padrão: Bonsai 2 9B) |

Copie para `/workspace/modelos/` os dois GGUF do Bonsai, `Ternary-Bonsai-2-9B-Q2_0.gguf` e
`Ternary-Bonsai-2-9B-mmproj-Q8_0.gguf` (pelo terminal web do pod, com `huggingface-cli download
prism-ml/Ternary-Bonsai-2-9B-gguf --local-dir /workspace/modelos`, ou por `runpodctl send`). O Grounding DINO baixa
sozinho do Hugging Face na primeira detecção, para `/workspace/hf`.

**No GeoFAT** (Modelos > Ambiente de processamento): endereço `https://POD-ID-8000.proxy.runpod.net`, o mesmo token,
**Testar conexão** (mostra GPU e VRAM) e **Status do serviço** (modelos carregados, fila, duração e erros dos últimos
trabalhos). Escolha **Runpod GPU** e salve. Daí em diante, detecção, classificação, OCR e a análise direcionada usam
o pod; YOLO e conexões de nuvem não mudam. Cada chamada tenta até 3 vezes em falha de rede, tempo esgotado ou HTTP 5xx
(token errado não é repetido); depois a tarefa para com a mensagem e pode ser retomada. Com o pod desligado, volte
para **Local**: nada mais depende dele. O proxy HTTP do Runpod corta requisições acima de ~100 s. Para trabalhos mais
longos, use a porta TCP exposta do pod (endereço `http://IP:PORTA`).

**Custo.** Informe o US$/hora do pod e um orçamento. A análise direcionada para de enviar trabalho quando o tempo de
processamento somado × custo atinge o orçamento. É uma estimativa: o Runpod cobra o tempo em que o pod fica ligado,
inclusive ocioso. Desligue o pod ao terminar.

**Diferenças esperadas entre ambientes.** O mesmo GGUF e o mesmo modelo do Hugging Face rodam nos dois ambientes, mas
CUDA e CPU/Vulkan não dão resultados idênticos bit a bit, e o Bonsai usa amostragem (temperatura 0,7). A categoria
pode mudar entre duas execuções até no mesmo computador. O detector local recebe a foto inteira e o remoto, o mesmo
JPEG recodificado. Use "comparar" na análise direcionada para ver caixas coincidentes (IoU ≥ 0,5), associação,
categoria e tempo por foto.

**Processar a planilha inteira, depois.** Meça antes: rode os mesmos alvos no Runpod (reprocessar), compare, e use
o tempo médio por foto e por alvo que o painel mostra ("Medido: …") para estimar o total. Depois, lotes de até 50
alvos por execução (o sorteio com a mesma semente e a lista de matrículas evitam repetir; alvos já concluídos no
ambiente são pulados), com tempo máximo e orçamento. O processamento automático da planilha inteira não existe de
propósito.

## Edificações, observações e trajetórias

Cada caixa numa foto é uma **observação** (`imovel`; a caixa original do detector fica em `bbox_detectada`). A posição
da **câmera** é `foto.geom`; a **fachada** estimada é `imovel.geom`; a **edificação** é o contorno da construção
(OSM/Open Buildings); o **lote** é o limite do terreno (tabela `lote`, vazia até haver uma fonte, carregue pelo QGIS);
**unidades** são as possíveis unidades consumidoras registradas dentro de uma edificação.

`localizar` faz, nesta ordem:

1. **Trajetórias** (`foto_trajeto`): fotos da mesma pasta com até 2 min e 150 m entre si formam um percurso (ou o
   `trajeto` informado). O rumo vem do metadado ou das fotos vizinhas e só é confiável com deslocamento ≥ 8 m (ou 2×
   o erro do GPS) e sem curva > 45°. O lado da via vem do CSV ou da direção da câmera em relação ao rumo.
2. **Candidatas** (`candidato_edificacao`): setor de visão no PostGIS (largura da caixa + 15° de erro da bússola,
   até 60 m). A coluna da caixa vira ângulo pelo modelo pinhole, ou pela projeção olho de peixe quando a foto tem
   `camera` (Mapillary: as câmeras veiculares de São Luís cobrem ~130° na horizontal, e não os ~72° do padrão de
   celular). Sem bússola, o eixo é rumo ± 90° pelo lado da via, **só com rumo confiável** (+30° de folga, confiança
   baixa). Com rumo e lado conhecidos, edificações do outro lado da via saem. Evidências por candidata: distância,
   desvio da linha de visada, se é a primeira atingida, lado, polígono do Open Buildings com score < 0,8. A pontuação
   só ordena; não é probabilidade. A sugestão é a candidata atingida pela linha de visada; sem nenhuma, a fachada fica
   na câmera (pendente) e as candidatas do setor ficam para o revisor.
3. **Grupos sugeridos** (`grupo`, `multivisual.py`): observações de fotos diferentes, na mesma edificação, com
   fachadas a até 10 m umas das outras (todas com todas). Duas caixas da mesma foto nunca são agrupadas
   automaticamente. Grupos do revisor (reunir, separar, confirmar) não são refeitos.

O módulo multivisual recebe evidências como pares de observações (`FONTES`); correspondência visual, OCR e
continuidade arquitetônica entram ali quando houver fotos reais para validar. Hoje só existe a evidência geográfica.

**Endereços** (`leitura_endereco`): "Ler números de endereço" (Processamentos) roda o OCR da etapa escolhida em
Modelos (qualquer provedor) no recorte de cada fachada, extrai números (ignora telefones, datas, preços) e procura o
mesmo número no CNEFE/OSM a até 30 m da edificação. Toda leitura do OCR entra pendente; o número digitado pelo revisor
entra confirmado. A região guardada é o recorte da fachada (os adaptadores atuais não informam a posição do texto).

## QGIS

Camada → Adicionar camada PostGIS: host `localhost`, porta `5433`, banco/usuário/senha `geofat`. Use a view
`imovel_analise` (estilize por `situacao`, `categoria_final` ou `localizacao_confianca`), `edificacao`, `gsan_ligacao`.
Lotes de terreno (ex.: cadastro da prefeitura) podem ser carregados na tabela `lote` (MultiPolygon, EPSG:4326).

## Testes

```bash
.venv\Scripts\python -m pytest -q tests
```

Os testes de banco usam o PostGIS do `docker compose` num schema temporário (`teste`), sem tocar nos dados.

## Atualizar o banco

As mudanças de banco ficam em `db/migracoes/` e são aplicadas na subida da API (ou com o comando abaixo), sem apagar
dados. Faça um backup antes: `docker exec projeto_caema-db-1 pg_dump -U geofat geofat > backup.sql`.

```bash
.venv\Scripts\python -m geofat migrar
```

## Limitações conhecidas

- Sem direção da câmera no EXIF, a fachada fica na posição da câmera (confiança baixa) até o revisor posicioná-la,
  salvo com lado da via informado e rumo confiável (sugestão de confiança baixa).
- Trajetórias, lado da via, grupos de observações e leitura de números foram testados só com coordenadas sintéticas;
  as 14 fotos atuais não formam percursos (fotos isoladas, minutos de intervalo). Os limiares (2 min, 8 m, 45°, 10 m,
  30 m) são escolhas iniciais e precisam ser calibrados com fotos próprias com GPS e lado da via.
- Os grupos sugeridos usam só geometria: uma loja vizinha no mesmo polígono, a menos de 10 m, pode ser agrupada por
  engano. A comparação visual entre fotos não está implementada.
- Erro de GPS do celular em ruas estreitas (5–15 m) pode levar o raio à edificação errada: por isso a sugestão é
  "média" e a confirmação é do revisor.
- O Grounding DINO marca a foto inteira como fachada em fotos de detalhe (porta, interior): o Bonsai tende a responder
  `inconclusivo`, e o revisor descarta.
- Bonsai no notebook (i7-1255U, Iris Xe): ~2,5–3 min por fachada (Vulkan), ~7 min só CPU. Grounding DINO: ~30 s/foto.
- O Bonsai acerta bem quando há letreiro/placa e marca fotos de detalhe como `inconclusivo`; sem letreiro, prédios
  institucionais com cara de sobrado saem como residenciais. Por isso toda divergência começa como "possível".
- Contornos OSM só de ways (multipolígonos ficam de fora). Fotos HEIC não são lidas.
- Sem login: rode só na rede interna. Fotos de rua podem mostrar pessoas e placas (LGPD): trate `data/` como restrito.
- OCR pelo Bonsai: no teste respondeu "NENHUM" numa fachada cujo letreiro ele lê na classificação; por isso o OCR
  começa desligado. PaddleOCR, Roboflow e OpenRouter têm adaptador, mas não foram testados (sem pacote ou chave).
- Treino só no CPU (torch CPU). Não há exclusão de dataset nem de treino pela interface.
- Fotos de terceiros (ex.: Wikimedia Commons) exigem crédito e licença: use a coluna `credito` do `coordenadas.csv`,
  que aparece no mapa.
