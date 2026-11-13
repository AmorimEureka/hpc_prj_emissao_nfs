from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.decorators import dag, task
from airflow.operators.python import get_current_context
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.maida_config import load_maida_settings
from nfs_fortaleza.maida_extraction import (
    MaidaExtractionPayload,
    extract_and_load_maida_glosas,
)


POSTGRES_CONN_ID = os.getenv(
    "MAIDA_POSTGRES_CONN_ID",
    os.getenv("IPM_POSTGRES_CONN_ID", "postgres_prontocardio"),
)
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "api_prontocardio")
DOWNLOADS_DIR = Path(
    os.getenv("MAIDA_DOWNLOADS_DIR", "/usr/local/airflow/data/maida")
)
TIMEOUT_SECONDS = float(os.getenv("MAIDA_TIMEOUT_SECONDS", "60"))


@dag(
    dag_id="extracao_glosas_maida",
    description=(
        "Extrai demonstrativos e lotes com glosa do Maida via APIs e carrega "
        "os registros idempotentes com dlt."
    ),
    schedule=os.getenv("MAIDA_EXTRACTION_SCHEDULE", "0 2,8,14,20 * * *"),
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=15),
        "execution_timeout": timedelta(hours=4),
    },
    tags=["maida", "issec", "glosas", "dlt"],
)
def extracao_glosas_maida():
    @task(task_id="extrair_e_carregar")
    def extrair_e_carregar() -> dict[str, object]:
        context = get_current_context()
        payload = MaidaExtractionPayload.from_mapping(context["dag_run"].conf)
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ["DATABASE_URL"] = hook.get_uri()
        os.environ["POSTGRES_SCHEMA"] = POSTGRES_SCHEMA
        summary = extract_and_load_maida_glosas(
            load_maida_settings(),
            payload,
            downloads_dir=DOWNLOADS_DIR,
            timeout_seconds=TIMEOUT_SECONDS,
        )
        return summary.as_dict()

    extraction = extrair_e_carregar()
    materialize = TriggerDagRunOperator(
        task_id="acionar_materializacao_glosas_ipm",
        trigger_dag_id="materializacao_glosas_ipm",
        wait_for_completion=False,
    )
    extraction >> materialize


dag = extracao_glosas_maida()
