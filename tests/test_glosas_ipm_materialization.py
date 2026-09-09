from pathlib import Path

import pytest

from nfs_fortaleza.glosas_ipm_materialization import (
    MATERIALIZAR_RASTREIO_SQL,
    MATERIALIZAR_REGISTROS_SQL,
    RECONCILIAR_REGISTROS_SQL,
    REMOVER_RASTREIOS_OBSOLETOS_SQL,
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
    def __init__(self, rowcounts=(2, 7, 3, 5)):
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
        MATERIALIZAR_REGISTROS_SQL,
        MATERIALIZAR_RASTREIO_SQL,
    ]
    assert resultado == {
        "registros_desativados": 2,
        "rastreios_removidos": 7,
        "registros_glosa": 3,
        "rastreios": 5,
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
