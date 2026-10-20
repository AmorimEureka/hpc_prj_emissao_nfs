from __future__ import annotations

from nfs_fortaleza.glosas_ipm_context import ENRIQUECER_CONTEXTO_MAIDA_SQL


RECONCILIAR_REGISTROS_SQL = """
WITH fontes_atuais AS (
    SELECT id_registro FROM api_prontocardio.glosas_ipm_vinculadas
    UNION
    SELECT id_registro
      FROM api_prontocardio.demonstrativo_conta_ipm
     WHERE origem_maida IS NOT NULL AND COALESCE(valor_glosa, 0) > 0
), rastreados_obsoletos AS (
    SELECT DISTINCT rastreio.registro_glosa_id
      FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
      LEFT JOIN fontes_atuais AS atual
        ON atual.id_registro = rastreio.id_registro
     WHERE atual.id_registro IS NULL
), rastreados_vigentes AS (
    SELECT DISTINCT rastreio.registro_glosa_id
      FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
      JOIN fontes_atuais AS atual
        ON atual.id_registro = rastreio.id_registro
)
UPDATE api_prontocardio.registros_glosa AS registro
   SET sn_ativo = 'false'
  FROM rastreados_obsoletos AS obsoleto
 WHERE registro.id = obsoleto.registro_glosa_id
   AND registro.sn_ativo = 'true'
   AND registro.origem_registro IN ('triagem', 'conciliacao')
   AND registro.qtd_recursado IS NULL
   AND registro.valor_recursado IS NULL
   AND registro.dt_recurso IS NULL
   AND registro.dt_pagamento IS NULL
   AND registro.dt_recebimento IS NULL
   AND NOT EXISTS (
       SELECT 1
         FROM rastreados_vigentes AS vigente
        WHERE vigente.registro_glosa_id = registro.id
   )
"""


REMOVER_RASTREIOS_OBSOLETOS_SQL = """
WITH fontes_atuais AS (
    SELECT id_registro FROM api_prontocardio.glosas_ipm_vinculadas
    UNION
    SELECT id_registro
      FROM api_prontocardio.demonstrativo_conta_ipm
     WHERE origem_maida IS NOT NULL AND COALESCE(valor_glosa, 0) > 0
)
DELETE FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
 USING api_prontocardio.registros_glosa AS registro
 WHERE registro.id = rastreio.registro_glosa_id
   AND registro.origem_registro IN ('triagem', 'conciliacao')
   AND NOT EXISTS (
       SELECT 1
         FROM fontes_atuais AS atual
        WHERE atual.id_registro = rastreio.id_registro
   )
"""


VINCULAR_TRATATIVAS_MANUAIS_MAIDA_SQL = """
WITH pendentes AS (
    SELECT demo.id_registro,
           UPPER(BTRIM(demo.numero_guia_senha)) AS guia,
           UPPER(BTRIM(demo.codigo_servico)) AS codigo_item
      FROM api_prontocardio.glossas_nao_vinculadas_ipm AS glosa
      JOIN api_prontocardio.demonstrativo_conta_ipm AS demo
        ON demo.id_registro = glosa.id_registro
     WHERE demo.origem_maida IS NOT NULL
       AND COALESCE(demo.valor_glosa, 0) > 0
       AND NULLIF(BTRIM(demo.numero_guia_senha), '') IS NOT NULL
       AND NULLIF(BTRIM(demo.codigo_servico), '') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
             FROM api_prontocardio.registros_glosa_demonstrativo_ipm r
            WHERE r.id_registro = demo.id_registro
       )
       AND NOT EXISTS (
           SELECT 1
             FROM api_prontocardio.registros_glosa r
            WHERE r.processo_controle_fatura_gab =
                  'MAIDA-' || COALESCE(NULLIF(BTRIM(demo.numero_lote), ''),
                                       TO_CHAR(demo.referencia, 'YYYYMM'))
              AND UPPER(BTRIM(r.guia)) = UPPER(BTRIM(COALESCE(
                  demo.numero_guia_senha, '-')))
              AND UPPER(BTRIM(r.procedimento)) = UPPER(BTRIM(COALESCE(
                  demo.codigo_servico, '-')))
              AND r.motivo_glosa IS NOT DISTINCT FROM
                  NULLIF(BTRIM(demo.codigo_glosa), '')
              AND r.sn_ativo = 'true'
       )
), candidatos AS (
    SELECT pendente.id_registro, registro.id AS registro_glosa_id,
           COUNT(*) OVER (PARTITION BY pendente.id_registro) AS quantidade
      FROM pendentes AS pendente
      JOIN api_prontocardio.registros_glosa AS registro
        ON UPPER(BTRIM(registro.guia)) = pendente.guia
       AND pendente.codigo_item IN (
           UPPER(BTRIM(registro.procedimento)),
           UPPER(BTRIM(COALESCE(registro.cd_tuss, '')))
       )
       AND registro.sn_ativo = 'true'
)
INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm (
    id_registro, registro_glosa_id, criterio_correspondencia
)
SELECT id_registro, registro_glosa_id,
       'maida_tratativa_manual_guia_codigo_item'
  FROM candidatos
 WHERE quantidade = 1
ON CONFLICT (id_registro) DO NOTHING
"""


