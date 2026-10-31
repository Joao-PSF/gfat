-- Edificações por polígono, trajetórias de captura, várias observações do mesmo imóvel, unidades e leituras
-- de endereço. Só acrescenta tabelas e colunas: fotos, detecções (imovel), revisões e histórico ficam como estão.
--
-- Vocabulário: foto.geom = posição da CÂMERA; imovel = uma OBSERVAÇÃO de fachada (uma caixa numa foto, com a
-- caixa original do detector preservada); edificacao = contorno da construção (OSM/Open Buildings);
-- lote = limite do terreno (não é o contorno da construção); unidade = possível unidade consumidora dentro
-- de uma edificação; grupo = observações, em fotos diferentes, que mostram o mesmo imóvel.

-- Metadados de captura informados (EXIF ou coordenadas.csv). NULL = desconhecido.
ALTER TABLE foto
  ADD COLUMN rumo real,                  -- sentido do deslocamento (EXIF GPSTrack), graus do norte verdadeiro
  ADD COLUMN lado_via text CHECK (lado_via IN ('direito', 'esquerdo')),  -- lado da via que a câmera fotografa
  ADD COLUMN trajeto text,               -- identificador do percurso, quando informado
  ADD COLUMN sequencia int,              -- ordem da foto no percurso, quando informada
  ADD COLUMN precisao_m real;            -- erro horizontal do GPS (EXIF GPSHPositioningError)

-- Calculado pelas fotos (geofat/trajetorias.py), refeito a cada 'localizar'. O rumo só é usado quando confiável.
CREATE TABLE foto_trajeto (
  foto_id int PRIMARY KEY REFERENCES foto ON DELETE CASCADE,
  trajeto text NOT NULL,
  ordem int NOT NULL,
  rumo real,
  rumo_fonte text CHECK (rumo_fonte IN ('informado', 'trajetoria')),
  rumo_confiavel boolean NOT NULL DEFAULT false,
  lado text CHECK (lado IN ('direito', 'esquerdo')),
  lado_fonte text CHECK (lado_fonte IN ('informado', 'direcao')),
  evidencias jsonb NOT NULL DEFAULT '{}'
);

-- Limites de terreno (ex.: cadastro da prefeitura, carregado pelo QGIS). Vazio enquanto não houver fonte.
CREATE TABLE lote (
  id serial PRIMARY KEY,
  fonte text NOT NULL,
  id_origem text NOT NULL,
  geom geometry(MultiPolygon, 4326) NOT NULL,
  UNIQUE (fonte, id_origem)
);
CREATE INDEX ON lote USING gist (geom);

-- Todas as edificações que podem corresponder a uma observação, com as evidências geográficas.
-- A escolhida fica em imovel.edificacao_id (sugerida, ou manual quando localizacao_fonte = 'manual').
CREATE TABLE candidato_edificacao (
  imovel_id int REFERENCES imovel ON DELETE CASCADE,
  edificacao_id int REFERENCES edificacao ON DELETE CASCADE,
  pontuacao real NOT NULL,               -- só ordena os candidatos; não é probabilidade
  evidencias jsonb NOT NULL,
  PRIMARY KEY (imovel_id, edificacao_id)
);
CREATE INDEX ON candidato_edificacao (edificacao_id);

-- Observações do mesmo imóvel. 'sugerido' é refeito pela associação multivisual; 'manual' nunca é tocado por ela.
CREATE TABLE grupo (
  id serial PRIMARY KEY,
  origem text NOT NULL CHECK (origem IN ('sugerido', 'manual')),
  evidencias jsonb,
  criado_em timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE imovel
  ADD COLUMN grupo_id int REFERENCES grupo ON DELETE SET NULL,
  ADD COLUMN localizacao_evidencias jsonb,
  DROP CONSTRAINT imovel_localizacao_fonte_check,
  ADD CONSTRAINT imovel_localizacao_fonte_check CHECK (localizacao_fonte IN ('camera', 'direcao', 'lado_via', 'manual'));
CREATE INDEX ON imovel (grupo_id);
CREATE INDEX ON imovel (edificacao_id);

-- Unidades independentes registradas pelo revisor dentro de uma edificação (lojas, casas, apartamentos).
CREATE TABLE unidade (
  id serial PRIMARY KEY,
  edificacao_id int NOT NULL REFERENCES edificacao ON DELETE CASCADE,
  grupo_id int REFERENCES grupo ON DELETE SET NULL,
  rotulo text NOT NULL,
  categoria categoria_uso,
  numero text,
  criada_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON unidade (edificacao_id);

-- Número do endereço em dados públicos, normalizado (sem zeros à esquerda, maiúsculo), para comparar com leituras.
ALTER TABLE referencia_publica ADD COLUMN logradouro text, ADD COLUMN numero text;

-- Números lidos numa observação (OCR ou digitados pelo revisor), com o endereço candidato nos dados públicos.
CREATE TABLE leitura_endereco (
  id serial PRIMARY KEY,
  imovel_id int NOT NULL REFERENCES imovel ON DELETE CASCADE,
  origem text NOT NULL CHECK (origem IN ('ocr', 'manual')),
  modelo text,
  texto_original text,
  numero text NOT NULL,
  bbox int[] CHECK (cardinality(bbox) = 4),  -- região da imagem, em pixels da foto
  evidencias jsonb NOT NULL DEFAULT '{}',
  endereco_candidato text,
  referencia_id int REFERENCES referencia_publica ON DELETE SET NULL,
  estado text NOT NULL DEFAULT 'pendente' CHECK (estado IN ('pendente', 'confirmada', 'rejeitada')),
  revisada_em timestamptz,
  criada_em timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON leitura_endereco (imovel_id);
