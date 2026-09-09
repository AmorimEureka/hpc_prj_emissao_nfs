with processos_associados as (
    select distinct
        numero_processo,
        competencia_producao,
        upper(btrim(numero_protocolo)) as numero_protocolo,
        round(valor_protocolo_cogestao::numeric, 2) as valor_protocolo
    from {{ ref('stg_demonstrativo_processos_ipm') }}
    where status_associacao like 'ASSOCIADO%'
), protocolos_sem_processo as (
    select distinct
        upper(btrim(numero_protocolo)) as numero_protocolo,
        to_char(data_realizacao, 'MM/YYYY') as competencia_producao,
        round(valor_protocolo::numeric, 2) as valor_protocolo
    from {{ ref('stg_demonstrativo_processos_ipm') }}
    where status_associacao = 'SEM_PROCESSO'
      and nullif(btrim(numero_protocolo), '') is not null
      and valor_protocolo is not null
      and coalesce(valor_glosa_protocolo, 0) > 0
), relatorios_remessas_ordenados as (
    select
        numero_processo,
        numero_processo_normalizado,
        cd_remessa,
        row_number() over (
            partition by cd_remessa
            order by extraido_em desc nulls last,
                     numero_processo_normalizado desc
        ) as ordem_remessa
    from {{ ref('stg_processos_relatorios_ipm') }}
    where nullif(btrim(numero_processo), '') is not null
      and cd_remessa is not null
), relatorios_remessas as (
    select numero_processo, numero_processo_normalizado, cd_remessa
    from relatorios_remessas_ordenados
    where ordem_remessa = 1
), candidatos_sem_processo as (
    select distinct
        rel.numero_processo,
        remessa.competencia as competencia_producao,
        protocolo.numero_protocolo,
        protocolo.valor_protocolo,
        remessa.cd_remessa,
        remessa.nm_convenio,
        remessa.cnpj_convenio,
        round(remessa.valor_total::numeric, 2) as valor_total_remessa,
        count(*) over (
            partition by protocolo.numero_protocolo,
                         protocolo.competencia_producao,
                         protocolo.valor_protocolo
        ) as quantidade_candidatos
    from protocolos_sem_processo protocolo
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} remessa
      on round(remessa.valor_total::numeric, 2)
         = protocolo.valor_protocolo
    join relatorios_remessas rel
      on rel.cd_remessa = remessa.cd_remessa
), processos_recuperados as (
    select
        numero_processo,
        competencia_producao,
        numero_protocolo,
        valor_protocolo
from candidatos_sem_processo
where quantidade_candidatos = 1
  and not exists (
      select 1
      from processos_associados associado
      where associado.numero_protocolo
            = candidatos_sem_processo.numero_protocolo
  )
), processos as (
    select * from processos_associados
    union
    select * from processos_recuperados
), manuais as (
    select
        upper(btrim(numero_processo)) as numero_processo_normalizado,
        btrim(competencia_producao) as competencia_producao,
        upper(btrim(nr)) as numero_protocolo,
        cd_remessa
    from {{ source('prontocardio', 'associacoes_remessas_ipm_manuais') }}
), candidatos_spu as (
    select distinct
        p.numero_processo,
        p.competencia_producao,
        p.numero_protocolo,
        p.valor_protocolo,
        r.cd_remessa,
        r.nm_convenio,
        r.cnpj_convenio,
        round(r.valor_total::numeric, 2) as valor_total_remessa
    from processos p
    join relatorios_remessas rel
      on rel.numero_processo_normalizado = upper(btrim(p.numero_processo))
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} r
      on r.cd_remessa = rel.cd_remessa
     and round(r.valor_total::numeric, 2) = p.valor_protocolo
), candidatos_spu_contados as (
    select candidato.*,
           count(*) over (
               partition by numero_processo, competencia_producao,
                            numero_protocolo, valor_protocolo
           ) as quantidade_candidatos
    from candidatos_spu candidato
), candidatos_valor_competencia as (
    select
        p.numero_processo,
        p.competencia_producao,
        p.numero_protocolo,
        p.valor_protocolo,
        r.cd_remessa,
        r.nm_convenio,
        r.cnpj_convenio,
        round(r.valor_total::numeric, 2) as valor_total_remessa,
        count(*) over (
            partition by p.numero_processo, p.competencia_producao,
                         p.numero_protocolo, p.valor_protocolo
        ) as quantidade_candidatos
    from processos p
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} r
      on r.competencia = p.competencia_producao
     and round(r.valor_total::numeric, 2) = p.valor_protocolo
), automaticos_spu as (
    select candidato.*, 'automatica_spu'::text as origem_associacao
    from candidatos_spu_contados candidato
    where candidato.quantidade_candidatos = 1
      and not exists (
          select 1
          from manuais manual
          where manual.numero_processo_normalizado
                = upper(btrim(candidato.numero_processo))
            and manual.competencia_producao
                = candidato.competencia_producao
            and manual.numero_protocolo = candidato.numero_protocolo
      )
), automaticos_valor_competencia as (
    select candidato.*, 'automatica'::text as origem_associacao
    from candidatos_valor_competencia candidato
    where candidato.quantidade_candidatos = 1
      and not exists (
          select 1
          from candidatos_spu candidato_spu
          where upper(btrim(candidato_spu.numero_processo))
                = upper(btrim(candidato.numero_processo))
            and candidato_spu.competencia_producao
                = candidato.competencia_producao
            and candidato_spu.numero_protocolo
                = candidato.numero_protocolo
            and candidato_spu.valor_protocolo = candidato.valor_protocolo
      )
      and not exists (
          select 1
          from manuais manual
          where manual.numero_processo_normalizado
                = upper(btrim(candidato.numero_processo))
            and manual.competencia_producao
                = candidato.competencia_producao
            and manual.numero_protocolo = candidato.numero_protocolo
      )
), automaticos as (
    select * from automaticos_spu
    union all
    select * from automaticos_valor_competencia
), resolvidos_manuais as (
    select distinct
        p.numero_processo,
        p.competencia_producao,
        p.numero_protocolo,
        p.valor_protocolo,
        r.cd_remessa,
        r.nm_convenio,
        r.cnpj_convenio,
        round(r.valor_total::numeric, 2) as valor_total_remessa,
        1::bigint as quantidade_candidatos,
        'manual'::text as origem_associacao
    from manuais manual
    join (
        select distinct numero_processo, competencia_producao,
                        numero_protocolo, valor_protocolo
        from processos
    ) p
      on upper(btrim(p.numero_processo))
         = manual.numero_processo_normalizado
     and p.competencia_producao = manual.competencia_producao
     and p.numero_protocolo = manual.numero_protocolo
    join {{ source('oracle_stage', 'ipm_remessas_oracle') }} r
      on r.cd_remessa = manual.cd_remessa
)
select * from automaticos
union all
select * from resolvidos_manuais
