from __future__ import annotations

from psycopg2 import sql


PROCESS_REPORT_TABLE_NAME = "processos_relatorios_ipm"
LEGACY_PROCESS_REPORT_TABLE_NAME = "processos_relatorios_tramitando_ipm"

PROCESS_REPORT_COLUMNS = (
    "id_registro",
    "numero_processo",
    "status_processo",
    "documento_id",
    "documento_nome",
    "pagina_pdf",
    "cd_remessa",
    "nome_paciente",
    "numero_guia",
    "numero_conta",
    "cd_atendimento",
    "competencia",
    "valor",
    "extraido_em",
)


def ensure_process_report_table(cursor, schema: str) -> None:
    """Create the canonical report table and migrate the legacy rows."""
    canonical = sql.Identifier(schema, PROCESS_REPORT_TABLE_NAME)
    cursor.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                id_registro TEXT PRIMARY KEY,
                numero_processo TEXT NOT NULL,
                status_processo TEXT NOT NULL,
                documento_id TEXT NOT NULL,
                documento_nome TEXT NOT NULL,
                pagina_pdf BIGINT NOT NULL,
                cd_remessa BIGINT NOT NULL,
                nome_paciente TEXT NOT NULL,
                numero_guia TEXT,
                numero_conta TEXT,
                cd_atendimento BIGINT,
                competencia DATE NOT NULL,
                valor NUMERIC(18,2) NOT NULL,
                extraido_em TIMESTAMP WITH TIME ZONE NOT NULL,
                _dlt_load_id VARCHAR NOT NULL DEFAULT 'legacy_migration',
                _dlt_id VARCHAR NOT NULL DEFAULT
                    md5(random()::text || clock_timestamp()::text)
            )
            """
        ).format(canonical)
    )
    cursor.execute(
        sql.SQL(
            """
            ALTER TABLE {}
                ADD COLUMN IF NOT EXISTS _dlt_load_id VARCHAR,
                ADD COLUMN IF NOT EXISTS _dlt_id VARCHAR
            """
        ).format(canonical)
    )
    cursor.execute(
        sql.SQL(
            """
            UPDATE {}
               SET _dlt_load_id = COALESCE(_dlt_load_id, 'legacy_migration'),
                   _dlt_id = COALESCE(_dlt_id, md5(id_registro))
             WHERE _dlt_load_id IS NULL OR _dlt_id IS NULL
            """
        ).format(canonical)
    )
    cursor.execute(
        sql.SQL(
            """
            ALTER TABLE {}
                ALTER COLUMN _dlt_load_id SET NOT NULL,
                ALTER COLUMN _dlt_load_id SET DEFAULT 'legacy_migration',
                ALTER COLUMN _dlt_id SET NOT NULL,
                ALTER COLUMN _dlt_id SET DEFAULT
                    md5(random()::text || clock_timestamp()::text)
            """
        ).format(canonical)
    )
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema = %s
               AND table_name = %s
        )
        """,
        (schema, LEGACY_PROCESS_REPORT_TABLE_NAME),
    )
    legacy_exists = bool(cursor.fetchone()[0])
    if legacy_exists:
        columns = sql.SQL(", ").join(
            sql.Identifier(column) for column in PROCESS_REPORT_COLUMNS
        )
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {} ({})
                SELECT rel.id_registro,
                       rel.numero_processo,
                       UPPER(BTRIM(proc.status_processo)),
                       rel.documento_id,
                       rel.documento_nome,
                       rel.pagina_pdf,
                       rel.cd_remessa,
                       rel.nome_paciente,
                       rel.numero_guia,
                       rel.numero_conta,
                       rel.cd_atendimento,
                       rel.competencia,
                       rel.valor,
                       rel.extraido_em
                  FROM {} AS rel
                  JOIN {} AS proc
                    ON UPPER(BTRIM(proc.numero_processo))
                     = UPPER(BTRIM(rel.numero_processo))
                 WHERE UPPER(BTRIM(proc.status_processo))
                       IN ('FINALIZADO', 'TRAMITANDO')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM {} AS atual
                        WHERE atual.id_registro = rel.id_registro
                   )
                ON CONFLICT (id_registro) DO NOTHING
                """
            ).format(
                canonical,
                columns,
                sql.Identifier(schema, LEGACY_PROCESS_REPORT_TABLE_NAME),
                sql.Identifier(schema, "processos_ipm"),
                canonical,
            )
        )
    cursor.execute(
        sql.SQL(
            """
            UPDATE {} AS rel
               SET status_processo = UPPER(BTRIM(proc.status_processo))
              FROM {} AS proc
             WHERE UPPER(BTRIM(proc.numero_processo))
                   = UPPER(BTRIM(rel.numero_processo))
               AND rel.status_processo IS DISTINCT FROM
                   UPPER(BTRIM(proc.status_processo))
            """
        ).format(
            canonical,
            sql.Identifier(schema, "processos_ipm"),
        )
    )
    cursor.execute(
        sql.SQL(
            r"""
            WITH identificadores AS (
                SELECT id_registro,
                       regexp_split_to_array(
                           btrim(numero_conta), '\s+'
                       ) AS partes
                  FROM {}
                 WHERE cd_atendimento IS NULL
                   AND numero_conta ~ '^\s*[0-9]+(?:\s+[0-9]+){{1,2}}\s*$'
            ), corrigidos AS (
                SELECT id_registro,
                       CASE
                           WHEN cardinality(partes) = 2 THEN partes[1]
                           WHEN cardinality(partes) = 3 THEN partes[2]
                       END AS numero_conta,
                       CASE
                           WHEN cardinality(partes) = 2 THEN partes[2]::bigint
                           WHEN cardinality(partes) = 3 THEN partes[3]::bigint
                       END AS cd_atendimento
                  FROM identificadores
            )
            UPDATE {} AS rel
               SET numero_conta = corrigidos.numero_conta,
                   cd_atendimento = corrigidos.cd_atendimento
              FROM corrigidos
             WHERE corrigidos.id_registro = rel.id_registro
               AND corrigidos.numero_conta IS NOT NULL
               AND corrigidos.cd_atendimento IS NOT NULL
            """
        ).format(canonical, canonical)
    )
    cursor.execute(
        sql.SQL(
            "CREATE INDEX IF NOT EXISTS {} ON {} "
            "(status_processo, numero_processo, cd_remessa)"
        ).format(
            sql.Identifier("ix_processos_relatorios_ipm_status_processo"),
            canonical,
        )
    )
