-- Imagens do Mapillary e pré-rotulagem automática (detector + verificação por VLM). Só acrescenta tabelas e
-- colunas; nenhum registro existente é alterado.

ALTER TABLE provedor DROP CONSTRAINT provedor_tipo_check;
ALTER TABLE provedor ADD CONSTRAINT provedor_tipo_check CHECK (tipo IN ('openai', 'roboflow', 'huggingface', 'mapillary'));
INSERT INTO provedor (nome, tipo, url, local) VALUES ('Mapillary', 'mapillary', 'https://graph.mapillary.com', false)
  ON CONFLICT DO NOTHING;

-- De onde veio cada foto externa e sob qual licença. O arquivo baixado é guardado sem alteração.
CREATE TABLE foto_procedencia (
  foto_id int PRIMARY KEY REFERENCES foto ON DELETE CASCADE,
  fonte text NOT NULL,                   -- 'mapillary'
  id_externo text NOT NULL,
  autor text,
  sequencia_externa text,
  licenca text NOT NULL,                 -- ex.: 'CC BY-SA 4.0'
  termos_url text NOT NULL,
  termos_verificados_em timestamptz NOT NULL,
  url_pagina text,
  dados jsonb NOT NULL,                  -- resposta da API para a imagem (posição, orientação, câmera)
  obtida_em timestamptz NOT NULL DEFAULT now(),
  UNIQUE (fonte, id_externo)
);

-- Quem aprovou a imagem do dataset: pessoa no editor ou a pré-rotulagem (que não comprova precisão)
ALTER TABLE dataset_imagem ADD COLUMN aprovacao text CHECK (aprovacao IN ('humana', 'automatica'));
UPDATE dataset_imagem SET aprovacao = 'humana' WHERE estado = 'aprovada';
-- Amostra sorteada antes do processamento: sempre vai à revisão humana, para medir o resultado automático
ALTER TABLE dataset_imagem ADD COLUMN amostra_avaliacao boolean NOT NULL DEFAULT false;

-- Cada versão das caixas de uma imagem (manual, sugestão do detector ou pré-rotulagem)
CREATE TABLE anotacao_versao (
  id bigserial PRIMARY KEY,
  imagem_id int NOT NULL REFERENCES dataset_imagem ON DELETE CASCADE,
  origem text NOT NULL CHECK (origem IN ('manual', 'sugestao', 'prerotulagem')),
  estado text NOT NULL,
  anotacoes jsonb NOT NULL,
  em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON anotacao_versao (imagem_id);

CREATE TABLE prerotulagem (
  id serial PRIMARY KEY,
  dataset_id int NOT NULL REFERENCES dataset ON DELETE CASCADE,
  tarefa_id int REFERENCES tarefa ON DELETE SET NULL,
  config jsonb NOT NULL,                 -- modelos, tentativas, critérios, revisão humana, amostra
  imagem_ids int[] NOT NULL,
  criada_em timestamptz NOT NULL DEFAULT now()
);

-- Resultado final por imagem; ausente = ainda não processada (a tarefa retoma daí)
CREATE TABLE prerotulo (
  prerotulagem_id int NOT NULL REFERENCES prerotulagem ON DELETE CASCADE,
  imagem_id int NOT NULL REFERENCES dataset_imagem ON DELETE CASCADE,
  decisao text NOT NULL CHECK (decisao IN ('aceita', 'revisar', 'rejeitada')),
  tentativas int NOT NULL,
  motivo text,
  caixas jsonb NOT NULL,                 -- caixas que a pré-rotulagem aceitou (para comparar com a revisão humana)
  em timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (prerotulagem_id, imagem_id)
);

-- Cada tentativa: parâmetros do detector, caixas propostas e o que o verificador respondeu
CREATE TABLE verificacao (
  id bigserial PRIMARY KEY,
  prerotulagem_id int NOT NULL REFERENCES prerotulagem ON DELETE CASCADE,
  imagem_id int NOT NULL REFERENCES dataset_imagem ON DELETE CASCADE,
  tentativa int NOT NULL,
  parametros jsonb NOT NULL,
  caixas jsonb NOT NULL,
  vereditos jsonb NOT NULL,
  omitidas int,
  resposta_omitidas text,
  problema text,
  detector text NOT NULL,
  verificador text NOT NULL,
  em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON verificacao (prerotulagem_id, imagem_id);