MATERIALIZAR_MAIDA_PENDENTE_SQL = """
WITH pendentes AS (
    SELECT demo.*,
           CASE
               WHEN BTRIM(COALESCE(demo.numero_lote, '')) ~ '^[0-9]+$'
                   THEN LEAST(demo.numero_lote::BIGINT, 2147483647)::INTEGER
               ELSE 1000000000 + (
                   ABS(HASHTEXTEXTENDED(COALESCE(demo.numero_lote, demo.id_registro), 0))
                   % 1000000000
               )::INTEGER
           END AS remessa_maida,
           CASE
               WHEN BTRIM(COALESCE(demo.numero_guia_senha, '')) ~ '^[0-9]+$'
                   THEN LEAST(demo.numero_guia_senha::BIGINT, 2147483647)::INTEGER
               ELSE 1000000000 + (
                   ABS(HASHTEXTEXTENDED(demo.id_registro || '|conta', 0))
                   % 1000000000
               )::INTEGER
           END AS conta_maida,
           1000000000 + (
               ABS(HASHTEXTEXTENDED(demo.id_registro || '|lancamento', 0))
               % 1000000000
           )::INTEGER AS lancamento_maida
      FROM api_prontocardio.glossas_nao_vinculadas_ipm AS glosa
      JOIN api_prontocardio.demonstrativo_conta_ipm AS demo
        ON demo.id_registro = glosa.id_registro
     WHERE demo.origem_maida IS NOT NULL
       AND COALESCE(demo.valor_glosa, 0) > 0
       AND NOT EXISTS (
           SELECT 1
             FROM api_prontocardio.registros_glosa_demonstrativo_ipm r
            WHERE r.id_registro = demo.id_registro
       )
       AND NOT EXISTS (
           SELECT 1
             FROM api_prontocardio.registros_glosa r
            WHERE r.processo_controle_fatura_gab =
                  'MAIDA-' || COALESCE(NULLIF(BTRIM(demo.numero_lote), ''),
                                       TO_CHAR(demo.referencia, 'YYYYMM'))
              AND UPPER(BTRIM(r.guia)) = UPPER(BTRIM(COALESCE(
                  demo.numero_guia_senha, '-')))
              AND UPPER(BTRIM(r.procedimento)) = UPPER(BTRIM(COALESCE(
                  demo.codigo_servico, '-')))
              AND r.motivo_glosa IS NOT DISTINCT FROM
                  NULLIF(BTRIM(demo.codigo_glosa), '')
              AND r.sn_ativo = 'true'
       )
)
INSERT INTO api_prontocardio.registros_glosa (
    codigo_paciente, nm_paciente, cd_remessa, cd_atendimento, conta,
    cd_prestador, cd_convenio, tp_atendimento, procedimento, convenio,
    guia, prestador, data_atendimento, valor,
    processo_controle_fatura_gab, processo_recurso, data_glosa,
    motivo_glosa, descricao_glosa, qtd_recursado, valor_recursado,
    dt_recurso, dt_pagamento, dt_recebimento, valor_recebido,
    qtd_recebida, observacao_recebimento, cd_lancamento, qtd_registro,
    descricao_item, data_alta, data_lancamento, cd_gru_pro, ds_gru_pro,
    cd_gru_fat, ds_gru_fat, cd_tuss, conciliacao_remessa_id,
    origem_registro, sn_glosado, sn_ativo, numero_lote
)
SELECT 0,
       COALESCE(NULLIF(BTRIM(item.nome_beneficiario), ''),
                'Guia ' || COALESCE(NULLIF(BTRIM(item.numero_guia_senha), ''),
                                    'não informada')),
       item.remessa_maida, 0, item.conta_maida, 0, 10, 'Externo',
       COALESCE(NULLIF(BTRIM(item.codigo_servico), ''), '-'), 'ISSEC',
       COALESCE(NULLIF(BTRIM(item.numero_guia_senha), ''), '-'),
       'HOSPITAL PRONTOCARDIO',
       COALESCE(item.data_realizacao::TIMESTAMP,
                item.referencia::TIMESTAMP,
                timezone('America/Sao_Paulo', now())),
       COALESCE(item.valor_processado, 0),
       'MAIDA-' || COALESCE(NULLIF(BTRIM(item.numero_lote), ''),
                            TO_CHAR(item.referencia, 'YYYYMM')),
       NULL, COALESCE(item.data_envio_lote, item.referencia, CURRENT_DATE),
       NULLIF(BTRIM(item.codigo_glosa), ''),
       CONCAT(COALESCE(NULLIF(BTRIM(item.descricao_servico), ''),
                       'Item extraído da Maida'),
              '. Valor glosado na origem: R$ ',
              TO_CHAR(item.valor_glosa, 'FM999999999990D00')),
       item.quantidade_executada, item.valor_glosa,
       NULL, NULL, NULL, NULL, NULL, NULL, item.lancamento_maida,
       item.quantidade_executada, item.descricao_servico,
       NULL, item.data_realizacao::TIMESTAMP, 0, 'Itens Maida',
       0, 'Itens Maida', item.codigo_servico, NULL,
       'triagem', 'true', 'true', item.numero_lote
  FROM pendentes AS item
"""


