from nfs_fortaleza.glosas_ipm_context import ENRIQUECER_CONTEXTO_MAIDA_SQL

from pathlib import Path

import pytest

from nfs_fortaleza.glosas_ipm_materialization import (
    CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL,
    MATERIALIZAR_MAIDA_PENDENTE_SQL,
    MATERIALIZAR_RASTREIO_MAIDA_PENDENTE_SQL,
    MATERIALIZAR_RASTREIO_SQL,
    MATERIALIZAR_REGISTROS_SQL,
    RECONCILIAR_REGISTROS_SQL,
    REMOVER_RASTREIOS_OBSOLETOS_SQL,
    VINCULAR_TRATATIVAS_MANUAIS_MAIDA_SQL,
    materializar_registros_glosa,
)


class CursorFake:
    def __init__(self, rowcounts):
        self.rowcounts = iter(rowcounts)
        self.rowcount = -1
        self.comandos = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, comando):
        self.comandos.append(comando)
        self.rowcount = next(self.rowcounts)


class PostgresFake:
    def __init__(self, rowcounts=(2, 7, 11, 3, 5, 17, 13, 13, 5)):
        self.cursor_fake = CursorFake(rowcounts)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_fake

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_materializa_registros_e_rastreios_em_uma_transacao():
    postgres = PostgresFake()

    resultado = materializar_registros_glosa(postgres)

    assert postgres.cursor_fake.comandos == [
        RECONCILIAR_REGISTROS_SQL,
        REMOVER_RASTREIOS_OBSOLETOS_SQL,
        VINCULAR_TRATATIVAS_MANUAIS_MAIDA_SQL,
        MATERIALIZAR_REGISTROS_SQL,
        MATERIALIZAR_RASTREIO_SQL,
        CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL,
        MATERIALIZAR_MAIDA_PENDENTE_SQL,
        MATERIALIZAR_RASTREIO_MAIDA_PENDENTE_SQL,
        ENRIQUECER_CONTEXTO_MAIDA_SQL,
    ]
    assert resultado == {
        "registros_desativados": 2,
        "rastreios_removidos": 7,
        "tratativas_manuais_maida": 11,
        "registros_glosa": 3,
        "rastreios": 5,
        "registros_maida_consolidados": 17,
        "registros_maida_pendentes": 13,
        "rastreios_maida_pendentes": 13,
        "contextos_maida_enriquecidos": 5,
    }
    assert postgres.commits == 1
    assert postgres.rollbacks == 0


def test_desfaz_transacao_quando_materializacao_falha():
    postgres = PostgresFake(rowcounts=())

    with pytest.raises(StopIteration):
        materializar_registros_glosa(postgres)

    assert postgres.commits == 0
    assert postgres.rollbacks == 1


def test_materializacao_preserva_tratativas_e_e_idempotente():
    reconciliacao = " ".join(RECONCILIAR_REGISTROS_SQL.upper().split())
    remocao = " ".join(REMOVER_RASTREIOS_OBSOLETOS_SQL.upper().split())
    registros = " ".join(MATERIALIZAR_REGISTROS_SQL.upper().split())
    rastreios = " ".join(MATERIALIZAR_RASTREIO_SQL.upper().split())

    assert "WHERE NOT EXISTS" in registros
    assert "EXISTENTE.SN_ATIVO = 'TRUE'" in registros
    assert (
        "ON CONFLICT ON CONSTRAINT UQ_REGISTRO_GLOSA_CONCILIACAO_ITEM "
        "DO UPDATE" in registros
    )
    assert "SN_ATIVO = 'TRUE'" in registros
    assert "ON CONFLICT (ID_REGISTRO) DO UPDATE" in rastreios
    assert "ORDER BY (ITEM.DT_RECURSO IS NULL) DESC" in rastreios
    assert "REGISTRO.QTD_RECURSADO IS NULL" in reconciliacao
    assert "REGISTRO.VALOR_RECURSADO IS NULL" in reconciliacao
    assert "REGISTRO.DT_RECURSO IS NULL" in reconciliacao
    assert "REGISTRO.DT_PAGAMENTO IS NULL" in reconciliacao
    assert "RASTREADOS_VIGENTES" in reconciliacao
    assert "DELETE FROM" in remocao
    assert "REGISTRO.ORIGEM_REGISTRO IN ('TRIAGEM', 'CONCILIACAO')" in remocao
    assert "ATUAL.ID_REGISTRO = RASTREIO.ID_REGISTRO" in remocao


