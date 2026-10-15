from datetime import date

from nfs_fortaleza.glosas_ipm_stage import (
    ITENS_SQL,
    REMESSAS_SQL,
    _consulta_com_escopo,
    carregar_hpc_intermediaria,
)


class CursorPostgres:
    def __init__(
        self,
        periodo=(date(2026, 1, 1), date(2026, 1, 31)),
        guias=(),
    ):
        self.comandos = []
        self.periodo = periodo
        self.proxima_linha = None
        self.guias = guias

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, comando, _parametros=None):
        self.comandos.append(comando)
        texto = str(comando)
        if "SELECT EXISTS" in texto:
            self.proxima_linha = (False,)
        elif "SELECT MIN(data_referencia)" in texto:
            self.proxima_linha = self.periodo
        elif "array_agg(DISTINCT BTRIM(numero_guia_senha::text))" in texto:
            self.proxima_linha = (self.guias,)

    def fetchone(self):
        return self.proxima_linha


def _comando_normalizado(comando) -> str:
    return " ".join(str(comando).split())


class ConexaoPostgres:
    def __init__(
        self,
        periodo=(date(2026, 1, 1), date(2026, 1, 31)),
        guias=(),
    ):
        self.destino = CursorPostgres(periodo, guias)
        self.commits = 0

    def cursor(self):
        return self.destino

    def commit(self):
        self.commits += 1


class CursorOracle:
    description = (("CD_REMESSA",),)

    def __init__(self):
        self.consulta = ""
        self.parametros = {}

    def execute(self, consulta, parametros=None):
        self.consulta = str(consulta)
        self.parametros = parametros or {}
        return None

    def fetchmany(self, _):
        return []

    def close(self):
        return None


class ConexaoOracle:
    def __init__(self):
        self.cursores = []

    def cursor(self):
        cursor = CursorOracle()
        self.cursores.append(cursor)
        return cursor


def test_cria_tabela_de_itens_antes_de_alterar_colunas():
    postgres = ConexaoPostgres()

    resultado = carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [_comando_normalizado(item) for item in postgres.destino.comandos]
    indice_create = next(
        indice
        for indice, comando in enumerate(comandos)
        if "CREATE TABLE IF NOT EXISTS" in comando
        and "ipm_itens_oracle" in comando
    )
    indice_alter = next(
        indice
        for indice, comando in enumerate(comandos)
        if "ALTER TABLE api_prontocardio_staging.ipm_itens_oracle" in comando
    )

    assert indice_create < indice_alter
    assert resultado == {"remessas": 0, "itens": 0}
    assert postgres.commits == 1


def test_garante_tabela_de_relatorios_antes_da_execucao_dbt():
    postgres = ConexaoPostgres()

    carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [_comando_normalizado(item) for item in postgres.destino.comandos]
    assert any(
        "CREATE TABLE IF NOT EXISTS" in comando
        and "processos_relatorios_ipm" in comando
        and "_dlt_load_id" in comando
        and "_dlt_id" in comando
        for comando in comandos
    )


def test_periodo_hpc_considera_demonstrativo_e_relatorios_spu():
    postgres = ConexaoPostgres()

    carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    comandos = [_comando_normalizado(item) for item in postgres.destino.comandos]
    consulta_periodo = next(
        comando
        for comando in comandos
        if "SELECT MIN(data_referencia), MAX(data_referencia)" in comando
    )
    assert "demonstrativo_processos_ipm" in consulta_periodo
    assert "demonstrativo_conta_ipm" in consulta_periodo
    assert "origem_maida IS NOT NULL" in consulta_periodo
    assert "processos_relatorios_ipm" in consulta_periodo
    assert consulta_periodo.count("DATE '2024-01-01'") == 3
    assert consulta_periodo.count("INTERVAL '2 months'") == 3
    assert "status_processo IN ('FINALIZADO', 'TRAMITANDO')" in consulta_periodo