MATERIALIZAR_RASTREIO_MAIDA_PENDENTE_SQL = """
INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm (
    id_registro, registro_glosa_id, criterio_correspondencia
)
SELECT DISTINCT ON (demo.id_registro) demo.id_registro, registro.id,
       'maida_pendente_lote_guia_codigo_item'
  FROM api_prontocardio.glossas_nao_vinculadas_ipm AS glosa
  JOIN api_prontocardio.demonstrativo_conta_ipm AS demo
    ON demo.id_registro = glosa.id_registro
   AND demo.origem_maida IS NOT NULL
  JOIN api_prontocardio.registros_glosa AS registro
    ON registro.processo_controle_fatura_gab =
       'MAIDA-' || COALESCE(NULLIF(BTRIM(demo.numero_lote), ''),
                            TO_CHAR(demo.referencia, 'YYYYMM'))
   AND UPPER(BTRIM(registro.guia)) =
       UPPER(BTRIM(COALESCE(demo.numero_guia_senha, '-')))
   AND UPPER(BTRIM(registro.procedimento)) =
       UPPER(BTRIM(COALESCE(demo.codigo_servico, '-')))
   AND registro.motivo_glosa IS NOT DISTINCT FROM
       NULLIF(BTRIM(demo.codigo_glosa), '')
   AND registro.sn_ativo = 'true'
 WHERE NOT EXISTS (
       SELECT 1
         FROM api_prontocardio.registros_glosa_demonstrativo_ipm r
        WHERE r.id_registro = demo.id_registro
 )
 ORDER BY demo.id_registro, registro.id
ON CONFLICT (id_registro) DO NOTHING
"""


