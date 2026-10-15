from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from psycopg2.extras import execute_values

from nfs_fortaleza.spu_report_storage import ensure_process_report_table


REMESSAS_SQL = """
WITH contas AS (
    SELECT cd_remessa, cd_reg, nm_convenio, cnpj_convenio, dt_competencia,
           MAX(vl_total_registro) AS vl_total_registro
      FROM dbamv.hpc_v_conta_atendimento
     WHERE sn_pertence_pacote = 'N'
       AND cd_convenio IN (__CONVENIOS_BASE__)
       AND cd_remessa IS NOT NULL
       AND dt_competencia >= :data_inicial
       AND dt_competencia < :data_final
     GROUP BY cd_remessa, cd_reg, nm_convenio, cnpj_convenio,
              dt_competencia
)
SELECT cd_remessa, nm_convenio, cnpj_convenio,
       TO_CHAR(dt_competencia, 'MM/YYYY') AS competencia,
       SUM(vl_total_registro) AS valor_total
  FROM contas
 GROUP BY cd_remessa, nm_convenio, cnpj_convenio,
          TO_CHAR(dt_competencia, 'MM/YYYY')
"""

ITENS_SQL = """
WITH itens_hpc AS (
SELECT h.cd_remessa, h.cd_reg, h.cd_lancamento, h.cd_atendimento,
       h.cd_paciente, h.nm_paciente, h.cd_prestador, h.nm_prestador,
       h.cd_convenio, h.cnpj_convenio, h.nm_convenio, h.tp_atendimento,
       COALESCE(gi.nr_guia, h.nr_guia) AS nr_guia,
       h.nr_carteira, h.cd_pro_fat, h.cd_tuss, h.descricao,
       h.dt_atendimento, h.dt_alta,
       h.dt_competencia, h.dt_lancamento, h.qt_lancamento,
       h.vl_total_conta, h.cd_gru_fat, h.ds_gru_fat,
       g.cd_gru_pro, g.ds_gru_pro
  FROM dbamv.hpc_v_conta_atendimento h
  LEFT JOIN dbamv.itreg_amb ia
    ON h.cd_convenio = 46 AND h.tp_atendimento <> 'Internação'
   AND ia.cd_reg_amb = h.cd_reg AND ia.cd_lancamento = h.cd_lancamento
   AND ia.cd_atendimento = h.cd_atendimento AND ia.cd_pro_fat = h.cd_pro_fat
  LEFT JOIN dbamv.itreg_fat it
    ON h.cd_convenio = 46 AND h.tp_atendimento = 'Internação'
   AND it.cd_reg_fat = h.cd_reg AND it.cd_lancamento = h.cd_lancamento
   AND it.cd_pro_fat = h.cd_pro_fat
  LEFT JOIN dbamv.guia gi ON gi.cd_guia = COALESCE(ia.cd_guia, it.cd_guia)
  LEFT JOIN dbamv.pro_fat p ON p.cd_pro_fat = h.cd_pro_fat
  LEFT JOIN dbamv.gru_pro g ON g.cd_gru_pro = p.cd_gru_pro
 WHERE h.cd_remessa IS NOT NULL
   AND (
       (h.dt_competencia >= :data_inicial AND h.dt_competencia < :data_final)
       OR (h.dt_lancamento >= :data_inicial AND h.dt_lancamento < :data_final)
       OR (h.dt_atendimento >= :data_inicial AND h.dt_atendimento < :data_final)
   )
)
SELECT h.* FROM itens_hpc h
 WHERE (__ESCOPO_ITENS__)
"""

BRASINDICE_SQL = """
SELECT MIN(TRIM(cd_pro_fat)) AS cd_pro_fat,
       LTRIM(TRIM(cd_tiss), '0') AS cd_tiss
  FROM dbamv.imp_bra
 WHERE REGEXP_LIKE(TRIM(cd_tiss), '^[0-9]+$')
   AND TRIM(cd_pro_fat) IS NOT NULL
 GROUP BY LTRIM(TRIM(cd_tiss), '0')
HAVING COUNT(DISTINCT TRIM(cd_pro_fat)) = 1
   AND LENGTH(LTRIM(TRIM(cd_tiss), '0')) > 0
"""


