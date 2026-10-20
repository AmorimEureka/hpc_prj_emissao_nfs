"""Integration tests: use a disposable local PostgreSQL database only.

MAIDA_TEST_POSTGRES_DSN=... pytest tests/test_glosas_ipm_context.py
The fixture rolls back its schemas and data after every test.
"""
import os
from decimal import Decimal

import pytest

from nfs_fortaleza.glosas_ipm_context import (
    CONTEXTOS_MAIDA_PENDENTES_SQL, ENRIQUECER_CONTEXTO_MAIDA_SQL,
)


@pytest.fixture
def db():
    dsn = os.environ.get("MAIDA_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Requires disposable local PostgreSQL")
    import psycopg2
    from psycopg2.extensions import parse_dsn
    settings = parse_dsn(dsn)
    assert settings.get("host") in ("127.0.0.1", "localhost")
    assert settings.get("dbname") == "maida_test"
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as c:
            c.execute("""
                CREATE SCHEMA api_prontocardio;
                CREATE SCHEMA api_prontocardio_staging;
                CREATE TABLE api_prontocardio.registros_glosa (
                    id bigint PRIMARY KEY, cd_remessa bigint, conta bigint,
                    cd_atendimento bigint DEFAULT 0, codigo_paciente bigint,
                    nm_paciente text, tp_atendimento text,
                    data_atendimento timestamp, data_alta timestamp,
                    cd_convenio bigint, convenio text DEFAULT 'ISSEC',
                    sn_ativo text DEFAULT 'true',
                    origem_registro text DEFAULT 'triagem',
                    processo_controle_fatura_gab text DEFAULT 'MAIDA-30700',
                    cd_lancamento bigint DEFAULT 1000000001,
                    procedimento text DEFAULT '0000060309',
                    valor numeric DEFAULT 5.60, qtd_registro numeric DEFAULT 1,
                    valor_recursado numeric, dt_recurso date
                );
                CREATE TABLE api_prontocardio.demonstrativo_conta_ipm (
                    id_registro text PRIMARY KEY, numero_lote text,
                    referencia date DEFAULT '2026-09-01',
                    origem_maida text DEFAULT 'lotes',
                    valor_glosa numeric DEFAULT 3.23,
                    numero_guia_senha text DEFAULT '570141',
                    codigo_beneficiario text DEFAULT '001234',
                    nome_beneficiario text, codigo_servico text DEFAULT '0000060309'
                );
                CREATE TABLE api_prontocardio.glosas_ipm_vinculadas (
                    id_registro text, cd_remessa bigint
                );
                CREATE TABLE api_prontocardio.registros_glosa_demonstrativo_ipm (
                    id_registro text PRIMARY KEY, registro_glosa_id bigint
                );
                CREATE TABLE api_prontocardio_staging.ipm_itens_oracle (
                    cd_remessa bigint DEFAULT 19848, cd_reg bigint DEFAULT 25529,
                    cd_atendimento bigint DEFAULT 345774,
                    cd_paciente bigint DEFAULT 32479,
                    nm_paciente text DEFAULT 'Paciente teste',
                    tp_atendimento text DEFAULT 'Internacao',
                    dt_atendimento timestamp DEFAULT '2026-08-17',
                    dt_alta timestamp DEFAULT '2026-08-19',
                    cd_convenio bigint DEFAULT 46,
                    nr_guia text DEFAULT '570141', nr_carteira text DEFAULT '1234',
                    cd_pro_fat text DEFAULT '90328558', cd_tuss text
                );
                CREATE TABLE api_prontocardio_staging.ipm_brasindice_oracle (
                    cd_tiss text PRIMARY KEY, cd_pro_fat text
                );
                INSERT INTO api_prontocardio.registros_glosa(id,cd_remessa,conta)
                    VALUES (1,30700,570141);
                INSERT INTO api_prontocardio.demonstrativo_conta_ipm
                    (id_registro,numero_lote) VALUES ('anchor','30700'),('a','30700'),('b','30700');
                INSERT INTO api_prontocardio.glosas_ipm_vinculadas VALUES ('anchor',19848);
                INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm
                    VALUES ('a',1),('b',1);
                INSERT INTO api_prontocardio_staging.ipm_itens_oracle DEFAULT VALUES;
                INSERT INTO api_prontocardio_staging.ipm_itens_oracle DEFAULT VALUES;
            """)
            yield c
    finally:
        conn.rollback()
        conn.close()


def test_enriches_context_once_preserving_sources_and_treatment(db):
    db.execute("UPDATE api_prontocardio.registros_glosa SET valor_recursado=2.50,dt_recurso='2026-10-20'")
    db.execute(ENRIQUECER_CONTEXTO_MAIDA_SQL)
    assert db.rowcount == 1
    db.execute("""SELECT cd_remessa,conta,cd_atendimento,codigo_paciente,
        processo_controle_fatura_gab,cd_lancamento,procedimento,valor,
        qtd_registro,valor_recursado,dt_recurso::text
        FROM api_prontocardio.registros_glosa""")
    assert db.fetchone() == (19848,25529,345774,32479,'30700',
        1000000001,'0000060309',Decimal('5.60'),1,Decimal('2.50'),'2026-10-20')
    db.execute("SELECT COUNT(*) FROM api_prontocardio.registros_glosa_demonstrativo_ipm")
    assert db.fetchone() == (2,)
    db.execute(ENRIQUECER_CONTEXTO_MAIDA_SQL)
    assert db.rowcount == 0