MATERIALIZAR_REGISTROS_SQL = """
WITH vinculos AS (
    SELECT DISTINCT ON (
               UPPER(BTRIM(conc.processo_recebimento)), rem.cd_remessa
           )
           UPPER(BTRIM(conc.processo_recebimento)) AS processo_normalizado,
           rem.cd_remessa,
           rem.id AS conciliacao_remessa_id
      FROM api_prontocardio.conciliacoes_faturamento AS conc
      JOIN api_prontocardio.conciliacoes_faturamento_remessas AS rem
        ON rem.conciliacao_id = conc.id
     WHERE conc.ativo IS TRUE
     ORDER BY UPPER(BTRIM(conc.processo_recebimento)), rem.cd_remessa,
              rem.id DESC
), itens AS (
    SELECT vinculos.conciliacao_remessa_id,
           glosa.numero_processo,
           glosa.cd_remessa,
           glosa.conta,
           glosa.cd_lancamento,
           NULLIF(BTRIM(glosa.codigo_glosa), '') AS codigo_glosa,
           MAX(glosa.cd_paciente) AS cd_paciente,
           MAX(glosa.nm_paciente) AS nm_paciente,
           MAX(glosa.cd_atendimento) AS cd_atendimento,
           MAX(glosa.cd_prestador) AS cd_prestador,
           MAX(glosa.nm_prestador) AS nm_prestador,
           MAX(glosa.cd_convenio) AS cd_convenio,
           MAX(glosa.nm_convenio) AS nm_convenio,
           MAX(glosa.tp_atendimento) AS tp_atendimento,
           MAX(glosa.cd_pro_fat) AS cd_pro_fat,
           MAX(NULLIF(glosa.cd_tuss, '')) AS cd_tuss,
           MAX(glosa.nr_guia) AS nr_guia,
           MAX(glosa.dt_atendimento) AS dt_atendimento,
           MAX(glosa.dt_alta) AS dt_alta,
           MAX(glosa.dt_lancamento) AS dt_lancamento,
           MAX(glosa.qt_lancamento) AS qt_lancamento,
           MAX(glosa.valor_item) AS valor_item,
           MAX(glosa.descricao) AS descricao,
           MAX(glosa.cd_gru_pro) AS cd_gru_pro,
           MAX(glosa.ds_gru_pro) AS ds_gru_pro,
           MAX(glosa.cd_gru_fat) AS cd_gru_fat,
           MAX(glosa.ds_gru_fat) AS ds_gru_fat,
           MAX(glosa.data_realizacao) AS data_glosa,
           SUM(COALESCE(glosa.valor_glosa, 0)) AS valor_glosa
      FROM api_prontocardio.glosas_ipm_vinculadas AS glosa
      LEFT JOIN vinculos
        ON vinculos.processo_normalizado
         = UPPER(BTRIM(glosa.numero_processo))
       AND vinculos.cd_remessa = glosa.cd_remessa
     GROUP BY vinculos.conciliacao_remessa_id, glosa.numero_processo,
              glosa.cd_remessa, glosa.conta, glosa.cd_lancamento,
              NULLIF(BTRIM(glosa.codigo_glosa), '')
)
INSERT INTO api_prontocardio.registros_glosa (
    codigo_paciente, nm_paciente, cd_remessa, cd_atendimento, conta,
    cd_prestador, cd_convenio, tp_atendimento, procedimento, convenio,
    guia, prestador, data_atendimento, valor,
    processo_controle_fatura_gab, processo_recurso, data_glosa,
    motivo_glosa, descricao_glosa, qtd_recursado, valor_recursado,
    dt_recurso, dt_pagamento, dt_recebimento, valor_recebido,
    qtd_recebida, observacao_recebimento, cd_lancamento, qtd_registro,
    descricao_item, data_alta, data_lancamento, cd_gru_pro, ds_gru_pro,
    cd_gru_fat, ds_gru_fat, cd_tuss, conciliacao_remessa_id,
    origem_registro, sn_glosado, sn_ativo
)
SELECT COALESCE(item.cd_paciente, 0), item.nm_paciente, item.cd_remessa,
       COALESCE(item.cd_atendimento, 0), item.conta,
       COALESCE(item.cd_prestador, 0), COALESCE(item.cd_convenio, 0),
       COALESCE(NULLIF(item.tp_atendimento, ''), 'Externo'),
       COALESCE(NULLIF(item.cd_pro_fat, ''), '-'),
       COALESCE(NULLIF(item.nm_convenio, ''), 'IPM'),
       COALESCE(NULLIF(item.nr_guia, ''), '-'),
       COALESCE(NULLIF(item.nm_prestador, ''), 'Prestador não informado'),
       COALESCE(item.dt_atendimento, item.dt_lancamento,
                item.data_glosa::timestamp,
                timezone('America/Sao_Paulo', now())),
       COALESCE(item.valor_item, 0), item.numero_processo, NULL,
       COALESCE(item.data_glosa, CURRENT_DATE), item.codigo_glosa,
       CONCAT(
           COALESCE(NULLIF(item.descricao, ''), 'Item do demonstrativo IPM'),
           '. Valor glosado na origem: R$ ',
           TO_CHAR(item.valor_glosa, 'FM999999999990D00')
       ),
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       item.cd_lancamento, item.qt_lancamento, item.descricao,
       item.dt_alta, item.dt_lancamento, item.cd_gru_pro,
       COALESCE(item.ds_gru_pro, 'Grupo não informado'), item.cd_gru_fat,
       COALESCE(item.ds_gru_fat, 'Grupo não informado'), item.cd_tuss,
       item.conciliacao_remessa_id,
       CASE WHEN item.conciliacao_remessa_id IS NULL
            THEN 'triagem' ELSE 'conciliacao' END,
       'true', 'true'
  FROM itens AS item
 WHERE NOT EXISTS (
       SELECT 1
         FROM api_prontocardio.registros_glosa AS existente
        WHERE UPPER(BTRIM(existente.processo_controle_fatura_gab))
              = UPPER(BTRIM(item.numero_processo))
          AND existente.cd_remessa = item.cd_remessa
          AND existente.conciliacao_remessa_id IS NOT DISTINCT FROM
              item.conciliacao_remessa_id
          AND existente.conta = item.conta
          AND existente.cd_lancamento IS NOT DISTINCT FROM item.cd_lancamento
          AND existente.motivo_glosa IS NOT DISTINCT FROM item.codigo_glosa
          AND existente.sn_ativo = 'true'
 )
ON CONFLICT ON CONSTRAINT uq_registro_glosa_conciliacao_item DO UPDATE
SET codigo_paciente = EXCLUDED.codigo_paciente,
    nm_paciente = EXCLUDED.nm_paciente,
    cd_atendimento = EXCLUDED.cd_atendimento,
    cd_prestador = EXCLUDED.cd_prestador,
    cd_convenio = EXCLUDED.cd_convenio,
    tp_atendimento = EXCLUDED.tp_atendimento,
    procedimento = EXCLUDED.procedimento,
    convenio = EXCLUDED.convenio,
    guia = EXCLUDED.guia,
    prestador = EXCLUDED.prestador,
    data_atendimento = EXCLUDED.data_atendimento,
    valor = EXCLUDED.valor,
    processo_controle_fatura_gab = EXCLUDED.processo_controle_fatura_gab,
    data_glosa = EXCLUDED.data_glosa,
    descricao_glosa = EXCLUDED.descricao_glosa,
    qtd_registro = EXCLUDED.qtd_registro,
    descricao_item = EXCLUDED.descricao_item,
    data_alta = EXCLUDED.data_alta,
    data_lancamento = EXCLUDED.data_lancamento,
    cd_gru_pro = EXCLUDED.cd_gru_pro,
    ds_gru_pro = EXCLUDED.ds_gru_pro,
    cd_gru_fat = EXCLUDED.cd_gru_fat,
    ds_gru_fat = EXCLUDED.ds_gru_fat,
    cd_tuss = EXCLUDED.cd_tuss,
    origem_registro = EXCLUDED.origem_registro,
    sn_ativo = 'true'
"""


