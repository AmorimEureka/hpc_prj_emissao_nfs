from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow.decorators import dag, task
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.issec_portal import (
    DEFAULT_BASE_URL,
    extract_and_load_processos,
)


POSTGRES_CONN_ID = os.getenv("ISSEC_POSTGRES_CONN_ID", "postgres_prontocardio")


@dag(
    dag_id="extracao_processos_portal_issec",
    description=(
        "Extrai os processos do portal do credenciado ISSEC e carrega "
        "raw_issec_portal.processos com dlt."
    ),
    schedule=os.getenv("ISSEC_PORTAL_SCHEDULE", "0 5 * * *"),
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=4),
    },
    tags=["issec", "processos", "dlt"],
)
def extracao_processos_portal_issec():
    @task(task_id="extrair_e_carregar")
    def extrair_e_carregar() -> dict[str, list[str]]:
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        try:
            rows = hook.get_records(
                """
                SELECT DISTINCT DATE_TRUNC('month', mes_producao)::date
                  FROM raw_issec_portal.processos
                 WHERE mes_producao IS NOT NULL
                """
            )
            extracted_months = {row[0] for row in rows}
        except Exception:
            # Primeira execução: o dlt ainda criará schema e tabela.
            extracted_months = set()

        return extract_and_load_processos(
            username=os.environ["ISSEC_PORTAL_LOGIN"],
            password=os.environ["ISSEC_PORTAL_SENHA"],
            database_url=hook.get_uri(),
            extracted_months=extracted_months,
            base_url=os.getenv("ISSEC_PORTAL_URL", DEFAULT_BASE_URL),
            timeout=int(os.getenv("ISSEC_PORTAL_TIMEOUT", "30")),
            max_workers=int(os.getenv("ISSEC_PORTAL_WORKERS", "4")),
        )

    extrair_e_carregar()


dag = extracao_processos_portal_issec()
