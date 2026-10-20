"""Enrich Maida technical records with a proven account context.

This does not match procedures: source codes, synthetic launch IDs, amounts,
traces and treatments remain unchanged. A unique matched lot/remittance and
unique guide/account are both required.
"""

CONTEXTOS_MAIDA_PENDENTES_SQL = """
WITH lotes AS MATERIALIZED (
    SELECT BTRIM(d.numero_lote) AS lote,
           DATE_TRUNC('month', d.referencia) AS competencia,
           MIN(v.cd_remessa) AS cd_remessa
      FROM api_prontocardio.demonstrativo_conta_ipm d
      JOIN api_prontocardio.glosas_ipm_vinculadas v USING (id_registro)
     WHERE d.origem_maida IS NOT NULL
       AND NULLIF(BTRIM(d.numero_lote), '') IS NOT NULL
     GROUP BY BTRIM(d.numero_lote), DATE_TRUNC('month', d.referencia)
    HAVING COUNT(DISTINCT v.cd_remessa) = 1
), pendentes AS MATERIALIZED (
    SELECT r.id, BTRIM(d.numero_lote) AS lote,
           d.numero_guia_senha, d.codigo_beneficiario,
           d.nome_beneficiario, d.codigo_servico,
           l.cd_remessa
      FROM api_prontocardio.registros_glosa r
      JOIN api_prontocardio.registros_glosa_demonstrativo_ipm t
        ON t.registro_glosa_id = r.id
      JOIN api_prontocardio.demonstrativo_conta_ipm d USING (id_registro)
      LEFT JOIN lotes l
        ON l.lote = BTRIM(d.numero_lote)
       AND l.competencia IS NOT DISTINCT FROM
           DATE_TRUNC('month', d.referencia)
     WHERE r.sn_ativo = 'true'
       AND UPPER(BTRIM(r.convenio)) = 'ISSEC'
       AND COALESCE(r.cd_atendimento, 0) = 0
       AND r.origem_registro = 'triagem'
       AND r.cd_lancamento >= 1000000000
       AND d.origem_maida IS NOT NULL
       AND COALESCE(d.valor_glosa, 0) > 0
       AND r.processo_controle_fatura_gab =
           'MAIDA-' || BTRIM(d.numero_lote)
), candidatos AS (
    SELECT DISTINCT p.id, p.lote, i.cd_remessa, i.cd_reg AS conta,
           i.cd_atendimento, i.cd_paciente AS codigo_paciente,
           i.nm_paciente, i.tp_atendimento, i.dt_atendimento,
           i.dt_alta, i.cd_convenio
      FROM pendentes p
      LEFT JOIN api_prontocardio_staging.ipm_itens_oracle i
        ON i.cd_remessa = p.cd_remessa
       AND i.cd_convenio = 46
       AND i.cd_atendimento > 0 AND i.cd_paciente > 0
       AND NULLIF(LTRIM(BTRIM(i.nr_guia), '0'), '') =
           NULLIF(LTRIM(BTRIM(p.numero_guia_senha), '0'), '')
       AND (
           NULLIF(BTRIM(p.codigo_beneficiario), '') IS NULL
           OR NULLIF(LTRIM(REGEXP_REPLACE(p.codigo_beneficiario,
                      '[^0-9]', '', 'g'), '0'), '') =
              NULLIF(LTRIM(REGEXP_REPLACE(i.nr_carteira,
                      '[^0-9]', '', 'g'), '0'), '')
           OR (
               NULLIF(UPPER(BTRIM(p.nome_beneficiario)), '') =
                   NULLIF(UPPER(BTRIM(i.nm_paciente)), '')
               AND (
                   NULLIF(LTRIM(BTRIM(p.codigo_servico), '0'), '') IN (
                       NULLIF(LTRIM(BTRIM(i.cd_pro_fat), '0'), ''),
                       NULLIF(LTRIM(BTRIM(i.cd_tuss), '0'), '')
                   )
                   OR EXISTS (
                       SELECT 1
                         FROM api_prontocardio_staging.ipm_brasindice_oracle b
                        WHERE b.cd_tiss = LTRIM(BTRIM(p.codigo_servico), '0')
                          AND b.cd_pro_fat = BTRIM(i.cd_pro_fat)
                   )
               )
           )
       )
), contados AS (
    SELECT *, COUNT(*) OVER (PARTITION BY id) AS quantidade
      FROM candidatos
)
SELECT id, lote, cd_remessa, conta, cd_atendimento, codigo_paciente,
       nm_paciente, tp_atendimento, dt_atendimento, dt_alta, cd_convenio
  FROM contados
 WHERE quantidade = 1 AND cd_atendimento IS NOT NULL
"""

ENRIQUECER_CONTEXTO_MAIDA_SQL = """
WITH contextos AS (
""" + CONTEXTOS_MAIDA_PENDENTES_SQL + """
)
UPDATE api_prontocardio.registros_glosa r
   SET cd_remessa = c.cd_remessa,
       processo_controle_fatura_gab = c.lote,
       conta = c.conta,
       cd_atendimento = c.cd_atendimento,
       codigo_paciente = c.codigo_paciente,
       nm_paciente = c.nm_paciente,
       tp_atendimento = c.tp_atendimento,
       data_atendimento = c.dt_atendimento,
       data_alta = c.dt_alta,
       cd_convenio = c.cd_convenio
  FROM contextos c
 WHERE r.id = c.id
"""