MATERIALIZAR_RASTREIO_SQL = """
WITH vinculos AS (
    SELECT DISTINCT ON (
               UPPER(BTRIM(conc.processo_recebimento)), rem.cd_remessa
           )
           UPPER(BTRIM(conc.processo_recebimento)) AS processo_normalizado,
           rem.cd_remessa,
           rem.id AS conciliacao_remessa_id
      FROM api_prontocardio.conciliacoes_faturamento AS conc
      JOIN api_prontocardio.conciliacoes_faturamento_remessas AS rem
        ON rem.conciliacao_id = conc.id
     WHERE conc.ativo IS TRUE
     ORDER BY UPPER(BTRIM(conc.processo_recebimento)), rem.cd_remessa,
              rem.id DESC
), rastreios AS (
    SELECT glosa.id_registro,
           glosa.criterio_correspondencia,
           registro.id AS registro_glosa_id
      FROM api_prontocardio.glosas_ipm_vinculadas AS glosa
      LEFT JOIN vinculos
        ON vinculos.processo_normalizado
         = UPPER(BTRIM(glosa.numero_processo))
       AND vinculos.cd_remessa = glosa.cd_remessa
      JOIN LATERAL (
          SELECT item.id
            FROM api_prontocardio.registros_glosa AS item
           WHERE UPPER(BTRIM(item.processo_controle_fatura_gab))
                 = UPPER(BTRIM(glosa.numero_processo))
             AND item.cd_remessa = glosa.cd_remessa
             AND item.conciliacao_remessa_id IS NOT DISTINCT FROM
                 vinculos.conciliacao_remessa_id
             AND item.conta = glosa.conta
             AND item.cd_lancamento IS NOT DISTINCT FROM glosa.cd_lancamento
             AND item.motivo_glosa IS NOT DISTINCT FROM
                 NULLIF(BTRIM(glosa.codigo_glosa), '')
             AND item.sn_ativo = 'true'
           ORDER BY (item.dt_recurso IS NULL) DESC, item.id
           LIMIT 1
      ) AS registro ON TRUE
)
INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm (
    id_registro, registro_glosa_id, criterio_correspondencia
)
SELECT id_registro, registro_glosa_id, criterio_correspondencia
  FROM rastreios
ON CONFLICT (id_registro) DO UPDATE
SET registro_glosa_id = EXCLUDED.registro_glosa_id,
    criterio_correspondencia = EXCLUDED.criterio_correspondencia,
    data_importacao = timezone('America/Sao_Paulo', now())
"""


CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL = """
WITH pares_brutos AS (
    SELECT DISTINCT
           rastreio.registro_glosa_id AS destino_id,
           legado.id AS legado_id,
           (
               legado.processo_recurso IS NOT NULL
               OR legado.qtd_recursado IS NOT NULL
               OR legado.valor_recursado IS NOT NULL
               OR legado.dt_recurso IS NOT NULL
               OR legado.dt_pagamento IS NOT NULL
               OR legado.dt_recebimento IS NOT NULL
               OR legado.valor_recebido IS NOT NULL
               OR legado.qtd_recebida IS NOT NULL
               OR legado.observacao_recebimento IS NOT NULL
               OR legado.descricao_glosa_agrupada IS NOT NULL
               OR legado.descricao_recurso_agrupada IS NOT NULL
               OR legado.descricao_acato_agrupada IS NOT NULL
           ) AS possui_tratativa,
           (
               destino.processo_recurso IS NOT NULL
               OR destino.qtd_recursado IS NOT NULL
               OR destino.valor_recursado IS NOT NULL
               OR destino.dt_recurso IS NOT NULL
               OR destino.dt_pagamento IS NOT NULL
               OR destino.dt_recebimento IS NOT NULL
               OR destino.valor_recebido IS NOT NULL
               OR destino.qtd_recebida IS NOT NULL
               OR destino.observacao_recebimento IS NOT NULL
               OR destino.descricao_glosa_agrupada IS NOT NULL
               OR destino.descricao_recurso_agrupada IS NOT NULL
               OR destino.descricao_acato_agrupada IS NOT NULL
           ) AS destino_possui_tratativa
      FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
      JOIN api_prontocardio.demonstrativo_conta_ipm AS demo
        ON demo.id_registro = rastreio.id_registro
       AND demo.origem_maida IS NOT NULL
      JOIN api_prontocardio.registros_glosa AS destino
        ON destino.id = rastreio.registro_glosa_id
       AND destino.sn_ativo = 'true'
      JOIN api_prontocardio.registros_glosa AS legado
        ON legado.id <> destino.id
       AND legado.sn_ativo = 'true'
       AND (
           UPPER(BTRIM(legado.processo_controle_fatura_gab)) = UPPER(
               'MAIDA-' || COALESCE(NULLIF(BTRIM(demo.numero_lote), ''),
                                    'SEM-LOTE')
           )
           OR (
               -- Context-only enrichment retains the synthetic launch.
               -- A later exact procedure match can still migrate treatment.
               legado.cd_lancamento >= 1000000000
               AND legado.origem_registro = 'triagem'
               AND BTRIM(legado.numero_lote) = BTRIM(demo.numero_lote)
               AND BTRIM(legado.processo_controle_fatura_gab) =
                   BTRIM(demo.numero_lote)
           )
       )
       AND UPPER(BTRIM(legado.guia)) =
           UPPER(BTRIM(COALESCE(demo.numero_guia_senha, '-')))
       AND UPPER(BTRIM(legado.procedimento)) =
           UPPER(BTRIM(COALESCE(demo.codigo_servico, '-')))
       AND legado.motivo_glosa IS NOT DISTINCT FROM
           NULLIF(BTRIM(demo.codigo_glosa), '')
     WHERE rastreio.criterio_correspondencia LIKE 'maida_hpc_%'
), pares_contados AS (
    SELECT pares_brutos.*,
           COUNT(*) OVER (PARTITION BY legado_id) AS destinos_por_legado,
           COUNT(*) FILTER (WHERE possui_tratativa)
               OVER (PARTITION BY destino_id)
           + MAX(destino_possui_tratativa::integer)
               OVER (PARTITION BY destino_id) AS tratativas_por_destino
      FROM pares_brutos
), pares_seguros AS (
    SELECT *
      FROM pares_contados
     WHERE destinos_por_legado = 1
       AND tratativas_por_destino <= 1
), fontes AS (
    SELECT DISTINCT ON (par.destino_id)
           par.destino_id, par.legado_id, par.possui_tratativa,
           legado.processo_recurso, legado.descricao_glosa,
           legado.qtd_recursado, legado.valor_recursado,
           legado.dt_recurso, legado.dt_pagamento,
           legado.dt_recebimento, legado.valor_recebido,
           legado.qtd_recebida, legado.observacao_recebimento,
           legado.descricao_glosa_agrupada,
           legado.descricao_recurso_agrupada,
           legado.descricao_acato_agrupada, legado.numero_lote
      FROM pares_seguros AS par
      JOIN api_prontocardio.registros_glosa AS legado
        ON legado.id = par.legado_id
     ORDER BY par.destino_id, par.possui_tratativa DESC, par.legado_id
), migrados AS (
    UPDATE api_prontocardio.registros_glosa AS destino
       SET processo_recurso = COALESCE(fonte.processo_recurso,
                                       destino.processo_recurso),
           descricao_glosa = CASE
               WHEN fonte.possui_tratativa
                   THEN COALESCE(NULLIF(BTRIM(fonte.descricao_glosa), ''),
                                 destino.descricao_glosa)
               ELSE destino.descricao_glosa
           END,
           qtd_recursado = COALESCE(fonte.qtd_recursado,
                                    destino.qtd_recursado),
           valor_recursado = COALESCE(fonte.valor_recursado,
                                      destino.valor_recursado),
           dt_recurso = COALESCE(fonte.dt_recurso, destino.dt_recurso),
           dt_pagamento = COALESCE(fonte.dt_pagamento,
                                   destino.dt_pagamento),
           dt_recebimento = COALESCE(fonte.dt_recebimento,
                                     destino.dt_recebimento),
           valor_recebido = COALESCE(fonte.valor_recebido,
                                     destino.valor_recebido),
           qtd_recebida = COALESCE(fonte.qtd_recebida,
                                   destino.qtd_recebida),
           observacao_recebimento = COALESCE(
               fonte.observacao_recebimento,
               destino.observacao_recebimento
           ),
           descricao_glosa_agrupada = COALESCE(
               fonte.descricao_glosa_agrupada,
               destino.descricao_glosa_agrupada
           ),
           descricao_recurso_agrupada = COALESCE(
               fonte.descricao_recurso_agrupada,
               destino.descricao_recurso_agrupada
           ),
           descricao_acato_agrupada = COALESCE(
               fonte.descricao_acato_agrupada,
               destino.descricao_acato_agrupada
           ),
           numero_lote = COALESCE(fonte.numero_lote, destino.numero_lote)
      FROM fontes AS fonte
     WHERE destino.id = fonte.destino_id
     RETURNING destino.id
)
UPDATE api_prontocardio.registros_glosa AS legado
   SET sn_ativo = 'false'
  FROM pares_seguros AS par
 WHERE legado.id = par.legado_id
   AND EXISTS (
       SELECT 1 FROM migrados WHERE migrados.id = par.destino_id
   )
"""


