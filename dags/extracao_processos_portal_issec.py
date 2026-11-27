from __future__ import annotations

import os
from datetime import timedelta

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.operators.python import get_current_context
from airflow.providers.postgres.hooks.postgres import PostgresHook

from nfs_fortaleza.issec_auth import (
    IssecInteractiveAuthError,
    authenticate_with_human_captcha,
)
from nfs_fortaleza.issec_portal import (
    DEFAULT_BASE_URL,
    extract_and_load_processos,
)


POSTGRES_CONN_ID = os.getenv("ISSEC_POSTGRES_CONN_ID", "postgres_prontocardio")
AUTH_TIMEOUT_SECONDS = float(os.getenv("ISSEC_AUTH_TIMEOUT_SECONDS", "1800"))


@dag(
    dag_id="extracao_processos_portal_issec",
    description=(
        "Extrai os processos e a página Notas Fiscais (pendentes e pagas) do "
        "portal do credenciado ISSEC e carrega raw_issec_portal com dlt."
    ),
    # Horário comercial: o login depende de um usuário resolver o captcha.
    # Dias úteis às 08:00, 12:00 e 16:00 (pedido de 27/11/2026): pedidos de NF
    # do convênio aparecem em até ~4 h. Cada execução pede captcha humano.
    schedule=os.getenv("ISSEC_PORTAL_SCHEDULE", "0 8,12,16 * * 1-5"),
    start_date=pendulum.datetime(2026, 1, 1, tz="America/Fortaleza"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "airflow",
        "depends_on_past": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=4),
    },
    tags=["issec", "processos", "captcha", "dlt"],
)
def extracao_processos_portal_issec():
    # O pool evita dois desafios de captcha ao mesmo tempo no desktop remoto.
    @task(task_id="extrair_e_carregar", pool="nfse_portal")
    def extrair_e_carregar() -> dict:
        context = get_current_context()
        metadata = {
            "dag_id": context["task_instance"].dag_id,
            "run_id": context["dag_run"].run_id,
            "task_id": context["task_instance"].task_id,
        }
        hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        try:
            rows = hook.get_records(
                """
                SELECT DATE_TRUNC('month', mes_producao)::date
                  FROM raw_issec_portal.processos
                 WHERE mes_producao IS NOT NULL
                 GROUP BY 1
                -- Mês só conta como extraído se todos os processos já têm o
                -- detalhe completo (campos_detalhe com os avisos da página,
                -- de onde vem o NUP do grupo); senão é lido de novo.
                -- Também precisa das guias ("Detalhes de Contas"), lidas desde
                -- 27/11/2026: a primeira execução relê os meses uma vez.
                HAVING COUNT(*) = COUNT(*) FILTER (
                    WHERE campos_detalhe LIKE '%"avisos"%'
                      AND to_jsonb(processos) ->> 'guias' IS NOT NULL
                )
                """
            )
            extracted_months = {row[0] for row in rows}
        except Exception:
            # Primeira execução: o dlt ainda criará schema e tabela.
            extracted_months = set()
        try:
            # Quando cada nota apareceu paga pela primeira vez (o portal não
            # informa a data do pagamento) e quais grupos já foram detalhados.
            pagas_conhecidas = {
                row[0]: row[1]
                for row in hook.get_records(
                    "SELECT nup, primeira_extracao_em::text"
                    "  FROM raw_issec_portal.notas_fiscais_pagas"
                )
            }
        except Exception:
            pagas_conhecidas = {}
        try:
            # Protocolos já registrados no Receita Certa (ISSEC) que ainda não
            # estão nos processos com mês de produção: processos "Em Análise"
            # não aparecem na busca por período, só na busca por número. O
            # número é só a chave de busca; a API confirma o vínculo pelo valor.
            processos_por_numero = [
                row[0]
                for row in hook.get_records(
                    """
                    SELECT DISTINCT p.numero
                      FROM api_prontocardio.cr_protocolos p
                      JOIN api_prontocardio.cr_protocolo_remessas pr ON pr.protocolo_id = p.id
                     WHERE p.cd_convenio = 46
                       AND p.numero ~ '^[0-9]{10}$'
                       AND NOT EXISTS (
                           SELECT 1 FROM raw_issec_portal.processos x
                            WHERE x.processo = p.numero AND x.mes_producao IS NOT NULL)
                     ORDER BY 1
                    """
                )
            ]
        except Exception:
            processos_por_numero = []

        try:
            return extract_and_load_processos(
                username=os.environ["ISSEC_PORTAL_LOGIN"],
                password=os.environ["ISSEC_PORTAL_SENHA"],
                database_url=hook.get_uri(),
                extracted_months=extracted_months,
                authenticate=lambda client: authenticate_with_human_captcha(
                    client,
                    timeout_seconds=AUTH_TIMEOUT_SECONDS,
                    challenge_metadata=metadata,
                ),
                base_url=os.getenv("ISSEC_PORTAL_URL", DEFAULT_BASE_URL),
                timeout=int(os.getenv("ISSEC_PORTAL_TIMEOUT", "30")),
                max_workers=int(os.getenv("ISSEC_PORTAL_WORKERS", "4")),
                pagas_conhecidas=pagas_conhecidas,
                processos_por_numero=processos_por_numero,
            )
        except IssecInteractiveAuthError as auth_error:
            # Sem retry: outra tentativa só abriria um novo desafio sem ninguém.
            raise AirflowFailException(str(auth_error)) from auth_error

    extrair_e_carregar()


dag = extracao_processos_portal_issec()