def _carregar_brasindice(oracle, destino):
    destino.execute("""
        CREATE TABLE IF NOT EXISTS api_prontocardio_staging.ipm_brasindice_oracle (
            cd_pro_fat TEXT NOT NULL, cd_tiss TEXT PRIMARY KEY
        )
    """)
    destino.execute("TRUNCATE api_prontocardio_staging.ipm_brasindice_oracle")
    origem = oracle.cursor()
    try:
        origem.execute(BRASINDICE_SQL)
        for lote in _linhas(origem):
            execute_values(destino,
                "INSERT INTO api_prontocardio_staging.ipm_brasindice_oracle "
                "(cd_pro_fat, cd_tiss) VALUES %s", lote)
    finally:
        origem.close()


CONVENIOS_BASE = (10,)
TAMANHO_LOTE_GUIAS = 500


def _linhas(cursor, tamanho: int = 5000) -> Iterable[list[tuple[Any, ...]]]:
    while lote := cursor.fetchmany(tamanho):
        yield lote


def _mes_seguinte(valor: date) -> date:
    if valor.month == 12:
        return date(valor.year + 1, 1, 1)
    return date(valor.year, valor.month + 1, 1)


def _periodo_fontes_glosas(destino) -> tuple[date, date] | None:
    destino.execute("""
        SELECT MIN(data_referencia), MAX(data_referencia)
          FROM (
                SELECT data_realizacao AS data_referencia
                  FROM api_prontocardio.demonstrativo_processos_ipm
                 WHERE COALESCE(valor_glosa, 0) > 0
                   AND data_realizacao >= DATE '2024-01-01'
                   AND data_realizacao <
                       date_trunc('month', CURRENT_DATE) + INTERVAL '2 months'
                UNION ALL
                SELECT COALESCE(data_realizacao, referencia) AS data_referencia
                  FROM api_prontocardio.demonstrativo_conta_ipm
                 WHERE origem_maida IS NOT NULL
                   AND COALESCE(valor_glosa, 0) > 0
                   AND COALESCE(data_realizacao, referencia) >= DATE '2024-01-01'
                   AND COALESCE(data_realizacao, referencia) <
                       date_trunc('month', CURRENT_DATE) + INTERVAL '2 months'
                UNION ALL
                SELECT competencia AS data_referencia
                  FROM api_prontocardio.processos_relatorios_ipm
                 WHERE split_part(numero_processo, '/', 2)
                       ~ '^[0-9]{4}$'
                   AND split_part(numero_processo, '/', 2)::integer >= 2024
                   AND status_processo IN ('FINALIZADO', 'TRAMITANDO')
                   AND competencia >= DATE '2024-01-01'
                   AND competencia <
                       date_trunc('month', CURRENT_DATE) + INTERVAL '2 months'
          ) AS fontes
    """)
    data_inicial, data_final = destino.fetchone()
    if data_inicial is None or data_final is None:
        return None
    if isinstance(data_inicial, datetime):
        data_inicial = data_inicial.date()
    if isinstance(data_final, datetime):
        data_final = data_final.date()
    return date(data_inicial.year, data_inicial.month, 1), _mes_seguinte(
        data_final
    )


def _guias_maida_com_glosa(
    destino, data_inicial: date, data_final: date
) -> tuple[str, ...]:
    destino.execute(
        """
        SELECT COALESCE(
                   array_agg(DISTINCT BTRIM(numero_guia_senha::text)),
                   ARRAY[]::text[]
               )
          FROM api_prontocardio.demonstrativo_conta_ipm
         WHERE origem_maida IS NOT NULL
           AND COALESCE(valor_glosa, 0) > 0
           AND COALESCE(data_realizacao, referencia) >= %s
           AND COALESCE(data_realizacao, referencia) < %s
           AND NULLIF(BTRIM(numero_guia_senha::text), '') IS NOT NULL
        """,
        (data_inicial, data_final),
    )
    guias = destino.fetchone()[0] or []
    return tuple(str(guia).strip() for guia in guias if str(guia).strip())