def test_confirma_tabela_de_relatorios_sem_periodo_do_demonstrativo():
    postgres = ConexaoPostgres(periodo=(None, None))

    resultado = carregar_hpc_intermediaria(ConexaoOracle(), postgres)

    assert resultado == {"remessas": 0, "itens": 0}
    assert postgres.commits == 1


def test_total_da_remessa_soma_registros_consolidados_por_conta():
    consulta = " ".join(REMESSAS_SQL.upper().split())

    assert "MAX(VL_TOTAL_CONTA)" not in consulta
    assert "MAX(VL_TOTAL_REGISTRO) AS VL_TOTAL_REGISTRO" in consulta
    assert "SUM(VL_TOTAL_REGISTRO) AS VALOR_TOTAL" in consulta
    assert "GROUP BY CD_REMESSA, CD_REG" in consulta


def test_consulta_de_itens_usa_guias_maida_em_qualquer_convenio():
    consulta, parametros = _consulta_com_escopo(
        ITENS_SQL, ("412690", "413462")
    )

    consulta = _comando_normalizado(consulta)
    assert "h.cd_convenio IN (:convenio_base_0)" in consulta
    assert "h.nr_guia IN (:guia_0, :guia_1)" in consulta
    assert parametros == {
        "convenio_base_0": 10,
        "guia_0": "412690",
        "guia_1": "413462",
    }


def test_guias_sao_divididas_em_lotes_compativeis_com_oracle():
    guias = tuple(str(numero) for numero in range(501))

    consulta, _ = _consulta_com_escopo(ITENS_SQL, guias)

    assert consulta.count("h.nr_guia IN (") == 2
    assert ":guia_0" in consulta
    assert ":guia_500" in consulta


def test_carga_limita_outros_convenios_as_guias_maida():
    postgres = ConexaoPostgres(guias=("412690",))
    oracle = ConexaoOracle()

    carregar_hpc_intermediaria(oracle, postgres)

    consultas_carga = [
        cursor
        for cursor in oracle.cursores
        if "data_inicial" in cursor.parametros
    ]
    assert len(consultas_carga) == 2
    assert all(
        cursor.parametros["convenio_base_0"] == 10
        for cursor in consultas_carga
    )
    consulta_itens = next(
        cursor for cursor in consultas_carga if "guia_0" in cursor.parametros
    )
    assert consulta_itens.parametros["guia_0"] == "412690"
    assert "h.nr_guia IN (:guia_0)" in consulta_itens.consulta
    assert all("__ESCOPO" not in cursor.consulta for cursor in consultas_carga)