def test_maida_pendente_vincula_manual_ou_cria_item_tratavel():
    manual = " ".join(VINCULAR_TRATATIVAS_MANUAIS_MAIDA_SQL.upper().split())
    pendente = " ".join(MATERIALIZAR_MAIDA_PENDENTE_SQL.upper().split())
    rastreio = " ".join(
        MATERIALIZAR_RASTREIO_MAIDA_PENDENTE_SQL.upper().split()
    )

    assert "UPPER(BTRIM(REGISTRO.GUIA)) = PENDENTE.GUIA" in manual
    assert "PENDENTE.CODIGO_ITEM IN" in manual
    assert "WHERE QUANTIDADE = 1" in manual
    assert "'MAIDA-' ||" in pendente
    assert "ITEM.VALOR_GLOSA" in pendente
    assert "ITEM.QUANTIDADE_EXECUTADA" in pendente
    assert "AND NOT EXISTS" in pendente
    assert "MAIDA_PENDENTE_LOTE_GUIA_CODIGO_ITEM" in rastreio
    assert "ON CONFLICT (ID_REGISTRO) DO NOTHING" in rastreio


def test_maida_enriquecida_consolida_fallback_sem_perder_tratativa():
    consolidacao = " ".join(
        CONSOLIDAR_REGISTROS_MAIDA_ENRIQUECIDOS_SQL.upper().split()
    )

    assert "RASTREIO.CRITERIO_CORRESPONDENCIA LIKE 'MAIDA_HPC_%'" in consolidacao
    assert "DESTINOS_POR_LEGADO = 1" in consolidacao
    assert "TRATATIVAS_POR_DESTINO <= 1" in consolidacao
    assert "DESTINO_POSSUI_TRATATIVA::INTEGER" in consolidacao
    assert "PROCESSO_RECURSO = COALESCE(FONTE.PROCESSO_RECURSO" in consolidacao
    assert "QTD_RECURSADO = COALESCE(FONTE.QTD_RECURSADO" in consolidacao
    assert "VALOR_RECURSADO = COALESCE(FONTE.VALOR_RECURSADO" in consolidacao
    assert "DT_RECURSO = COALESCE(FONTE.DT_RECURSO" in consolidacao
    assert "SET SN_ATIVO = 'FALSE'" in consolidacao


def test_materializacao_usa_mesmo_destino_para_ambos_os_status():
    registros = " ".join(MATERIALIZAR_REGISTROS_SQL.upper().split())
    rastreios = " ".join(MATERIALIZAR_RASTREIO_SQL.upper().split())

    assert "LEFT JOIN VINCULOS" in registros
    assert "THEN 'TRIAGEM' ELSE 'CONCILIACAO'" in registros
    assert "LEFT JOIN VINCULOS" in rastreios
    assert (
        "ITEM.CONCILIACAO_REMESSA_ID IS NOT DISTINCT FROM "
        "VINCULOS.CONCILIACAO_REMESSA_ID"
    ) in rastreios


def test_mart_nao_anexa_glosa_ao_primeiro_lancamento_da_conta():
    raiz = Path(__file__).parents[1] / "dbt_glosas_ipm" / "models"
    resolucao = (
        raiz / "intermediate" / "int_ipm_glosas_resolvidas.sql"
    ).read_text()
    mart = (
        raiz / "marts" / "processos_relatorios_itens_ipm.sql"
    ).read_text()

    assert "case when quantidade_itens = 1 then cd_lancamento" not in resolucao
    assert "glosa.cd_lancamento = item.cd_lancamento" in mart
    assert "item.ordem_item_conta = 1" not in mart
    assert "nullif(glosa.codigo_servico, '')" in mart
    assert "nullif(glosa.descricao_servico, '')" in mart