def materializar_registros_glosa(postgres) -> dict[str, int]:
    try:
        with postgres.cursor() as cursor:
            cursor.execute(RECONCILIAR_REGISTROS_SQL)
            desativados = max(cursor.rowcount, 0)
            cursor.execute(REMOVER_RASTREIOS_OBSOLETOS_SQL)
            rastreios_removidos = max(cursor.rowcount, 0)
            cursor.execute(VINCULAR_TRATATIVAS_MANUAIS_MAIDA_SQL)
            tratativas_manuais_maida = max(cursor.rowcount, 0)
            cursor.execute(MATERIALIZAR_REGISTROS_SQL)
            registros = max(cursor.rowcount, 0)
            cursor.execute(MATERIALIZAR_RASTREIO_SQL)
            rastreios = max(cursor.rowcount, 0)
            cursor.execute(CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL)
            registros_maida_consolidados = max(cursor.rowcount, 0)
            cursor.execute(MATERIALIZAR_MAIDA_PENDENTE_SQL)
            registros_maida_pendentes = max(cursor.rowcount, 0)
            cursor.execute(MATERIALIZAR_RASTREIO_MAIDA_PENDENTE_SQL)
            rastreios_maida_pendentes = max(cursor.rowcount, 0)
            cursor.execute(ENRIQUECER_CONTEXTO_MAIDA_SQL)
            contextos_maida = max(cursor.rowcount, 0)
        postgres.commit()
    except Exception:
        postgres.rollback()
        raise
    return {
        "registros_desativados": desativados,
        "rastreios_removidos": rastreios_removidos,
        "tratativas_manuais_maida": tratativas_manuais_maida,
        "registros_glosa": registros,
        "rastreios": rastreios,
        "registros_maida_consolidados": registros_maida_consolidados,
        "registros_maida_pendentes": registros_maida_pendentes,
        "rastreios_maida_pendentes": rastreios_maida_pendentes,
        "contextos_maida_enriquecidos": contextos_maida,
    }
