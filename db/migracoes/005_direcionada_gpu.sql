-- Análise direcionada (imóveis-alvo de uma planilha) e processamento em GPU remota (Runpod). Só acrescenta
-- tabelas, uma conexão e uma configuração; nenhum registro existente é alterado.

ALTER TABLE provedor DROP CONSTRAINT provedor_tipo_check;
ALTER TABLE provedor ADD CONSTRAINT provedor_tipo_check
  CHECK (tipo IN ('openai', 'roboflow', 'huggingface', 'mapillary', 'gpu_remota'));
INSERT INTO provedor (nome, tipo, url, local) VALUES ('Runpod GPU', 'gpu_remota', 'https://POD-ID-8000.proxy.runpod.net', false)
  ON CONFLICT DO NOTHING;

-- Projeção da câmera quando a fonte informa ({"tipo": "fisheye"|"perspective", "parametros": [f, k1, k2]}, padrão
-- OpenSfM/Mapillary, focal relativa ao maior lado). NULL = modelo pinhole pelo fov_horizontal, como antes.
ALTER TABLE foto ADD COLUMN camera jsonb;

CREATE TABLE configuracao (
  chave text PRIMARY KEY,
  valor jsonb NOT NULL,
  alterada_em timestamptz NOT NULL DEFAULT now()
);
-- ambiente: 'local' ou 'runpod'. Custo e orçamento valem para o Runpod (US$); nulos = sem limite.
INSERT INTO configuracao (chave, valor) VALUES
  ('processamento', '{"ambiente": "local", "custo_hora_usd": null, "orcamento_usd": null}');

-- Imóvel a procurar nas fotos. Da planilha só entram os campos que localizam o imóvel: nada de categoria,
-- documento, débito ou situação de faturamento.
CREATE TABLE alvo (
  id serial PRIMARY KEY,
  matricula text NOT NULL UNIQUE,
  origem text NOT NULL,                  -- nome do arquivo de onde veio
  linha int,
  localidade text,
  municipio text,
  geom geometry(Point, 4326) NOT NULL,
  criado_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON alvo USING gist (geom);

CREATE TABLE analise (
  id serial PRIMARY KEY,
  ambiente text NOT NULL CHECK (ambiente IN ('local', 'runpod')),
  config jsonb NOT NULL,
  alvo_ids int[] NOT NULL,
  gasto_s real NOT NULL DEFAULT 0,       -- tempo de processamento somado entre retomadas (base do orçamento)
  criada_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE analise_alvo (
  analise_id int NOT NULL REFERENCES analise ON DELETE CASCADE,
  alvo_id int NOT NULL REFERENCES alvo ON DELETE CASCADE,
  estado text NOT NULL DEFAULT 'pendente'
    CHECK (estado IN ('pendente', 'fotos', 'concluido', 'sem_foto', 'erro')),
  edificacao_id int REFERENCES edificacao ON DELETE SET NULL,
  edificacao_evidencias jsonb,
  buscadas int,                          -- imagens do Mapillary no raio
  resultado jsonb,
  erro text,
  atualizado_em timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (analise_id, alvo_id)
);

-- Uma foto candidata de um alvo numa análise. Os resultados de cada etapa ficam aqui (inclusive quando a foto já
-- tinha passado pela detecção), para comparar execuções e ambientes sem sobrescrever nada.
CREATE TABLE analise_foto (
  analise_id int NOT NULL,
  alvo_id int NOT NULL,
  foto_id int NOT NULL REFERENCES foto ON DELETE CASCADE,
  etapa text NOT NULL DEFAULT 'avaliada' CHECK (etapa IN ('avaliada', 'detectada', 'associada', 'concluida')),
  avaliacao jsonb NOT NULL,              -- distância, ângulo, campo de visão, obstrução, colunas previstas
  deteccao jsonb,
  associacao text CHECK (associacao IN ('provavel', 'possivel', 'nao_observado', 'caixa_recusada')),
  bbox int[],
  imovel_id int REFERENCES imovel ON DELETE SET NULL,
  verificacao jsonb,
  classificacao jsonb,
  tempos jsonb NOT NULL DEFAULT '{}',
  erro text,
  PRIMARY KEY (analise_id, alvo_id, foto_id),
  FOREIGN KEY (analise_id, alvo_id) REFERENCES analise_alvo ON DELETE CASCADE
);
CREATE INDEX ON analise_foto (imovel_id);