def test_fallback_nao_associa_beneficiarios_diferentes():
    modelo = (
        Path(__file__).parents[1]
        / "dbt_glosas_ipm"
        / "models"
        / "intermediate"
        / "int_ipm_candidatos_sete_regras.sql"
    ).read_text()

    regra_15 = modelo.split(
        "select 15, 'relatorio_hpc_competencia_servico_valor'",
        1,
    )[1].split("union all", 1)[0]
    regra_16 = modelo.split(
        "select 16, 'relatorio_hpc_atendimento_guia_servico_valor'",
        1,
    )[1].split("union all", 1)[0]
    regra_legada_5 = modelo.split(
        "select d.prioridade_origem + 5,",
        1,
    )[1].split("union all", 1)[0]
    regra_legada_6 = modelo.split(
        "select d.prioridade_origem + 6,",
        1,
    )[1].split("union all", 1)[0]

    for regra in (regra_15, regra_16, regra_legada_5, regra_legada_6):
        assert (
            "i.nr_carteira_normalizada = d.carteira_normalizada" in regra
        )
def test_fallback_nao_contradiz_guia_quando_ambas_estao_preenchidas():
    modelo = (
        Path(__file__).parents[1]
        / "dbt_glosas_ipm"
        / "models"
        / "intermediate"
        / "int_ipm_candidatos_sete_regras.sql"
    ).read_text()
    protecao = (
        "d.guia_normalizada = ''",
        "i.nr_guia_normalizada = ''",
        "i.nr_guia_normalizada = d.guia_normalizada",
    )

    for prioridade in (12, 13, 14, 15, 17):
        inicio = f"select {prioridade}, 'relatorio_hpc_"
        regra = modelo.split(inicio, 1)[1].split("union all", 1)[0]
        assert all(trecho in regra for trecho in protecao)


def test_maida_usa_chave_tripla_ou_guia_item_sem_carteira():
    raiz = Path(__file__).parents[1] / "dbt_glosas_ipm" / "models"
    modelo = (
        raiz / "intermediate" / "int_ipm_candidatos_sete_regras.sql"
    ).read_text()
    regra = modelo.split("candidatos_maida_brutos as (", 1)[1].split(
        "), resumo_maida", 1
    )[0]

    assert "maida_hpc_carteira_guia_codigo_item" in regra
    assert "maida_hpc_guia_codigo_item_sem_carteira" in regra
    assert "when d.carteira_normalizada = ''" in regra
    assert "d.carteira_normalizada = ''" in regra
    assert "i.nr_carteira_normalizada = d.carteira_normalizada" in regra
    assert "coalesce(i.nr_guia_normalizada, '')" in regra
    assert "coalesce(i.cd_pro_fat_normalizado, '')" in regra
    assert "coalesce(i.cd_tuss_normalizado, '')" in regra
    assert "regexp_replace" in modelo
    assert "'[^0-9]', '', 'g'" in modelo
    assert "valor_item =" not in regra

    staging = (
        raiz / "staging" / "stg_demonstrativo_processos_ipm.sql"
    ).read_text()
    assert "source('prontocardio', 'demonstrativo_conta_ipm')" in staging
    assert "where origem_maida is not null" in staging
    assert "maida.origem_maida is not null" in staging
    assert "chave_maida" not in staging
    assert (
        "maida.id_registro::text = demonstrativo_processos_ipm.id_registro::text"
        in staging
    )

    for prioridade in (11, 16):
        inicio = f"select {prioridade} as prioridade" if prioridade == 11 else (
            f"select {prioridade}, 'relatorio_hpc_"
        )
        regra = modelo.split(inicio, 1)[1].split("union all", 1)[0]
        assert "i.nr_guia_normalizada = d.guia_normalizada" in regra


def test_pendencia_herda_contexto_unico_do_mesmo_protocolo():
    modelo = (
        Path(__file__).parents[1]
        / "dbt_glosas_ipm"
        / "models"
        / "marts"
        / "glossas_nao_vinculadas_ipm.sql"
    ).read_text()

    assert "ref('glosas_ipm_vinculadas')" in modelo
    assert "where quantidade_contextos = 1" in modelo
    assert "coalesce(r.cd_remessa, contexto.cd_remessa)" in modelo
    assert "numero_processo_remessa" in modelo
    assert "numero_processo_protocolo" in modelo
