from __future__ import annotations


RECONCILIAR_REGISTROS_SQL = """
WITH rastreados_obsoletos AS (
    SELECT DISTINCT rastreio.registro_glosa_id
      FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
      LEFT JOIN api_prontocardio.glosas_ipm_vinculadas AS atual
        ON atual.id_registro = rastreio.id_registro
     WHERE atual.id_registro IS NULL
), rastreados_vigentes AS (
    SELECT DISTINCT rastreio.registro_glosa_id
      FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
      JOIN api_prontocardio.glosas_ipm_vinculadas AS atual
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
DELETE FROM api_prontocardio.registros_glosa_demonstrativo_ipm AS rastreio
 USING api_prontocardio.registros_glosa AS registro
 WHERE registro.id = rastreio.registro_glosa_id
   AND registro.origem_registro IN ('triagem', 'conciliacao')
   AND NOT EXISTS (
       SELECT 1
         FROM api_prontocardio.glosas_ipm_vinculadas AS atual
        WHERE atual.id_registro = rastreio.id_registro
   )
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


def materializar_registros_glosa(postgres) -> dict[str, int]:
    try:
        with postgres.cursor() as cursor:
            cursor.execute(RECONCILIAR_REGISTROS_SQL)
            desativados = max(cursor.rowcount, 0)
            cursor.execute(REMOVER_RASTREIOS_OBSOLETOS_SQL)
            rastreios_removidos = max(cursor.rowcount, 0)
            cursor.execute(MATERIALIZAR_REGISTROS_SQL)
            registros = max(cursor.rowcount, 0)
            cursor.execute(MATERIALIZAR_RASTREIO_SQL)
            rastreios = max(cursor.rowcount, 0)
        postgres.commit()
    except Exception:
        postgres.rollback()
        raise
    return {
        "registros_desativados": desativados,
        "rastreios_removidos": rastreios_removidos,
        "registros_glosa": registros,
        "rastreios": rastreios,
    }
