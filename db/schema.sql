CREATE EXTENSION IF NOT EXISTS postgis;

-- Mesmas categorias para IA, revisão manual e GSAN ('institucional' corresponde à categoria Pública do GSAN).
CREATE DOMAIN categoria_uso AS text
  CHECK (VALUE IN ('residencial', 'comercial', 'industrial', 'institucional', 'misto', 'inconclusivo'));

CREATE FUNCTION categoria_gsan(res bigint, com bigint, ind bigint, pub bigint) RETURNS text
IMMUTABLE LANGUAGE sql AS $$
  SELECT CASE
    WHEN (res > 0)::int + (com > 0)::int + (ind > 0)::int + (pub > 0)::int > 1 THEN 'misto'
    WHEN res > 0 THEN 'residencial'
    WHEN com > 0 THEN 'comercial'
    WHEN ind > 0 THEN 'industrial'
    WHEN pub > 0 THEN 'institucional'
  END
$$;

CREATE TABLE foto (
  id serial PRIMARY KEY,
  arquivo text NOT NULL UNIQUE,          -- caminho relativo a FOTOS_DIR
  fonte text NOT NULL,                   -- 'existente' ou 'captura_propria'
  capturada_em timestamp,
  largura int NOT NULL,                  -- já com a rotação EXIF aplicada
  altura int NOT NULL,
  direcao real,                          -- azimute da câmera, graus a partir do norte verdadeiro; NULL = desconhecida
  fov_horizontal real NOT NULL,          -- campo de visão horizontal da foto, em graus
  credito text,                          -- autor e licença, quando a foto é de terceiros
  geom geometry(Point, 4326) NOT NULL,   -- posição da CÂMERA
  detectada boolean NOT NULL DEFAULT false
);

-- Polígonos de edificações existentes (OpenStreetMap e Google Open Buildings).
CREATE TABLE edificacao (
  id serial PRIMARY KEY,
  fonte text NOT NULL,                   -- 'osm' ou 'open_buildings'
  id_origem text NOT NULL,
  confianca real,                        -- score do Open Buildings; NULL no OSM
  geom geometry(Polygon, 4326) NOT NULL,
  UNIQUE (fonte, id_origem)
);
CREATE INDEX ON edificacao USING gist (geom);

-- Cada fachada detectada numa foto é um imóvel candidato.
CREATE TABLE imovel (
  id serial PRIMARY KEY,
  foto_id int NOT NULL REFERENCES foto ON DELETE CASCADE,
  bbox int[],                            -- caixa atual x1,y1,x2,y2 em pixels (revisável); NULL = foto inteira
  bbox_detectada int[],                  -- caixa original do detector, para medir a qualidade da detecção
  detector text,                         -- 'manual' quando o revisor desenhou a caixa
  score_deteccao real,
  geom geometry(Point, 4326) NOT NULL,   -- posição estimada da FACHADA (a câmera está em foto.geom)
  edificacao_id int REFERENCES edificacao ON DELETE SET NULL,
  localizacao_fonte text NOT NULL DEFAULT 'camera'
    CHECK (localizacao_fonte IN ('camera', 'direcao', 'manual')),
  localizacao_confianca text NOT NULL DEFAULT 'baixa'
    CHECK (localizacao_confianca IN ('baixa', 'media', 'alta')),
  categoria_ia categoria_uso,
  unidades_ia int,
  confianca_ia real,
  evidencias_ia text,
  limitacoes_ia text,
  modelo_ia text,
  classificado_em timestamptz,
  categoria_revisada categoria_uso,
  unidades_revisadas int,
  fachada_valida boolean,                -- NULL = não revisado; false = descartado
  observacao text,
  revisado_em timestamptz,
  matriculas_confirmadas text[],         -- NULL = associação não revisada; '{}' = revisor confirmou que não há matrícula
  associacao_revisada_em timestamptz
);
CREATE INDEX ON imovel (foto_id);
CREATE INDEX ON imovel USING gist (geom);

-- CNEFE e OpenStreetMap: evidência complementar para o revisor, não entra na IA.
CREATE TABLE referencia_publica (
  id serial PRIMARY KEY,
  fonte text NOT NULL,                   -- 'cnefe' ou 'osm'
  id_origem text NOT NULL,
  categoria categoria_uso,               -- NULL quando a origem não indica o uso com clareza
  descricao text,
  geom geometry(Point, 4326) NOT NULL,
  UNIQUE (fonte, id_origem)
);
CREATE INDEX ON referencia_publica USING gist ((geom::geography));

