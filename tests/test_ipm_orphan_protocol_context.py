from pathlib import Path


MODELS = Path(__file__).parents[1] / 'dbt_glosas_ipm' / 'models'


def test_protocolo_sem_processo_recupera_contexto_por_remessa_unica():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_processos_remessas.sql'
    ).read_text()

    assert 'protocolos_sem_processo as (' in modelo
    assert "status_associacao = 'SEM_PROCESSO'" in modelo
    assert 'rel.cd_remessa = remessa.cd_remessa' in modelo
    assert 'where quantidade_candidatos = 1' in modelo
    assert 'select * from processos_recuperados' in modelo


def test_protocolo_sem_processo_nao_duplica_contexto_ja_associado():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_processos_remessas.sql'
    ).read_text()

    trecho = modelo.split('), processos_recuperados as (', 1)[1]
    trecho = trecho.split('), processos as (', 1)[0]
    assert 'not exists (' in trecho
    assert 'from processos_associados associado' in trecho
    assert (
        'associado.numero_protocolo\n'
        '            = candidatos_sem_processo.numero_protocolo'
        in trecho
    )


def test_protocolo_orfao_prefere_relatorio_mais_recente_da_remessa():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_processos_remessas.sql'
    ).read_text()

    assert 'partition by cd_remessa' in modelo
    assert 'order by extraido_em desc nulls last' in modelo
    assert 'where ordem_remessa = 1' in modelo


def test_protocolo_orfao_herda_competencia_oficial_da_remessa_mv():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_processos_remessas.sql'
    ).read_text()

    assert 'remessa.competencia as competencia_producao' in modelo


def test_associacao_spu_usa_apenas_contexto_mais_recente_da_remessa():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_processos_remessas.sql'
    ).read_text()

    trecho = modelo.split('), candidatos_spu as (', 1)[1]
    trecho = trecho.split('), candidatos_spu_contados as (', 1)[0]
    assert "join relatorios_remessas rel" in trecho
    assert "ref('stg_processos_relatorios_ipm')" not in trecho


def test_contexto_recuperado_restringe_candidatos_a_remessa_correta():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_candidatos_sete_regras.sql'
    ).read_text()

    assert 'r.numero_processo as numero_processo_resolvido' in modelo
    assert 'coalesce(d.valor_protocolo_cogestao, d.valor_protocolo)' in modelo
    assert 'i.cd_remessa = d.cd_remessa_esperada' in modelo


def test_correspondencia_direta_respeita_contexto_do_protocolo():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_candidatos_sete_regras.sql'
    ).read_text()

    assert 'contextos_protocolos as (' in modelo
    trecho = modelo.split('), candidatos_relatorio_brutos as (', 1)[1]
    trecho = trecho.split('), resumo_relatorio as (', 1)[0]
    assert 'contexto.numero_processo_normalizado' in trecho
    assert '= item.numero_processo_normalizado' in trecho
    assert 'contexto.cd_remessa = item.cd_remessa' in trecho


def test_correspondencia_direta_nao_contradiz_processo_do_demonstrativo():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_candidatos_sete_regras.sql'
    ).read_text()

    trecho = modelo.split('), candidatos_relatorio_brutos as (', 1)[1]
    trecho = trecho.split('), resumo_relatorio as (', 1)[0]
    assert "nullif(btrim(d.numero_processo), '') is null" in trecho
    assert '= upper(btrim(d.numero_processo))' in trecho


def test_contexto_canonico_recupera_beneficiario_distorcido_por_guia_servico():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_candidatos_sete_regras.sql'
    ).read_text()

    trecho = modelo.split('), candidatos_relatorio_brutos as (', 1)[1]
    trecho = trecho.split('), resumo_relatorio as (', 1)[0]
    assert "else 'contexto_protocolo_guia_servico'" in trecho
    assert 'contexto.numero_processo_normalizado' in trecho
    assert '= item.numero_processo_normalizado' in trecho
    assert 'contexto.cd_remessa = item.cd_remessa' in trecho
    assert "item.nr_guia_normalizada <> ''" in trecho
    assert 'item.cd_tuss_normalizado' in trecho


def test_fallback_exige_processo_informado_ou_contexto_canonico():
    modelo = (
        MODELS / 'intermediate' / 'int_ipm_candidatos_sete_regras.sql'
    ).read_text()

    assert 'candidatos_fallback_brutos_sem_contexto as (' in modelo
    trecho = modelo.split('), candidatos_fallback_brutos as (', 1)[1]
    trecho = trecho.split('), resumo_fallback as (', 1)[0]
    assert 'join demonstrativos_fallback demonstrativo' in trecho
    assert 'contexto.numero_processo_normalizado' in trecho
    assert '= upper(btrim(candidato.numero_processo_resolvido))' in trecho
    assert 'contexto.cd_remessa = candidato.cd_remessa' in trecho
    assert (
        "nullif(btrim(demonstrativo.numero_processo), '') is not null"
        in trecho
    )
    assert 'not exists (' in trecho


def test_pendencia_usa_processo_recuperado_da_remessa():
    modelo = (
        MODELS / 'marts' / 'glossas_nao_vinculadas_ipm.sql'
    ).read_text()

    assert 'r.numero_processo as numero_processo_remessa' in modelo
    assert 'numero_processo_remessa,' in modelo
    assert 'coalesce(d.valor_protocolo_cogestao, d.valor_protocolo)' in modelo
