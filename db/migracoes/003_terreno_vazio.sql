-- Categoria 'terreno_vazio': lote sem edificação. Não existe no GSAN; com matrícula vinculada aparece como divergência.
ALTER DOMAIN categoria_uso DROP CONSTRAINT categoria_uso_check;
ALTER DOMAIN categoria_uso ADD CONSTRAINT categoria_uso_check
  CHECK (VALUE IN ('residencial', 'comercial', 'industrial', 'institucional', 'misto', 'terreno_vazio', 'inconclusivo'));
