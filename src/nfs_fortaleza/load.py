from __future__ import annotations

import os
from pathlib import Path

import dlt
import psycopg2
from psycopg2 import sql

from nfs_fortaleza.config import Settings
from nfs_fortaleza.nfse_xml import (
    NfseIdentity,
    nfse_identity,
    nfse_xml_resource,
)
from nfs_fortaleza.periods import DateRangePeriod, MonthPeriod
from nfs_fortaleza.portal import ListedNfse


QueryPeriod = MonthPeriod | DateRangePeriod


def _destination_table_exists(
    settings: Settings,
    table_name: str,
) -> bool:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = %s
                )
                """,
                (settings.postgres_schema, table_name),
            )
            return bool(cursor.fetchone()[0])


def _existing_nfse_keys(
    settings: Settings,
    table_name: str,
) -> set[NfseIdentity]:
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT codigo_verificacao_nfse, "
                    "prestador_cnpj, numero_nfse "
                    "FROM {}.{} "
                    "WHERE NULLIF(BTRIM(codigo_verificacao_nfse), '') "
                    "IS NOT NULL "
                    "OR (prestador_cnpj IS NOT NULL "
                    "AND numero_nfse IS NOT NULL)"
                ).format(
                    sql.Identifier(settings.postgres_schema),
                    sql.Identifier(table_name),
                )
            )
            identities = {
                identity
                for (
                    codigo_verificacao_nfse,
                    prestador_cnpj,
                    numero_nfse,
                ) in cursor.fetchall()
                if (
                    identity := nfse_identity(
                        codigo_verificacao_nfse,
                        prestador_cnpj=prestador_cnpj,
                        numero_nfse=numero_nfse,
                    )
                )
                is not None
            }
    return identities


def load_nfse_xml(
    settings: Settings,
    file_path: Path,
    competencia: QueryPeriod,
    *,
    table_name: str = "nfse_xml",
):
    os.environ["DESTINATION__POSTGRES__CREDENTIALS"] = settings.database_url

    table_exists = _destination_table_exists(settings, table_name)
    existing_nfse_keys = (
        _existing_nfse_keys(settings, table_name)
        if table_exists
        else set()
    )

    resource = nfse_xml_resource(
        file_path,
        competencia,
        table_name=table_name,
        existing_nfse_keys=tuple(sorted(existing_nfse_keys)),
    )

    pipeline = dlt.pipeline(
        pipeline_name="iss_fortaleza",
        destination="postgres",
        dataset_name=settings.postgres_schema,
    )
    if table_exists:
        return pipeline.run(resource)

    # A table managed by dlt may have been removed directly from PostgreSQL
    # while its schema history remains in _dlt_version. Reset only this
    # resource so dlt emits the CREATE TABLE before running the merge job.
    return pipeline.run(resource, refresh="drop_resources")


def situacao_table_name(table_name: str) -> str:
    return f"{table_name}_situacao"


def upsert_nfse_situacao(
    settings: Settings,
    listed: list[ListedNfse],
    *,
    table_name: str,
) -> int:
    """Grava a situação (NORMAL, CANCELADA...) de cada NFS-e listada no portal.

    O XML exportado não traz o cancelamento e as notas já carregadas não são
    baixadas de novo; por isso a situação fica numa tabela própria, atualizada
    a cada consulta.
    """
    linhas = {
        (item.documento, item.numero_nfse): item
        for item in listed
        if item.documento and item.numero_nfse and item.situacao
    }
    if not linhas:
        return 0
    tabela = sql.Identifier(settings.postgres_schema, situacao_table_name(table_name))
    with psycopg2.connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        prestador_cnpj varchar(14) NOT NULL,
                        numero_nfse varchar(30) NOT NULL,
                        tomador_cnpj varchar(14),
                        situacao varchar(40) NOT NULL,
                        data_emissao date,
                        primeira_consulta_em timestamptz NOT NULL DEFAULT now(),
                        atualizado_em timestamptz NOT NULL DEFAULT now(),
                        situacao_alterada_em timestamptz,
                        PRIMARY KEY (prestador_cnpj, numero_nfse)
                    )
                    """
                ).format(tabela)
            )
            cursor.executemany(
                sql.SQL(
                    """
                    INSERT INTO {} AS t
                        (prestador_cnpj, numero_nfse, tomador_cnpj, situacao, data_emissao)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (prestador_cnpj, numero_nfse) DO UPDATE SET
                        tomador_cnpj = COALESCE(EXCLUDED.tomador_cnpj, t.tomador_cnpj),
                        situacao = EXCLUDED.situacao,
                        data_emissao = COALESCE(EXCLUDED.data_emissao, t.data_emissao),
                        atualizado_em = now(),
                        situacao_alterada_em = CASE
                            WHEN t.situacao IS DISTINCT FROM EXCLUDED.situacao THEN now()
                            ELSE t.situacao_alterada_em
                        END
                    """
                ).format(tabela),
                [
                    (
                        item.documento,
                        item.numero_nfse,
                        item.inscricao_documento,
                        item.situacao,
                        item.data_emissao,
                    )
                    for item in linhas.values()
                ],
            )
    return len(linhas)