def test_item_guide_is_used_before_scope_filter_without_mixing_accounts():
    import sqlite3
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS dbamv")
    fields = ['cd_remessa', 'cd_reg', 'cd_lancamento', 'cd_atendimento',
        'cd_paciente', 'nm_paciente', 'cd_prestador', 'nm_prestador',
        'cd_convenio', 'cnpj_convenio', 'nm_convenio', 'tp_atendimento',
        'nr_guia', 'nr_carteira', 'cd_pro_fat', 'cd_tuss', 'descricao',
        'dt_atendimento', 'dt_alta', 'dt_competencia', 'dt_lancamento',
        'qt_lancamento', 'vl_total_conta', 'cd_gru_fat', 'ds_gru_fat']
    db.execute('CREATE TABLE dbamv.hpc_v_conta_atendimento (' + ','.join(fields) + ')')
    db.execute('CREATE TABLE dbamv.itreg_amb (cd_reg_amb,cd_lancamento,cd_atendimento,cd_pro_fat,cd_guia)')
    db.execute('CREATE TABLE dbamv.itreg_fat (cd_reg_fat,cd_lancamento,cd_pro_fat,cd_guia)')
    db.execute('CREATE TABLE dbamv.guia (cd_guia,nr_guia)')
    db.execute('CREATE TABLE dbamv.pro_fat (cd_pro_fat,cd_gru_pro)')
    db.execute('CREATE TABLE dbamv.gru_pro (cd_gru_pro,ds_gru_pro)')
    def add(account, kind, guide, convenio=46):
        row = dict(cd_remessa=19877,cd_reg=account,cd_lancamento=11,
            cd_atendimento=344949,cd_convenio=convenio,tp_atendimento=kind,
            nr_guia=guide,cd_pro_fat='41001230',dt_competencia='2026-08-01',
            vl_total_conta=580.95)
        db.execute('INSERT INTO dbamv.hpc_v_conta_atendimento VALUES (' + ','.join('?' for _ in fields) + ')', [row.get(f) for f in fields])
    add(420579, 'Externo', '567274')
    add(420579, 'Internação', 'outra')
    add(420580, 'Externo', '568383')
    add(420581, 'Externo', 'original', 10)
    db.execute("INSERT INTO dbamv.itreg_amb VALUES (420579,11,344949,'41001230',664858)")
    db.execute("INSERT INTO dbamv.itreg_amb VALUES (420581,11,344949,'41001230',664858)")
    db.execute("INSERT INTO dbamv.guia VALUES (664858,'567350')")
    sql,params = _consulta_com_escopo(ITENS_SQL, ('567350','568383'))
    rows = list(db.execute(sql,{**params,'data_inicial':'2026-08-01','data_final':'2026-09-01'}))
    assert [(r['cd_reg'],r['nr_guia']) for r in rows] == [(420579,'567350'),(420580,'568383'),(420581,'original')]
    assert rows[0]['vl_total_conta'] == 580.95
    db.close()


def test_brasindice_requires_unique_procedure_and_deduplicates_catalog_rows():
    import re
    import sqlite3
    from nfs_fortaleza.glosas_ipm_stage import BRASINDICE_SQL
    db = sqlite3.connect(':memory:')
    db.create_function('REGEXP_LIKE', 2, lambda value, pattern: bool(value and re.fullmatch(pattern, value)))
    db.execute("ATTACH DATABASE ':memory:' AS dbamv")
    db.execute('CREATE TABLE dbamv.imp_bra (cd_pro_fat TEXT,cd_tiss TEXT)')
    db.executemany('INSERT INTO dbamv.imp_bra VALUES (?,?)', [
        ('90425405','0000078358'), ('90425405','0000078358'),
        ('A','00123'), ('B','123'), ('C','invalid'), ('D','000'),
    ])
    assert db.execute(BRASINDICE_SQL).fetchall() == [('90425405','78358')]
    db.close()


def test_brasindice_fallback_matches_only_same_procedure_and_issec():
    import sqlite3
    from pathlib import Path
    sql = (Path(__file__).parents[1] / 'dbt_glosas_ipm/models/intermediate/int_ipm_candidatos_sete_regras.sql').read_text()
    clause = sql.split('or (i.cd_convenio = 46 and exists (', 1)[1].split(')))', 1)[0]
    clause = clause.replace("{{ source('oracle_stage', 'ipm_brasindice_oracle') }}", 'mapping')
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE mapping (cd_tiss TEXT, cd_pro_fat TEXT)')
    db.execute("INSERT INTO mapping VALUES ('78358','90425405')")
    query = ('SELECT i.cd_convenio = 46 AND EXISTS (' + clause +
        ') FROM (SELECT ? cd_convenio, ? cd_pro_fat_normalizado) i '
        'CROSS JOIN (SELECT ? servico_normalizado) d')
    assert db.execute(query, (46,'90425405','78358')).fetchone()[0] == 1
    assert db.execute(query, (10,'90425405','78358')).fetchone()[0] == 0
    assert db.execute(query, (46,'999','78358')).fetchone()[0] == 0
    assert db.execute(query, (46,'90425405','999')).fetchone()[0] == 0
    db.close()