-- Cópia importada da exportação do GSAN. Usada só na comparação final; o GSAN em si nunca é alterado.
CREATE TABLE gsan_ligacao (
  matricula text PRIMARY KEY,
  endereco text,
  economias_residencial int NOT NULL DEFAULT 0,
  economias_comercial int NOT NULL DEFAULT 0,
  economias_industrial int NOT NULL DEFAULT 0,
  economias_publica int NOT NULL DEFAULT 0,
  economias int GENERATED ALWAYS AS
    (economias_residencial + economias_comercial + economias_industrial + economias_publica) STORED,
  categoria text GENERATED ALWAYS AS
    (categoria_gsan(economias_residencial, economias_comercial, economias_industrial, economias_publica)) STORED,
  geom geometry(Point, 4326) NOT NULL
);
CREATE INDEX ON gsan_ligacao USING gist (geom);
CREATE INDEX ON gsan_ligacao USING gist ((geom::geography));

-- Associação com o GSAN e situação comercial de cada imóvel.
-- Associação: confirmada pelo revisor; ou sugerida (localização média/alta e ligações a até 8 m da edificação,
-- ou 10 m do ponto da fachada); localização só pela câmera = inconclusiva. Nunca "a mais próxima em 30 m".
CREATE VIEW imovel_analise AS
WITH base AS (
  SELECT i.*, f.geom AS geom_camera,
         coalesce(i.categoria_revisada, i.categoria_ia) AS categoria_final,
         coalesce(i.unidades_revisadas, i.unidades_ia) AS unidades_final,
         g.matriculas, g.economias AS economias_gsan, g.categoria AS categoria_gsan,
         CASE WHEN i.matriculas_confirmadas IS NOT NULL THEN 'confirmada'
              WHEN i.localizacao_confianca = 'baixa' THEN 'inconclusiva'
              WHEN g.matriculas IS NULL THEN 'sem_candidato'
              ELSE 'sugerida' END AS associacao
  FROM imovel i
  JOIN foto f ON f.id = i.foto_id
  LEFT JOIN edificacao e ON e.id = i.edificacao_id
  LEFT JOIN LATERAL (
    SELECT array_agg(l.matricula ORDER BY l.matricula) AS matriculas, sum(l.economias)::int AS economias,
           categoria_gsan(sum(l.economias_residencial), sum(l.economias_comercial),
                          sum(l.economias_industrial), sum(l.economias_publica)) AS categoria
    FROM (
      SELECT * FROM gsan_ligacao WHERE matricula = ANY (i.matriculas_confirmadas)
      UNION ALL
      SELECT * FROM gsan_ligacao l
      WHERE i.matriculas_confirmadas IS NULL AND i.localizacao_confianca <> 'baixa' AND e.id IS NOT NULL
        AND ST_DWithin(e.geom::geography, l.geom::geography, 8)
      UNION ALL
      SELECT * FROM gsan_ligacao l
      WHERE i.matriculas_confirmadas IS NULL AND i.localizacao_confianca <> 'baixa' AND e.id IS NULL
        AND ST_DWithin(i.geom::geography, l.geom::geography, 10)
    ) l
    HAVING count(*) > 0
  ) g ON true
), comparada AS (
  SELECT b.*, CASE WHEN b.matriculas IS NOT NULL AND b.categoria_final <> 'inconclusivo' THEN
           nullif(concat_ws('+',
             CASE WHEN b.categoria_gsan IS DISTINCT FROM b.categoria_final THEN 'categoria' END,
             CASE WHEN b.unidades_final > b.economias_gsan THEN 'economias' END), '')
         END AS tipo_divergencia
  FROM base b
)
SELECT c.*,
  CASE
    WHEN c.fachada_valida = false THEN 'descartado'
    WHEN c.categoria_final IS NULL THEN 'nao_classificado'
    WHEN c.categoria_final = 'inconclusivo' THEN 'classificacao_inconclusiva'
    WHEN c.associacao = 'inconclusiva' THEN 'associacao_inconclusiva'
    WHEN c.associacao = 'confirmada' AND c.matriculas IS NULL AND c.revisado_em IS NOT NULL
      THEN 'sem_matricula_confirmada'
    WHEN c.matriculas IS NULL THEN 'sem_correspondencia'
    WHEN c.tipo_divergencia IS NULL THEN 'ok'
    WHEN c.associacao = 'confirmada' AND c.revisado_em IS NOT NULL THEN 'divergencia_confirmada'
    ELSE 'possivel_divergencia'
  END AS situacao
FROM comparada c;
