from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pendulum
from airflow.decorators import dag, task
from airflow.operators.python import get_current_context
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.config import load_settings
from nfs_fortaleza.extraction import (
    ExtractionPayload,
    extract_and_load_nfse,
)
from nfs_fortaleza.portal import VISAO_TOMADOR, PortalOptions


POSTGRES_CONN_ID = os.getenv(
    "NFSE_POSTGRES_CONN_ID",
    "postgres_prontocardio",
)
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "api_prontocardio")
TABLE_NAME = os.getenv("NFSE_TOMADOR_TABLE", "nfse_xml_tomador")
DOWNLOADS_DIR = Path(
    os.getenv("NFSE_TOMADOR_DOWNLOADS_DIR", "/usr/local/airflow/data/nfse_tomador")
)
ARTIFACTS_DIR = Path(
    os.getenv("NFSE_ARTIFACTS_DIR", "/usr/local/airflow/data/artifacts")
)


@dag(
    dag_id="extracao_nfse_tomador",
    description=(
        "Extrai do ISS Fortaleza as NFS-e RECEBIDAS (Serviços Tomados) pelas "
        "inscrições do grupo e carrega a tabela nfse_xml_tomador (Contas a Pagar)."
    ),
    schedule=os.getenv("NFSE_TOMADOR_EXTRACTION_SCHEDULE", "40 7-19/2 * * 1-6"),
    start_date=pendulum.datetime(2026, 11, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=2),
    },
    tags=["nfse", "extracao", "dlt", "contas_a_pagar"],
)
def extracao_nfse_tomador():
    # Mesmo pool da extracao_nfse: o portal usa um único login, e duas
    # sessões simultâneas derrubam uma à outra.
    @task(task_id="extrair_e_carregar", pool="nfse_portal")
    def extrair_e_carregar() -> dict[str, object]:
        context = get_current_context()
        dag_run = context["dag_run"]
        reference_date = context["data_interval_end"].in_timezone(
            "America/Fortaleza"
        ).date()
        payload = ExtractionPayload.from_mapping(
            dag_run.conf,
            default_date=reference_date,
        )

        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        os.environ.setdefault("DATABASE_URL", hook.get_uri())
        os.environ.setdefault("POSTGRES_SCHEMA", POSTGRES_SCHEMA)
        settings = load_settings()
        summary = extract_and_load_nfse(
            settings,
            payload,
            options=PortalOptions(
                downloads_dir=DOWNLOADS_DIR,
                artifacts_dir=ARTIFACTS_DIR,
                query_date=reference_date,
                visao=VISAO_TOMADOR,
            ),
            table_name=TABLE_NAME,
        )
        return summary.as_dict()

    extrair_e_carregar()


dag = extracao_nfse_tomador()
