-- Plataforma web: fila de tarefas, modelos de IA configuráveis, datasets de anotação, treinos,
-- decisões da análise comercial e histórico das alterações manuais. Não altera tabelas existentes,
-- exceto a coluna nova imovel.texto_ocr.

-- Tarefas em segundo plano. Progresso real: total NULL = indeterminado (não se inventa percentual).
CREATE TABLE tarefa (
  id serial PRIMARY KEY,
  tipo text NOT NULL,
  fila text NOT NULL,                    -- 'computacao' (uma por vez), 'rede' (downloads/importações), 'interativa'
  titulo text NOT NULL,
  parametros jsonb NOT NULL DEFAULT '{}',
  estado text NOT NULL DEFAULT 'fila'
    CHECK (estado IN ('fila', 'executando', 'concluida', 'falhou', 'cancelada')),
  atual bigint,
  total bigint,
  unidade text,
  mensagem text,
  resultado jsonb,
  arquivo text,                          -- arquivo gerado para download, relativo a data/
  log jsonb NOT NULL DEFAULT '[]',
  cancelar boolean NOT NULL DEFAULT false,
  repete int REFERENCES tarefa ON DELETE SET NULL,
  criada_em timestamptz NOT NULL DEFAULT now(),
  iniciada_em timestamptz,
  terminada_em timestamptz
);
CREATE INDEX ON tarefa (fila, estado, id);

-- Conexões com servidores de modelos. A chave nunca sai pela API.
CREATE TABLE provedor (
  id serial PRIMARY KEY,
  nome text NOT NULL UNIQUE,
  tipo text NOT NULL CHECK (tipo IN ('openai', 'roboflow', 'huggingface')),
  url text NOT NULL,
  local boolean NOT NULL,                -- false = as imagens saem do computador
  chave text,
  testado_em timestamptz,
  teste_ok boolean,
  teste_mensagem text
);

-- Modelos configurados; o que cada tipo sabe fazer está em geofat/provedores.py (TIPOS).
CREATE TABLE modelo (
  id serial PRIMARY KEY,
  nome text NOT NULL UNIQUE,
  tipo text NOT NULL CHECK (tipo IN ('grounding_dino', 'yolo', 'roboflow', 'vlm', 'paddleocr')),
  provedor_id int REFERENCES provedor ON DELETE SET NULL,
  config jsonb NOT NULL DEFAULT '{}',
  criado_em timestamptz NOT NULL DEFAULT now()
);

-- Qual modelo faz cada etapa. ocr sem modelo = não usar.
CREATE TABLE etapa (
  etapa text PRIMARY KEY CHECK (etapa IN ('deteccao', 'anotacao', 'ocr', 'classificacao')),
  modelo_id int REFERENCES modelo ON DELETE SET NULL,
  alterada_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dataset (
  id serial PRIMARY KEY,
  nome text NOT NULL UNIQUE CHECK (nome ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
  classes text[] NOT NULL,               -- a posição é o índice da classe no YOLO
  textos_busca jsonb NOT NULL DEFAULT '{}',  -- classe -> texto para detectores por descrição
  criado_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dataset_imagem (
  id serial PRIMARY KEY,
  dataset_id int NOT NULL REFERENCES dataset ON DELETE CASCADE,
  foto_id int REFERENCES foto ON DELETE SET NULL,
  arquivo text NOT NULL,                 -- relativo a FOTOS_DIR
  nome text NOT NULL,
  largura int NOT NULL,
  altura int NOT NULL,
  conjunto text NOT NULL CHECK (conjunto IN ('treino', 'validacao', 'teste')),
  estado text NOT NULL DEFAULT 'vazia' CHECK (estado IN ('vazia', 'revisar', 'aprovada')),
  atualizada_em timestamptz NOT NULL DEFAULT now(),
  UNIQUE (dataset_id, arquivo)
);

CREATE TABLE anotacao (
  id serial PRIMARY KEY,
  imagem_id int NOT NULL REFERENCES dataset_imagem ON DELETE CASCADE,
  classe text NOT NULL,
  bbox int[] NOT NULL CHECK (cardinality(bbox) = 4),
  origem text NOT NULL CHECK (origem IN ('manual', 'sugestao', 'aceita')),
  score real,
  modelo text
);
CREATE INDEX ON anotacao (imagem_id);

CREATE TABLE treino (
  id serial PRIMARY KEY,
  nome text NOT NULL UNIQUE,
  dataset_id int REFERENCES dataset ON DELETE SET NULL,
  tarefa_id int REFERENCES tarefa ON DELETE SET NULL,
  parametros jsonb NOT NULL,
  classes text[] NOT NULL,
  imagens jsonb,                         -- quantas imagens em cada conjunto
  metricas jsonb NOT NULL DEFAULT '[]',  -- uma entrada por época, medida pelo Ultralytics
  pesos text,                            -- best.pt, relativo à raiz do projeto
  criado_em timestamptz NOT NULL DEFAULT now()
);

-- Decisão do analista sobre a divergência. Fica no GeoFAT; o GSAN não é alterado.
CREATE TABLE decisao_comercial (
  imovel_id int PRIMARY KEY REFERENCES imovel ON DELETE CASCADE,
  decisao text NOT NULL CHECK (decisao IN ('confirmada', 'vistoria', 'descartada')),
  observacao text,
  decidida_em timestamptz NOT NULL DEFAULT now()
);

-- Histórico das alterações manuais (revisões, caixas, localização, associação, anotações, decisões).
CREATE TABLE historico (
  id bigserial PRIMARY KEY,
  entidade text NOT NULL,
  entidade_id int NOT NULL,
  acao text NOT NULL,
  dados jsonb,
  em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON historico (entidade, entidade_id);

ALTER TABLE imovel ADD COLUMN texto_ocr text;
