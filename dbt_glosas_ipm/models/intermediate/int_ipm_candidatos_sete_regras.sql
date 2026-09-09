with demonstrativos_legado as (
    select
        d.*,
        r.cd_remessa as cd_remessa_esperada,
        null::bigint as conta_esperada,
        r.numero_processo as numero_processo_resolvido,
        20 as prioridade_origem,
        r.origem_associacao,
        to_char(d.data_realizacao, 'YYYY-MM') as mes_realizacao,
        upper(btrim(coalesce(d.numero_guia_senha, ''))) as guia_normalizada,
        upper(btrim(coalesce(d.codigo_servico, ''))) as servico_normalizado,
        ltrim(
            regexp_replace(coalesce(d.codigo_beneficiario, ''), '[^0-9]', '', 'g'),
            '0'
        ) as carteira_normalizada,
        round(coalesce(d.valor_processado, 0)::numeric, 2) as valor_normalizado
    from {{ ref('stg_demonstrativo_processos_ipm') }} d
    join {{ ref('int_ipm_processos_remessas') }} r
      on (
            d.numero_processo is null
            or r.numero_processo = d.numero_processo
         )
     and (
            d.competencia_producao is null
            or r.competencia_producao = d.competencia_producao
         )
     and r.valor_protocolo
         = round(
             coalesce(d.valor_protocolo_cogestao, d.valor_protocolo)::numeric,
             2
         )
     and r.numero_protocolo = upper(btrim(d.numero_protocolo))
), contextos_protocolos as (
    select distinct
        upper(btrim(numero_protocolo)) as numero_protocolo,
        upper(btrim(numero_processo)) as numero_processo_normalizado,
        cd_remessa
    from {{ ref('int_ipm_processos_remessas') }}
), candidatos_relatorio_brutos as (
    select distinct
           case
               when ltrim(
                       regexp_replace(
                           coalesce(d.codigo_beneficiario, ''),
                           '[^0-9]', '', 'g'
                       ),
                       '0'
                    ) <> ''
                and item.nr_carteira_normalizada = ltrim(
                       regexp_replace(
                           coalesce(d.codigo_beneficiario, ''),
                           '[^0-9]', '', 'g'
                       ),
                       '0'
                    )
               then 1 else 2
           end as prioridade,
           case
               when item.nr_carteira_normalizada = ltrim(
                       regexp_replace(
                           coalesce(d.codigo_beneficiario, ''),
                           '[^0-9]', '', 'g'
                       ),
                       '0'
                    )
               then 'relatorio_hpc_guia_beneficiario_servico'
               else 'contexto_protocolo_guia_servico'
           end::text as criterio,
           d.id_registro,
           item.numero_processo as numero_processo_resolvido,
           item.cd_remessa,
           item.conta,
           item.cd_lancamento,
           item.cd_atendimento,
           item.cd_paciente,
           item.nm_paciente,
           item.cd_prestador,
           item.nm_prestador,
           item.cd_convenio,
           item.cnpj_convenio,
           item.nm_convenio,
           item.tp_atendimento,
           item.nr_guia_normalizada,
           item.nr_carteira_normalizada_com_zero,
           item.nr_carteira_normalizada,
           item.cd_pro_fat_normalizado,
           item.cd_tuss_normalizado,
           item.descricao,
           item.dt_atendimento,
           item.dt_alta,
           item.dt_competencia,
           item.dt_lancamento,
           item.qt_lancamento,
           item.valor_item,
           item.cd_gru_fat,
           item.ds_gru_fat,
           item.cd_gru_pro,
           item.ds_gru_pro
      from {{ ref('stg_demonstrativo_processos_ipm') }} d
      join {{ ref('int_ipm_relatorios_itens') }} item
        on item.nr_guia_normalizada
           = upper(btrim(coalesce(d.numero_guia_senha, '')))
       and item.nr_guia_normalizada <> ''
       and (
               (
                   ltrim(
                       regexp_replace(
                           coalesce(d.codigo_beneficiario, ''),
                           '[^0-9]', '', 'g'
                       ),
                       '0'
                   ) <> ''
                   and item.nr_carteira_normalizada = ltrim(
                       regexp_replace(
                           coalesce(d.codigo_beneficiario, ''),
                           '[^0-9]', '', 'g'
                       ),
                       '0'
                   )
               )
               or exists (
                   select 1
                     from contextos_protocolos contexto
                    where contexto.numero_protocolo
                          = upper(btrim(d.numero_protocolo))
                      and contexto.numero_processo_normalizado
                          = item.numero_processo_normalizado
                      and contexto.cd_remessa = item.cd_remessa
               )
           )
       and upper(btrim(coalesce(d.codigo_servico, ''))) in (
           item.cd_pro_fat_normalizado,
           item.cd_tuss_normalizado
       )
     where (
               nullif(btrim(d.numero_processo), '') is null
               or item.numero_processo_normalizado
                  = upper(btrim(d.numero_processo))
           )
       and (
           not exists (
               select 1
               from contextos_protocolos contexto
               where contexto.numero_protocolo
                     = upper(btrim(d.numero_protocolo))
           )
           or exists (
               select 1
               from contextos_protocolos contexto
               where contexto.numero_protocolo
                     = upper(btrim(d.numero_protocolo))
                 and contexto.numero_processo_normalizado
                     = item.numero_processo_normalizado
                 and contexto.cd_remessa = item.cd_remessa
           )
       )
), resumo_relatorio as (
    select
        id_registro,
        count(
            distinct (numero_processo_resolvido, cd_remessa, conta)
        ) as quantidade_contextos
    from candidatos_relatorio_brutos
    group by id_registro
), candidatos_relatorio as (
    select candidato.*
    from candidatos_relatorio_brutos candidato
    join resumo_relatorio resumo using (id_registro)
    where resumo.quantidade_contextos = 1
), demonstrativos_fallback as (
    select
        d.*,
        to_char(d.data_realizacao, 'YYYY-MM') as mes_realizacao,
        upper(btrim(coalesce(d.numero_guia_senha, ''))) as guia_normalizada,
        upper(btrim(coalesce(d.codigo_servico, ''))) as servico_normalizado,
        ltrim(
            regexp_replace(
                coalesce(d.codigo_beneficiario, ''), '[^0-9]', '', 'g'
            ),
            '0'
        ) as carteira_normalizada,
        round(coalesce(d.valor_processado, 0)::numeric, 2)
            as valor_normalizado
    from {{ ref('stg_demonstrativo_processos_ipm') }} d
    where not exists (
        select 1
        from candidatos_relatorio direto
        where direto.id_registro = d.id_registro
    )
), itens_relatorio as (
    select
        numero_processo as numero_processo_resolvido,
        cd_remessa,
        conta,
        cd_lancamento,
        cd_atendimento,
        cd_paciente,
        nm_paciente,
        cd_prestador,
        nm_prestador,
        cd_convenio,
        cnpj_convenio,
        nm_convenio,
        tp_atendimento,
        nr_guia_normalizada,
        nr_carteira_normalizada_com_zero,
        nr_carteira_normalizada,
        cd_pro_fat_normalizado,
        cd_tuss_normalizado,
        descricao,
        dt_atendimento,
        dt_alta,
        dt_competencia,
        dt_lancamento,
        qt_lancamento,
        valor_item,
        cd_gru_fat,
        ds_gru_fat,
        cd_gru_pro,
        ds_gru_pro
    from {{ ref('int_ipm_relatorios_itens') }}
), candidatos_fallback_brutos_sem_contexto as (
    select 11 as prioridade,
           'relatorio_hpc_competencia_guia_servico_carteira'::text
               as criterio,
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.nr_guia_normalizada = d.guia_normalizada
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select 12, 'relatorio_hpc_competencia_servico_carteira',
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and (
               d.guia_normalizada = ''
            or i.nr_guia_normalizada = ''
            or i.nr_guia_normalizada = d.guia_normalizada
       )

    union all
    select 13, 'relatorio_hpc_competencia_tuss_carteira_valor',
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.cd_tuss_normalizado <> ''
       and i.cd_tuss_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado
       and (
               d.guia_normalizada = ''
            or i.nr_guia_normalizada = ''
            or i.nr_guia_normalizada = d.guia_normalizada
       )

    union all
    select 14, 'relatorio_hpc_lancamento_servico_carteira',
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_lancamento, 'YYYY-MM') = d.mes_realizacao
       and coalesce(
               nullif(i.cd_pro_fat_normalizado, ''),
               i.cd_tuss_normalizado
           ) = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and (
               d.guia_normalizada = ''
            or i.nr_guia_normalizada = ''
            or i.nr_guia_normalizada = d.guia_normalizada
       )

    union all
    select 15, 'relatorio_hpc_competencia_servico_valor',
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and coalesce(
               nullif(i.cd_tuss_normalizado, ''),
               i.cd_pro_fat_normalizado
           ) = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado
       and (
               d.guia_normalizada = ''
            or i.nr_guia_normalizada = ''
            or i.nr_guia_normalizada = d.guia_normalizada
       )

    union all
    select 16, 'relatorio_hpc_atendimento_guia_servico_valor',
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_atendimento, 'YYYY-MM') = d.mes_realizacao
       and i.nr_guia_normalizada = d.guia_normalizada
       and coalesce(
               nullif(i.cd_tuss_normalizado, ''),
               i.cd_pro_fat_normalizado
           ) = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado

    union all
    select 17, 'relatorio_hpc_lancamento_servico_carteira_valor',
           d.id_registro, i.*
      from demonstrativos_fallback d
      join itens_relatorio i
        on to_char(i.dt_lancamento, 'YYYY-MM') = d.mes_realizacao
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado
       and (
               d.guia_normalizada = ''
            or i.nr_guia_normalizada = ''
            or i.nr_guia_normalizada = d.guia_normalizada
       )
), candidatos_fallback_brutos as (
    select candidato.*
      from candidatos_fallback_brutos_sem_contexto candidato
      join demonstrativos_fallback demonstrativo using (id_registro)
     where (
               nullif(btrim(demonstrativo.numero_processo), '') is null
               or upper(btrim(candidato.numero_processo_resolvido))
                  = upper(btrim(demonstrativo.numero_processo))
           )
       and (
               exists (
                   select 1
                     from contextos_protocolos contexto
                    where contexto.numero_protocolo
                          = upper(btrim(demonstrativo.numero_protocolo))
                      and contexto.numero_processo_normalizado
                          = upper(btrim(candidato.numero_processo_resolvido))
                      and contexto.cd_remessa = candidato.cd_remessa
               )
               or (
                   nullif(btrim(demonstrativo.numero_processo), '') is not null
                   and not exists (
                       select 1
                         from contextos_protocolos contexto
                        where contexto.numero_protocolo
                              = upper(btrim(demonstrativo.numero_protocolo))
                   )
               )
           )
), resumo_fallback as (
    select
        id_registro,
        prioridade,
        criterio,
        count(
            distinct (numero_processo_resolvido, cd_remessa, conta)
        ) as quantidade_contextos
    from candidatos_fallback_brutos
    group by id_registro, prioridade, criterio
), regras_fallback_seguras as (
    select *,
           row_number() over (
               partition by id_registro order by prioridade
           ) as ordem
    from resumo_fallback
    where quantidade_contextos = 1
), candidatos_fallback as (
    select candidato.*
    from candidatos_fallback_brutos candidato
    join regras_fallback_seguras regra
      on regra.id_registro = candidato.id_registro
     and regra.prioridade = candidato.prioridade
     and regra.criterio = candidato.criterio
     and regra.ordem = 1
), candidatos_legado as (
    select d.prioridade_origem + 1 as prioridade,
           d.origem_associacao
               || '_competencia_guia_servico_carteira' as criterio,
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.nr_guia_normalizada = d.guia_normalizada
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select d.prioridade_origem + 2,
           d.origem_associacao || '_competencia_servico_carteira',
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select d.prioridade_origem + 3,
           d.origem_associacao || '_competencia_tuss_carteira',
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and i.cd_tuss_normalizado <> ''
       and i.cd_tuss_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado

    union all
    select d.prioridade_origem + 4,
           d.origem_associacao
               || '_lancamento_coalesce_servico_carteira',
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_lancamento, 'YYYY-MM') = d.mes_realizacao
       and coalesce(nullif(i.cd_pro_fat_normalizado, ''), i.cd_tuss_normalizado)
           = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada

    union all
    select d.prioridade_origem + 5,
           d.origem_associacao
               || '_competencia_coalesce_servico_valor',
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_competencia, 'YYYY-MM') = d.mes_realizacao
       and coalesce(nullif(i.cd_tuss_normalizado, ''), i.cd_pro_fat_normalizado)
           = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado

    union all
    select d.prioridade_origem + 6,
           d.origem_associacao
               || '_atendimento_guia_coalesce_servico_valor',
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_atendimento, 'YYYY-MM') = d.mes_realizacao
       and i.nr_guia_normalizada = d.guia_normalizada
       and coalesce(nullif(i.cd_tuss_normalizado, ''), i.cd_pro_fat_normalizado)
           = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado

    union all
    select d.prioridade_origem + 7,
           d.origem_associacao
               || '_lancamento_pro_fat_carteira_valor',
           d.id_registro, d.numero_processo_resolvido, i.*
      from demonstrativos_legado d
      join {{ ref('stg_hpc_itens_ipm') }} i
        on i.cd_remessa = d.cd_remessa_esperada
       and (d.conta_esperada is null or i.conta = d.conta_esperada)
       and to_char(i.dt_lancamento, 'YYYY-MM') = d.mes_realizacao
       and i.cd_pro_fat_normalizado = d.servico_normalizado
       and i.nr_carteira_normalizada = d.carteira_normalizada
       and i.valor_item = d.valor_normalizado
), candidatos as (
    select * from candidatos_relatorio
    union all
    select * from candidatos_fallback
    union all
    select * from candidatos_legado
)
select * from candidatos