@pytest.mark.parametrize('change', [
    "INSERT INTO api_prontocardio.glosas_ipm_vinculadas VALUES ('anchor',999)",
    "INSERT INTO api_prontocardio_staging.ipm_itens_oracle(cd_atendimento) VALUES (999)",
    "UPDATE api_prontocardio_staging.ipm_itens_oracle SET nr_carteira='999'",
    "UPDATE api_prontocardio_staging.ipm_itens_oracle SET cd_convenio=10",
    "UPDATE api_prontocardio_staging.ipm_itens_oracle SET nr_guia='999'",
    "UPDATE api_prontocardio.registros_glosa SET sn_ativo='false'",
    "UPDATE api_prontocardio.registros_glosa SET cd_atendimento=345774",
    "UPDATE api_prontocardio.registros_glosa SET processo_controle_fatura_gab='2600033271'",
    "UPDATE api_prontocardio.demonstrativo_conta_ipm SET origem_maida=NULL",
    "UPDATE api_prontocardio.demonstrativo_conta_ipm SET referencia='2026-08-01' WHERE id_registro='anchor'",
    "UPDATE api_prontocardio.demonstrativo_conta_ipm SET numero_guia_senha='999' WHERE id_registro='b'",
    "DELETE FROM api_prontocardio.registros_glosa_demonstrativo_ipm",
    "UPDATE api_prontocardio.registros_glosa SET origem_registro='manual'",
    "UPDATE api_prontocardio.registros_glosa SET cd_lancamento=27",
])
def test_rejects_ambiguous_incomplete_or_nontechnical_context(db, change):
    db.execute(change)
    db.execute(CONTEXTOS_MAIDA_PENDENTES_SQL)
    assert db.fetchall() == []
    db.execute(ENRIQUECER_CONTEXTO_MAIDA_SQL)
    assert db.rowcount == 0


def test_blank_wallet_allowed_only_with_unique_guide_context(db):
    db.execute("UPDATE api_prontocardio.demonstrativo_conta_ipm SET codigo_beneficiario=NULL")
    db.execute(CONTEXTOS_MAIDA_PENDENTES_SQL)
    assert len(db.fetchall()) == 1
    db.execute("INSERT INTO api_prontocardio_staging.ipm_itens_oracle(cd_reg,nr_carteira) VALUES(999,'999')")
    db.execute(CONTEXTOS_MAIDA_PENDENTES_SQL)
    assert db.fetchall() == []


def test_different_wallet_requires_same_patient_and_known_procedure(db):
    db.execute("UPDATE api_prontocardio_staging.ipm_itens_oracle SET nr_carteira='999'")
    db.execute("UPDATE api_prontocardio.demonstrativo_conta_ipm SET nome_beneficiario='Paciente teste'")
    # A name and guide alone cannot bypass a conflicting wallet.
    db.execute(CONTEXTOS_MAIDA_PENDENTES_SQL)
    assert db.fetchall() == []
    db.execute("INSERT INTO api_prontocardio_staging.ipm_brasindice_oracle VALUES ('60309','90328558')")
    db.execute(CONTEXTOS_MAIDA_PENDENTES_SQL)
    assert len(db.fetchall()) == 1
    db.execute("UPDATE api_prontocardio.demonstrativo_conta_ipm SET nome_beneficiario='Outra pessoa'")
    db.execute(CONTEXTOS_MAIDA_PENDENTES_SQL)
    assert db.fetchall() == []


@pytest.mark.parametrize("enriched", [False, True])
def test_later_exact_match_migrates_treatment_without_duplication(db, enriched):
    from nfs_fortaleza.glosas_ipm_materialization import (
        CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL,
    )
    for field, kind in {
        "processo_recurso": "text", "qtd_recursado": "numeric",
        "dt_pagamento": "date", "dt_recebimento": "date",
        "valor_recebido": "numeric", "qtd_recebida": "numeric",
        "observacao_recebimento": "text", "descricao_glosa": "text",
        "descricao_glosa_agrupada": "text", "descricao_recurso_agrupada": "text",
        "descricao_acato_agrupada": "text", "numero_lote": "text",
        "guia": "text", "motivo_glosa": "text",
    }.items():
        db.execute(f"ALTER TABLE api_prontocardio.registros_glosa ADD COLUMN {field} {kind}")
    db.execute("ALTER TABLE api_prontocardio.registros_glosa_demonstrativo_ipm ADD COLUMN criterio_correspondencia text")
    db.execute("ALTER TABLE api_prontocardio.demonstrativo_conta_ipm ADD COLUMN codigo_glosa text DEFAULT '1705'")
    db.execute("""UPDATE api_prontocardio.registros_glosa
        SET valor_recursado=2.50,dt_recurso='2026-10-20',numero_lote='30700',
            origem_registro='triagem',guia='570141',motivo_glosa='1705'""")
    if enriched:
        db.execute(ENRIQUECER_CONTEXTO_MAIDA_SQL)
        assert db.rowcount == 1
    db.execute("""INSERT INTO api_prontocardio.registros_glosa
        (id,cd_remessa,conta,cd_atendimento,cd_lancamento,guia,motivo_glosa)
        VALUES (2,19848,25529,345774,27,'570141','1705')""")
    # Exact matching on a later import has moved both source traces to ID 2.
    db.execute("""UPDATE api_prontocardio.registros_glosa_demonstrativo_ipm
        SET registro_glosa_id=2,criterio_correspondencia='maida_hpc_guia_codigo_item'""")
    db.execute(CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL)
    assert db.rowcount == 1
    db.execute("SELECT id,sn_ativo,valor_recursado FROM api_prontocardio.registros_glosa ORDER BY id")
    assert db.fetchall() == [(1,'false',Decimal('2.50')),(2,'true',Decimal('2.50'))]
    db.execute(CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL)
    assert db.rowcount == 0