def _consulta_com_escopo(
    consulta: str, guias: tuple[str, ...] = ()
) -> tuple[str, dict[str, int | str]]:
    binds = {
        f"convenio_base_{indice}": codigo
        for indice, codigo in enumerate(CONVENIOS_BASE)
    }
    marcadores_base = ", ".join(f":{nome}" for nome in binds)
    consulta = consulta.replace("__CONVENIOS_BASE__", marcadores_base)
    if "__ESCOPO_ITENS__" not in consulta:
        return consulta, binds

    escopos = [f"h.cd_convenio IN ({marcadores_base})"]
    for inicio in range(0, len(guias), TAMANHO_LOTE_GUIAS):
        lote = guias[inicio : inicio + TAMANHO_LOTE_GUIAS]
        nomes = []
        for deslocamento, guia in enumerate(lote):
            nome = f"guia_{inicio + deslocamento}"
            binds[nome] = guia
            nomes.append(f":{nome}")
        escopos.append(f"h.nr_guia IN ({', '.join(nomes)})")
    return consulta.replace("__ESCOPO_ITENS__", " OR ".join(escopos)), binds


def carregar_hpc_intermediaria(oracle, postgres) -> dict[str, int]:
    with postgres.cursor() as destino:
        ensure_process_report_table(destino, "api_prontocardio")
        periodo = _periodo_fontes_glosas(destino)
        if periodo is None:
            postgres.commit()
            return {"remessas": 0, "itens": 0}
        data_inicial, data_final = periodo
        guias_maida = _guias_maida_com_glosa(
            destino, data_inicial, data_final
        )
        destino.execute("CREATE SCHEMA IF NOT EXISTS api_prontocardio_staging")
        destino.execute("""
            CREATE TABLE IF NOT EXISTS
                api_prontocardio_staging.ipm_remessas_oracle (
                cd_remessa BIGINT, nm_convenio TEXT, cnpj_convenio TEXT,
                competencia TEXT, valor_total NUMERIC(18,2)
            )
        """)
        destino.execute("""
            CREATE TABLE IF NOT EXISTS
                api_prontocardio_staging.ipm_itens_oracle (
                cd_remessa BIGINT, cd_reg BIGINT, cd_lancamento BIGINT,
                cd_atendimento BIGINT, cd_paciente BIGINT, nm_paciente TEXT,
                cd_prestador BIGINT, nm_prestador TEXT, cd_convenio BIGINT,
                cnpj_convenio TEXT, nm_convenio TEXT, tp_atendimento TEXT,
                nr_guia TEXT, nr_carteira TEXT, cd_pro_fat TEXT, cd_tuss TEXT,
                descricao TEXT, dt_atendimento TIMESTAMP, dt_alta TIMESTAMP,
                dt_competencia DATE, dt_lancamento TIMESTAMP,
                qt_lancamento NUMERIC, vl_total_conta NUMERIC(18,2),
                cd_gru_fat BIGINT, ds_gru_fat TEXT,
                cd_gru_pro BIGINT, ds_gru_pro TEXT
            )
        """)
        destino.execute("""
            ALTER TABLE api_prontocardio_staging.ipm_itens_oracle
                ADD COLUMN IF NOT EXISTS cd_gru_pro BIGINT,
                ADD COLUMN IF NOT EXISTS ds_gru_pro TEXT
        """)
        destino.execute(
            "TRUNCATE api_prontocardio_staging.ipm_remessas_oracle, "
            "api_prontocardio_staging.ipm_itens_oracle"
        )

        totais = {}
        for nome, consulta, tabela in (
            ("remessas", REMESSAS_SQL, "ipm_remessas_oracle"),
            ("itens", ITENS_SQL, "ipm_itens_oracle"),
        ):
            consulta, parametros_escopo = _consulta_com_escopo(
                consulta, guias_maida if nome == "itens" else ()
            )
            origem = oracle.cursor()
            origem.execute(
                consulta,
                {
                    "data_inicial": data_inicial,
                    "data_final": data_final,
                    **parametros_escopo,
                },
            )
            colunas = [item[0].lower() for item in origem.description]
            quantidade = 0
            for lote in _linhas(origem):
                execute_values(
                    destino,
                    f"INSERT INTO api_prontocardio_staging.{tabela} "
                    f"({', '.join(colunas)}) VALUES %s",
                    lote,
                )
                quantidade += len(lote)
            origem.close()
            totais[nome] = quantidade
        destino.execute("""
            CREATE INDEX IF NOT EXISTS ix_ipm_remessas_oracle_chave
                ON api_prontocardio_staging.ipm_remessas_oracle
                (competencia, valor_total)
        """)
        destino.execute("""
            CREATE INDEX IF NOT EXISTS ix_ipm_itens_oracle_remessa
                ON api_prontocardio_staging.ipm_itens_oracle (cd_remessa)
        """)
        _carregar_brasindice(oracle, destino)
        postgres.commit()
        return totais
