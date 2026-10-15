# Automações fiscais e assistenciais com Astro, Airflow e dlt

Automação para emitir NFS-e, consultar notas emitidas, baixar PDF/XML e
carregar dados fiscais no PostgreSQL. O projeto também extrai demonstrativos
de contas médicas do Portal Credenciado IPM Saúde.

Para o portal ISS Fortaleza, o projeto usa endpoints HTTP JSF/Seam,
preservando cookies, conversação e `javax.faces.ViewState`. O SPU exige um
navegador Chromium para a paginação e reutiliza um perfil isolado autenticado.

- [Visão geral](#visão-geral)
- [Configuração compartilhada](#configuração-compartilhada)
- [Emissão de NFS-e — `emissao_nfse`](#emissão-de-nfs-e--emissao_nfse)
- [Extração de NFS-e — `extracao_nfse`](#extração-de-nfs-e--extracao_nfse)
- [Demonstrativo IPM — `extracao_demonstrativo_conta_ipm`](#demonstrativo-ipm--extracao_demonstrativo_conta_ipm)
- [Processos SPU — `extracao_processos_virtuais_spu`](#processos-spu--extracao_processos_virtuais_spu)
- [Materialização das glosas IPM com dbt](#materialização-das-glosas-ipm-com-dbt)
- [Testes](#testes)

## Visão geral

Emissão e extração são funcionalidades independentes:

| DAG | Responsabilidade | Disparo |
| --- | --- | --- |
| `emissao_nfse` | Emitir solicitações aprovadas, baixar o PDF e registrar o resultado | Manual ou API REST, sem agendamento |
| `extracao_nfse` | Consultar notas existentes, baixar XML e carregar `nfse_xml` | A cada 15 minutos ou manualmente |
| `extracao_demonstrativo_conta_ipm` | Extrair contas médicas e carregar `demonstrativo_conta_ipm` | Diariamente às 04:00 ou manualmente |
| `extracao_processos_virtuais_spu` | Extrair processos e PDFs IPM do SPU | Diariamente às 05:00 ou manualmente |
| `materializacao_glosas_ipm` | Carregar o estágio Oracle e executar as sete regras no dbt | Acionada ao término das extrações IPM/SPU |

```mermaid
flowchart LR
    API[api_prontocardio] -->|lote_id + IDs| AIRFLOW[Airflow/Astro]
    AIRFLOW --> EMISSAO[DAG emissao_nfse]
    AIRFLOW --> EXTRACAO[DAG extracao_nfse]
    AIRFLOW --> IPM_DAG[DAG demonstrativo IPM]
    AIRFLOW --> SPU_DAG[DAG processos SPU]
    EMISSAO -->|Lê e atualiza solicitações| PG[(PostgreSQL)]
    EMISSAO -->|Emite e baixa PDF| ISS[ISS Fortaleza]
    EXTRACAO -->|Consulta e baixa XML| ISS
    EXTRACAO -->|dlt merge| NFSE[(nfse_xml)]
    IPM_DAG -->|API e arquivo nativo| IPM[Portal IPM Saúde]
    IPM_DAG -->|dlt merge por referência| IPM_TABLE[(demonstrativo_conta_ipm)]
    SPU_DAG -->|Navega e baixa PDFs| SPU[SPU Virtual]
    SPU_DAG -->|dlt merge por processo| SPU_TABLES[(Tabelas SPU)]
    IPM_DAG --> DBT_DAG[DAG materialização dbt]
    SPU_DAG --> DBT_DAG
    DBT_DAG -->|carga intermediária| STAGE[(Tabelas staging Oracle)]
    DBT_DAG -->|dbt build| MARTS[(Vinculadas e não vinculadas)]
```

As DAGs compartilham somente a infraestrutura, as credenciais do portal e a
conexão PostgreSQL. Payloads, regras, horários, resultados e documentação
operacional ficam separados nas seções de cada funcionalidade.

## Configuração compartilhada

### Pré-requisitos

- Python 3.12 ou superior;
- Docker Engine;
- Astro CLI;
- PostgreSQL acessível pelos containers;

### Estrutura do projeto

```text
.
├── .astro/
├── dags/
│   ├── emissao_nfse.py
│   ├── extracao_demonstrativo_conta_ipm.py
│   ├── extracao_processos_virtuais_spu.py
│   ├── materializacao_glosas_ipm.py
│   └── extracao_nfse.py
├── dbt_glosas_ipm/
├── include/
├── plugins/
├── src/
│   └── nfs_fortaleza/
│       ├── batch.py
│       ├── cli.py
│       ├── config.py
│       ├── extraction.py
│       ├── issuance.py
│       ├── ipm_config.py
│       ├── ipm_demonstrativo.py
│       ├── ipm_extraction.py
│       ├── ipm_portal.py
│       ├── spu_auth.py
│       ├── spu_config.py
│       ├── spu_extraction.py
│       ├── spu_pdf.py
│       ├── spu_portal.py
│       ├── spu_resources.py
│       ├── load.py
│       ├── nfse_xml.py
│       ├── periods.py
│       ├── portal.py
│       └── spreadsheet.py
├── tests/
├── Dockerfile
├── docker-compose.override.yml
├── pyproject.toml
└── requirements.txt
```

### Variáveis de ambiente

Crie o arquivo local:

```bash
cp .env.example .env
```

Preencha as credenciais e a conexão:

```dotenv
PORTAL_PREFEITURA_FORTALEZA=https://iss.fortaleza.ce.gov.br/grpfor/home.seam
CPF_LOGIN=CPF_USADO_NO_LOGIN
SENHA='SENHA_DO_PORTAL'
# Fallback apenas para disparos legados; a DAG usa o CNPJ de cada emissão.
NFSE_ISSUER_CNPJ=59932105000121

DATABASE_URL=postgresql+psycopg://usuario:senha@host:5432/banco
POSTGRES_SCHEMA=api_prontocardio
NFSE_POSTGRES_CONN_ID=postgres_prontocardio
AIRFLOW_CONN_POSTGRES_PRONTOCARDIO=postgresql://usuario:senha@host:5432/banco

IPM_PORTAL_URL=https://ipmsaude.topsaudehub.com.br/PortalCredenciado
IPM_LOGIN=LOGIN_DO_PRESTADOR
IPM_PASSWORD='SENHA_DO_PORTAL'
```

Nos disparos feitos pela API, `dag_run.conf.cnpj_por_solicitacao`
transporta o CNPJ emissor de cada item. A DAG confere esse valor com o
snapshot gravado em `emissao_nfse.cnpj_emissor` e seleciona a inscrição
correspondente no portal, inclusive quando um lote contém CNPJs diferentes.

Se uma senha contiver `$`, mantenha o valor entre aspas simples para impedir
a interpolação pelo Docker Compose:

```dotenv
SENHA='senha-com-$-literal'
```

Variáveis opcionais para priorizar uma inscrição:

| Variável | Uso |
| --- | --- |
| `INSCRICAO_CNPJ` | Selecionar pelo CNPJ |
| `INSCRICAO_MUNICIPAL` | Selecionar pela inscrição municipal |
| `INSCRICAO_NOME` | Selecionar por parte da razão social |

### Executar com Astro

Valide a imagem e as DAGs:

```bash
astro dev parse
```

Inicie o ambiente:

```bash
astro dev start
```

Serviços locais:

| Serviço | Endereço |
| --- | --- |
| Airflow | `http://localhost:8082` |
| PostgreSQL de metadados do Airflow | `localhost:5433` |

Essas portas são definidas em `.astro/config.yaml` para não disputar a porta
`5432` do banco da aplicação nem a porta `8080` de outros serviços.

Para encerrar:

```bash
astro dev stop
```

Use `astro dev kill` somente quando também quiser remover containers e
metadados locais do Airflow.

PDFs, XMLs e diagnósticos persistem no volume Docker nomeado `nfse_data`,
montado em `/usr/local/airflow/data`. O volume preserva a propriedade do
usuário `astro` e evita erros de permissão causados por UIDs diferentes entre
host e container.

### Conexão PostgreSQL e pool

As DAGs usam a conexão `postgres_prontocardio`. No desenvolvimento local,
`airflow_settings.yaml` contém os mesmos dados de `DATABASE_URL`.

O arquivo possui senha em texto simples e, por isso, fica fora do Git e do
contexto de build. Depois de iniciar ou reiniciar o Astro, importe os objetos:

```bash
astro dev object import
```

Também é possível definir a conexão pela variável
`AIRFLOW_CONN_POSTGRES_PRONTOCARDIO`. Variáveis de ambiente têm precedência
sobre conexões importadas no banco de metadados do Airflow.

Em ambiente local, prefira preencher essa variável no `.env`: assim a conexão
já estará disponível quando os containers iniciarem. A DAG de emissão também
possui a etapa `aguardar_postgres`, que tolera o pequeno intervalo entre a
subida do Airflow e a importação de `airflow_settings.yaml`. Somente essa
verificação pode ser repetida; a etapa `processar_lote` permanece sem retry
automático para não correr o risco de emitir a mesma nota novamente após uma
falha ocorrida no portal.

O pool `nfse_portal` possui um slot e serializa o acesso ao portal. Em um
deployment Astro, armazene conexão e credenciais no painel ou em um secret
backend.

## Glosas Maida/ISSEC — `extracao_glosas_maida`

A DAG acessa exclusivamente as APIs usadas pelo portal Maida, sem automação de
cliques. Ela autentica no serviço de contas, identifica o prestador vinculado,
consulta o Resumo e os lotes analisados e baixa os demonstrativos XLSX pelo
endpoint de arquivos.

Na primeira execução são verificadas todas as competências de janeiro de 2026
até o mês corrente. Nas execuções seguintes, competências históricas já
consultadas são preservadas e os três meses mais recentes são verificados
novamente a cada execução, inclusive quando o arquivo ainda não tiver sido
disponibilizado pelo portal.

Somente linhas que possuam `Valor Glosa` maior que zero e um `Código Glosa`
numérico são carregadas em `demonstrativo_conta_ipm`. O carregamento `dlt` usa
chave estável e `merge`, permitindo reprocessamentos sem duplicação. Ao final,
a DAG aciona `materializacao_glosas_ipm`, que alimenta os registros exibidos no
Follow-Up de Glosas.

Credenciais reais devem ficar no secret backend do Airflow ou no `.env` não
versionado:

```dotenv
MAIDA_LOGIN=...
MAIDA_PASSWORD='...'
MAIDA_POSTGRES_CONN_ID=postgres_prontocardio
```

Uma execução manual pode limitar o período com `dag_run.conf`:

```json
{"competencias": ["01/2026", "02/2026"]}
```

## Emissão de NFS-e — `emissao_nfse`

### Objetivo e arquivos

A DAG recebe um lote criado por `api_prontocardio`, consulta novamente o banco
e emite somente os itens que continuam aprovados e pendentes.

| Arquivo | Responsabilidade |
| --- | --- |
| `dags/emissao_nfse.py` | Definição da DAG e leitura do `dag_run.conf` |
| `src/nfs_fortaleza/batch.py` | Elegibilidade, reserva dos itens e atualização dos estados |
| `src/nfs_fortaleza/issuance.py` | Emissão no portal por requisições HTTP |
| `src/nfs_fortaleza/spreadsheet.py` | Validação dos dados e nome do PDF |

### Fluxo para o usuário

<div align="center">
  <p>
    O usuário seleciona as solicitações aprovadas e acompanha o resultado pela
    aplicação, sem acessar manualmente o portal da Prefeitura.
  </p>
  <table width="82%">
    <tr>
      <td align="center" bgcolor="#E8F1FF">
        <br>
        <strong>📋 Solicitações aprovadas</strong><br><br>
        A aplicação apresenta os itens disponíveis para emissão.
        <br><br>
      </td>
    </tr>
    <tr><td align="center"><strong>↓</strong></td></tr>
    <tr>
      <td align="center" bgcolor="#F1ECFF">
        <br>
        <strong>☑️ Seleção das notas</strong><br><br>
        O usuário marca uma ou mais solicitações.
        <br><br>
      </td>
    </tr>
    <tr><td align="center"><strong>↓</strong></td></tr>
    <tr>
      <td align="center" bgcolor="#EAF7FF">
        <br>
        <strong>🚀 Solicitação da emissão</strong><br><br>
        A aplicação envia os itens selecionados para processamento.
        <br><br>
      </td>
    </tr>
    <tr><td align="center"><strong>↓</strong></td></tr>
    <tr>
      <td align="center" bgcolor="#FFF5D6">
        <br>
        <strong>⏳ Emissão em andamento</strong><br><br>
        Cliente, serviço e valor são conferidos automaticamente.
        <br><br>
      </td>
    </tr>
  </table>

  <table width="92%">
    <tr>
      <td align="center" width="46%"><strong>↙</strong></td>
      <td align="center" width="8%"></td>
      <td align="center" width="46%"><strong>↘</strong></td>
    </tr>
    <tr>
      <td align="center" bgcolor="#E6F7EC">
        <br>
        <strong>✅ Nota emitida</strong><br><br>
        Número e PDF disponíveis para consulta.
        <br><br>
      </td>
      <td align="center"></td>
      <td align="center" bgcolor="#FDECEC">
        <br>
        <strong>⚠️ Emissão não concluída</strong><br><br>
        O motivo fica disponível para correção.
        <br><br>
      </td>
    </tr>
  </table>
</div>

### Regras de negócio

```mermaid
flowchart TD
    INICIO["Usuário seleciona<br/>as solicitações aprovadas"] --> GRUPO["Sistema reúne as notas<br/>que serão emitidas"]
    GRUPO --> AUTOMACAO["A emissão automática é iniciada"]

    subgraph SELECAO["1. Conferência das solicitações"]
        AUTOMACAO --> BUSCA["Busca somente as notas selecionadas"]
        BUSCA --> PRONTA{"A nota está pronta<br/>para emissão?"}
        REGRAS["A solicitação foi aprovada<br/>Foi marcada para emissão<br/>Ainda não foi emitida"] -.-> PRONTA
        PRONTA -- "Não" --> FORA["Não envia a nota<br/>e informa a pendência"]
        PRONTA -- "Sim" --> ANDAMENTO["Marca a nota como em andamento<br/>para evitar emissão duplicada"]
        ANDAMENTO --> DADOS{"Os dados estão completos?<br/>CPF, paciente, serviço,<br/>local, atendimento e valor"}
        DADOS -- "Não" --> ERRO
    end

    subgraph PREFEITURA["2. Emissão no site da Prefeitura"]
        DADOS -- "Sim" --> ACESSO["Acessa o site da Prefeitura"]
        ACESSO --> EMPRESA["Seleciona a empresa emissora correta"]
        EMPRESA --> EMITIR["Abre a opção Emitir NFS-e"]
        EMITIR --> CLIENTE["Pesquisa o cliente pelo CPF"]
        CLIENTE --> TOMADOR{"O cliente já está<br/>cadastrado?"}
        TOMADOR -- "Não" --> CADASTRO["Cadastra nome, CPF,<br/>endereço, cidade e e-mail"]
        TOMADOR -- "Sim" --> SERVICO
        CADASTRO --> SERVICO["Preenche o serviço realizado<br/>com o tipo de exame"]
        SERVICO --> VALOR["Informa o valor aprovado"]
        VALOR --> VALIDAR["Confere os dados antes de emitir"]
        VALIDAR --> CAMPOS{"A Prefeitura<br/>aceitou os dados?"}
        CAMPOS -- "Não" --> ERRO["Interrompe a emissão<br/>e guarda o motivo do erro"]
        CAMPOS -- "Sim" --> CONFIRMAR["Confirma a emissão"]
        CONFIRMAR --> EMITIDA{"A nota foi<br/>emitida com sucesso?"}
        EMITIDA -- "Não" --> ERRO
        EMITIDA -- "Sim" --> PDF["Obtém o número e baixa o PDF"]
    end

    subgraph RESULTADO["3. Resultado na aplicação"]
        PDF --> SUCESSO["Nota concluída<br/>Número e PDF disponíveis"]
        ERRO --> FALHA["Nota requer atenção<br/>Motivo disponível para correção"]
        SUCESSO --> LOTE{"Todas as notas selecionadas<br/>foram emitidas?"}
        FALHA --> LOTE
        FORA --> LOTE
        LOTE -- "Sim" --> LOTE_OK["Processo concluído"]
        LOTE -- "Não" --> LOTE_ERRO["Processo requer atenção"]
    end

    classDef origem fill:#172033,stroke:#d9e2f2,color:#ffffff,stroke-width:1.5px;
    classDef processo fill:#1d2738,stroke:#cbd5e1,color:#ffffff,stroke-width:1.5px;
    classDef decisao fill:#4a3418,stroke:#f4c56a,color:#ffffff,stroke-width:1.5px;
    classDef sucesso fill:#123b2a,stroke:#70d6a0,color:#ffffff,stroke-width:1.5px;
    classDef falha fill:#471f25,stroke:#f08a96,color:#ffffff,stroke-width:1.5px;
    classDef regra fill:#2a2d33,stroke:#9ca3af,color:#ffffff,stroke-dasharray: 4 3;

    class INICIO,GRUPO,AUTOMACAO origem;
    class BUSCA,ANDAMENTO,ACESSO,EMPRESA,EMITIR,CLIENTE,CADASTRO,SERVICO,VALOR,VALIDAR,CONFIRMAR,PDF processo;
    class PRONTA,DADOS,TOMADOR,CAMPOS,EMITIDA,LOTE decisao;
    class SUCESSO,LOTE_OK sucesso;
    class ERRO,FALHA,FORA,LOTE_ERRO falha;
    class REGRAS regra;
```

Os IDs recebidos definem somente o escopo. Antes de acessar a Prefeitura, a
DAG exige simultaneamente:

- item pertencente ao `lote_id` recebido;
- solicitação presente em `solicitacao_ids`;
- emissão com estado `PENDENTE`;
- validação `VALIDADA`;
- workflow `EMISSAO_SOLICITADA`;
- CPF válido, paciente, procedimento, local e tipo de atendimento;
- `valor_nota` maior que zero.

O item é alterado de `PENDENTE` para `PROCESSANDO` de forma atômica. Assim,
duas execuções não conseguem emitir a mesma nota.

### Emissão no ISS Fortaleza

Para cada item elegível, a automação:

1. autentica no portal e seleciona o CNPJ `59.932.105/0001-21`;
2. abre `NFS-e > Emitir NFS-e`;
3. pesquisa o tomador pelo CPF;
4. cadastra Pessoa Física quando o cliente ainda não existe;
5. preenche serviço, descrição e valor aprovado;
6. solicita a validação dos campos;
7. confirma somente quando o portal habilita a ação final;
8. recupera o número da nota;
9. baixa um PDF válido;
10. atualiza emissão, workflow, evento e lote.

O cadastro do tomador utiliza paciente, CPF, rua, número, bairro, cidade e
e-mail. A descrição do serviço recebe o tipo de exame.

Os parâmetros fiscais usados pelo fluxo são:

| Campo | Valor |
| --- | --- |
| CNAE | `861010101` |
| NBS | `123011900` |
| Indicador da operação | `030104` |
| CST | `200` |
| Classificação tributária | `200029` |
| Alíquota | `3,00` |

O PDF segue o padrão:

```text
NÚMERO DA NFS-E - LOCAL, TIPO ATENDIMENTO e PACIENTE.pdf
```

### Estados e auditoria

| Momento | Workflow | Emissão | Lote |
| --- | --- | --- | --- |
| Após validação | `VALIDADA` | — | — |
| Selecionada para emissão | `EMISSAO_SOLICITADA` | `PENDENTE` | `PENDENTE` |
| Durante a execução | `EMISSAO_SOLICITADA` | `PROCESSANDO` | `PROCESSANDO` |
| Concluída | `EMITIDA` | `EMITIDA` | `EMITIDA` se todos concluírem |
| Falha | `ERRO_EMISSAO` | `ERRO` | `ERRO` |

Número, protocolo, `dag_run_id`, horários e eventual erro ficam registrados.
Os eventos são gravados como `NFSE_EMITIDA` ou `ERRO_EMISSAO`.

### Integração com `api_prontocardio`

O projeto `/home/rafaelamorim/repo/api_prontocardio` fornece:

- endpoint `POST /app_glosas/requisicoes/emissoes-nfse`;
- cliente REST em `app_prontocardio/services/airflow_nfse.py`;
- tabelas operacionais e migrations `20260723_028` e `20260723_029`.

Configure a API:

```dotenv
AIRFLOW_NFSE_BASE_URL=http://host.docker.internal:8082
AIRFLOW_NFSE_DAG_ID=emissao_nfse
AIRFLOW_NFSE_DAG_RUNS_PATH=/api/v1/dags/{dag_id}/dagRuns
AIRFLOW_NFSE_TOKEN=
AIRFLOW_NFSE_USERNAME=admin
AIRFLOW_NFSE_PASSWORD=admin
AIRFLOW_NFSE_TIMEOUT_SECONDS=15
AIRFLOW_NFSE_VERIFY_SSL=false
```

Em produção, use HTTPS e um usuário de serviço com permissão mínima. Quando a
API estiver em Docker, `localhost:8082` apontará para o próprio container;
use `host.docker.internal`, um DNS compartilhado ou o endereço publicado do
Airflow.

Emissão individual:

```bash
curl -X POST "http://localhost:8000/app_glosas/requisicoes/emissoes-nfse" \
  -H "Authorization: Bearer TOKEN_DA_API" \
  -H "Content-Type: application/json" \
  -d '{"solicitacao_ids":[101]}'
```

Emissão em lote:

```bash
curl -X POST "http://localhost:8000/app_glosas/requisicoes/emissoes-nfse" \
  -H "Authorization: Bearer TOKEN_DA_API" \
  -H "Content-Type: application/json" \
  -d '{"solicitacao_ids":[101,102,103]}'
```

A API envia ao Airflow:

```json
{
  "dag_run_id": "api_prontocardio_nfse_lote_42",
  "conf": {
    "origem": "API_PRONTOCARDIO",
    "lote_id": 42,
    "solicitacao_ids": [101, 102, 103]
  }
}
```

#### Contrato PostgreSQL

| Tabela | Responsabilidade |
| --- | --- |
| `solicitacao_nota` | Dados fiscais e cadastrais aprovados |
| `solicitacao_nota_workflow` | Aprovação e estado funcional |
| `lote_emissao_nfse` | Estado, disparo, `dag_run_id` e erro do lote |
| `emissao_nfse` | Estado e resultado de cada nota |
| `solicitacao_nota_evento` | Auditoria do processamento |

A seleção da DAG equivale a:

```sql
SELECT e.id
FROM api_prontocardio.emissao_nfse e
JOIN api_prontocardio.solicitacao_nota s
  ON s.id = e.solicitacao_nota_id
JOIN api_prontocardio.solicitacao_nota_workflow w
  ON w.solicitacao_nota_id = s.id
WHERE e.lote_id = :lote_id
  AND e.solicitacao_nota_id = ANY(:solicitacao_ids)
  AND e.status = 'PENDENTE'
  AND w.validacao = 'VALIDADA'
  AND w.status = 'EMISSAO_SOLICITADA';
```

O valor aprovado vem de `solicitacao_nota.valor_nota`. Na ausência de cidade
ou UF, são aplicados `FORTALEZA` e `CE`.

#### Disparo direto para diagnóstico

```bash
curl -u "admin:admin" \
  -X POST "http://localhost:8082/api/v1/dags/emissao_nfse/dagRuns" \
  -H "Content-Type: application/json" \
  -d '{
    "dag_run_id":"teste_nfse_lote_42",
    "conf":{"lote_id":42,"solicitacao_ids":[101,102]}
  }'
```

Esse disparo não cria lote nem aprova solicitações. Se os registros não
estiverem nos estados exigidos, nenhuma nota será emitida.

### Emissão manual por planilha

O comando manual permanece disponível para operação assistida e diagnóstico.
A integração oficial em lote deve usar `emissao_nfse` e o PostgreSQL.

#### Instalação local

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

#### Colunas da planilha

Por padrão, o comando lê `NOTAS FISCAIS.xlsx`:

| Coluna | Uso |
| --- | --- |
| `PACIENTE` | Nome do tomador e nome do PDF |
| `CPF` | Pesquisa ou cadastro do tomador |
| `RUA` | Logradouro |
| `NUMERO CASA` | Número do endereço |
| `BAIRRO` | Bairro |
| `CIDADE` | Cidade |
| `UF` | Unidade federativa |
| `EMAIL` | E-mail |
| `TIPO DE EXAME` | Descrição do serviço |
| `VALOR` | Valor do serviço |
| `LOCAL` | Nome do PDF |
| `TIPO ATENDIMENTO` | Nome do PDF |

`ATENDIMENTO` e `DATA` são opcionais. Cabeçalhos são normalizados quanto a
maiúsculas, minúsculas e acentuação. A primeira linha de dados é a linha `2`.

#### Conferir sem emitir

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --linha 2
```

Sem `--confirmar-emissao`, o comando valida e exibe a linha, mas não autentica
nem altera o portal.

Prévia de até dez linhas:

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --todas \
  --limite 10
```

#### Emitir uma nota

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --linha 2 \
  --cnpj 59932105000121 \
  --confirmar-emissao
```

O CNPJ pode ser omitido quando for a inscrição padrão.

#### Emitir várias linhas

```bash
.venv/bin/nfs-fortaleza emitir-planilha "NOTAS FISCAIS.xlsx" \
  --todas \
  --limite 10 \
  --confirmar-emissao
```

É obrigatório informar `--linha` ou `--todas`. No modo em lote, uma falha
interrompe as próximas linhas; emissões anteriores permanecem registradas.

#### Proteção contra duplicidade

Após obter um PDF válido, o comando grava o sucesso em
`downloads/emissoes_nfse.jsonl`. Uma linha com o mesmo conteúdo será ignorada
nas execuções seguintes.

Erros de navegação ou respostas inesperadas geram HTML em `.artifacts/`.

| Parâmetro | Descrição |
| --- | --- |
| `PLANILHA` | Caminho do XLSX |
| `--linha N` | Processar uma linha |
| `--todas` | Processar todas as linhas pendentes |
| `--limite N` | Limitar o lote |
| `--cnpj CNPJ` | Selecionar a inscrição emissora |
| `--downloads-dir DIR` | Alterar o diretório de PDFs |
| `--confirmar-emissao` | Autorizar a emissão fiscal real |

## Extração de NFS-e — `extracao_nfse`

### Objetivo e arquivos

A DAG consulta NFS-e já existentes, exporta os XMLs e carrega os campos
normalizados em `POSTGRES_SCHEMA.nfse_xml`.

| Arquivo | Responsabilidade |
| --- | --- |
| `dags/extracao_nfse.py` | Agenda e leitura dos filtros do `dag_run.conf` |
| `src/nfs_fortaleza/extraction.py` | Resolução dos filtros e execução |
| `src/nfs_fortaleza/portal.py` | Consulta, paginação e exportação dos XMLs |
| `src/nfs_fortaleza/nfse_xml.py` | Parse e normalização do XML |
| `src/nfs_fortaleza/load.py` | Pipeline `dlt` e carga PostgreSQL |
| `src/nfs_fortaleza/periods.py` | Competências, datas e janelas permitidas |

### Agenda e filtros da DAG

Por padrão, `extracao_nfse` executa a cada 15 minutos, usando a expressão
`*/15 * * * *`. A agenda pode ser alterada por `NFSE_EXTRACTION_SCHEDULE`.

Uma execução agendada sem configuração consulta a competência corrente, do
primeiro dia do mês até o dia atual. Antes da carga, o pipeline consulta
`POSTGRES_SCHEMA.nfse_xml` e descarta as notas já existentes pela chave
`codigo_verificacao_nfse`; se essa chave não existir no XML, usa CNPJ do
prestador e número da NFS-e. Assim, somente notas inéditas são enviadas ao
`dlt` e ao PostgreSQL.

Execuções manuais preservam a consulta de qualquer competência no formato
`mm/aaaa` por meio de `dag_run.conf`. Também são aceitos os demais formatos
abaixo.

Competência:

```json
{"competencia": "07/2026"}
```

Intervalo:

```json
{"inicio": "01/07/2026", "fim": "23/07/2026"}
```

Nota específica:

```json
{"cnpj": "59932105000121", "numero_nfse": "8"}
```

Disparo REST:

```bash
curl -u "admin:admin" \
  -X POST "http://localhost:8082/api/v1/dags/extracao_nfse/dagRuns" \
  -H "Content-Type: application/json" \
  -d '{
    "dag_run_id":"extracao_manual_202607",
    "conf":{"competencia":"07/2026"}
  }'
```

### Regras de consulta

Para cada competência ou janela, a automação:

1. abre `NFS-e > Consultar NFS-e`;
2. seleciona `Serviços Prestados`;
3. usa o filtro `Período Emissão/Tomador`;
4. preenche as datas permitidas;
5. consulta as notas;
6. exporta o XML de cada linha;
7. percorre todas as páginas;
8. entrega XML ou ZIP ao pipeline `dlt`;
9. normaliza e carrega os campos no PostgreSQL.

O portal não aceita data final futura. Na competência atual, a data final é
limitada ao dia de hoje.

Consultas sem CPF/CNPJ do tomador são limitadas a 31 dias. Intervalos maiores
são divididos automaticamente por mês e pelo limite do portal.

Se nenhuma nota for encontrada, a execução registra `Ignorado` e continua
para a próxima janela.

```mermaid
flowchart TD
    A[Receber competência, intervalo ou número] --> B[Acessar ISS Fortaleza]
    B --> C[Autenticar]
    C --> D[Abrir Consultar NFS-e]
    D --> E[Selecionar Serviços Prestados]
    E --> F[Dividir o período em janelas permitidas]
    F --> G[Consultar uma janela]
    G --> H{Existem notas?}
    H -- Não --> I[Registrar janela sem notas]
    H -- Sim --> J[Exportar XML de cada linha]
    J --> K{Existe próxima página?}
    K -- Sim --> J
    K -- Não --> L[Processar XML ou ZIP]
    L --> M[Normalizar campos fiscais]
    M --> N[Carregar nfse_xml]
```

### Execução manual em desenvolvimento e produção

Cada execução precisa de um `run-id` exclusivo. O padrão abaixo inclui a
competência e um timestamp para evitar `DagRunAlreadyExists`. Ao repetir uma
competência, o pipeline consulta `nfse_xml` e envia ao `dlt` somente notas cujo
`codigo_verificacao_nfse`, a chave fiscal da nota, ainda não exista na tabela.
Quando um XML excepcional não traz essa chave, a combinação de CNPJ do
prestador e número da NFS-e é usada como fallback.

No desenvolvimento local, com o projeto iniciado por `astro dev start`, use o
comando nativo do Astro:

```bash
astro dev run dags trigger extracao_nfse \
  --run-id "extracao_manual_202608_$(date +%Y%m%d_%H%M%S)" \
  --conf '{"competencia":"08/2026"}'
```

Em produção, execute o Airflow dentro do container do scheduler:

```bash
docker exec -it prj-web-nfs_1f87bb-scheduler-1 \
  airflow dags trigger extracao_nfse \
  --run-id "extracao_manual_202608_$(date +%Y%m%d_%H%M%S)" \
  --conf '{"competencia":"08/2026"}'
```

Se o nome do container mudar após um novo deployment, localize-o antes com:

```bash
docker ps --filter name=prj-web-nfs --filter name=scheduler \
  --format '{{.Names}}'
```

Para outra competência, altere tanto `202608` no `run-id` quanto `08/2026` no
`--conf`.

### Uso pela linha de comando

Consultar e carregar uma nota específica:

```bash
.venv/bin/nfs-fortaleza run \
  --cnpj 59932105000121 \
  --numero-nfse 12345
```

`--cnpj` e `--numero-nfse` devem ser informados juntos e não podem ser
combinados com filtros de período.

Consultar e carregar uma competência:

```bash
.venv/bin/nfs-fortaleza run --competencia 06/2026
```

Baixar sem carregar:

```bash
.venv/bin/nfs-fortaleza download --competencia 06/2026
```

Carregar XML ou ZIP já baixado:

```bash
.venv/bin/nfs-fortaleza load-file \
  downloads/nota_5.xml \
  --competencia 06/2026
```

Intervalo de competências:

```bash
.venv/bin/nfs-fortaleza run --inicio 01/2026 --fim 06/2026
```

Intervalo de datas:

```bash
.venv/bin/nfs-fortaleza run \
  --inicio 01/06/2026 \
  --fim 09/07/2026
```

Somente download de um intervalo:

```bash
.venv/bin/nfs-fortaleza download \
  --inicio 01/06/2026 \
  --fim 09/07/2026
```

### Carga e deduplicação

O resultado é entregue a
`dlt.sources.filesystem.filesystem`. Um `@dlt.transformer` interpreta XML/ZIP,
preserva o documento original e gera `row_hash`.

A carga usa `merge` com `row_hash`, impedindo duplicação em `nfse_xml`.

Principais grupos de campos:

- situação, cancelamento e mensagens;
- número, código de verificação, competência e datas;
- RPS e lote de RPS;
- serviço, CNAE, tributação, alíquota e discriminação;
- prestador e tomador;
- endereço e contato;
- construção civil, obra e ART;
- valores, deduções, retenções, ISS e descontos;
- `xml_campos`, com o XML normalizado;
- `xml_documento`, com o documento original;
- `row_hash`, chave de merge.

`aliquota` é normalizada com quatro casas decimais. O conteúdo original
permanece em `aliquota_xml`.

### Observações do portal

O botão geral `Exportar XML das Notas Selecionadas` pode retornar XML vazio
quando a seleção visual não está sincronizada no servidor. A automação usa o
link `Exportar XML` de cada linha e envia o formulário com o
`javax.faces.ViewState` atual.

Falhas de login, navegação, sessão ou mudança do portal geram HTML de
diagnóstico em `.artifacts/`.

## Demonstrativo IPM — `extracao_demonstrativo_conta_ipm`

A DAG autentica no Portal Credenciado, consulta `Demonstrativos >
Demonstrativo de Conta Médica`, gera o arquivo nativo de cada referência e
carrega `POSTGRES_SCHEMA.demonstrativo_conta_ipm` com `dlt`.

portal com as já existentes na tabela e extrai somente referências inéditas.
`IPM_EXTRACTION_SCHEDULE` altera a agenda. Em uma execução manual, é possível
restringir o escopo:

```json
{"referencia": "05/2026"}
```

Ou selecionar várias referências:
```json
{"referencias": ["12/2025", "01/2026", "05/2026"]}
```

Mesmo quando informada manualmente, uma referência já presente na tabela é
ignorada e não é baixada novamente.

Disparo REST de exemplo:

```bash
curl -u "admin:admin" \
  -X POST \
  "http://localhost:8082/api/v1/dags/extracao_demonstrativo_conta_ipm/dagRuns" \
  -H "Content-Type: application/json" \
  -d '{
    "dag_run_id":"ipm_202605",
    "conf":{"referencia":"05/2026"}
  }'
```

### Variáveis do IPM

| Variável | Padrão/uso |
| --- | --- |
| `IPM_PORTAL_URL` | URL do Portal Credenciado |
| `IPM_LOGIN` | Login do prestador |
| `IPM_PASSWORD` | Senha do prestador |
| `IPM_PROVIDER_CODE` | Opcional; usa `IPM_LOGIN` por padrão |
| `IPM_OPERATOR_CODE` | `1` |
| `IPM_POSTGRES_CONN_ID` | `postgres_prontocardio` |
| `IPM_EXTRACTION_SCHEDULE` | `0 4 * * *` |
| `IPM_DOWNLOADS_DIR` | `/usr/local/airflow/data/ipm` |
| `IPM_TIMEOUT_SECONDS` | `60` |

As credenciais reais ficam somente no `.env`, que está ignorado pelo Git.
Em produção, use as variáveis/segredos do deployment Airflow.

### Dados e atualização

Cada linha representa um serviço executado e repete os dados de referência,
lote, protocolo, guia e beneficiário necessários para consulta direta. Datas
são carregadas como `date`, valores como `decimal` e códigos como texto para
preservar zeros à esquerda.

A tabela PostgreSQL é consultada antes de qualquer arquivo ser solicitado ao
portal. Referências encontradas em `demonstrativo_conta_ipm` são ignoradas;
quando não há referência nova, a execução termina com sucesso sem download e
sem iniciar uma carga `dlt`. Na primeira execução, a ausência da tabela é
tratada como histórico vazio.

A chave técnica `id_registro` é determinística e a carga mantém `merge` com
`referencia` como chave da janela, oferecendo uma segunda proteção contra
duplicidade.

O arquivo TXT do portal apresenta os rótulos de lote e protocolo invertidos.
O parser aplica os significados confirmados na tela HTML: o valor `TISS_...`
é armazenado em `numero_lote`, e o identificador numérico em
`numero_protocolo`.

As colunas funcionais são:

- `referencia`, `cnpj_operadora`, `numero_lote`, `data_envio_lote`;
- `numero_protocolo`, `valor_protocolo`, `valor_glosa_protocolo`;
- `numero_guia_senha`, `nome_beneficiario`, `codigo_beneficiario`;
- `data_realizacao`, `descricao_servico`, `codigo_tabela`, `codigo_servico`;
- `grau_participacao`, `quantidade_executada`;
- `valor_processado`, `valor_liberado`, `valor_glosa`, `codigo_glosa`.

## Processos SPU — `extracao_processos_virtuais_spu`

A DAG tem duas etapas. `carregar_processos` percorre as páginas de
**Processos Virtuais**, coleta número, status, tipo/assunto, data de abertura
e motivo da finalização e grava os registros com dlt a cada lote, sem esperar
o fim da paginação. Depois, `processar_pdfs` consulta somente os finalizados
pendentes e acessa a árvore materializada. Somente os documentos de
`IPM/SAUDECOGESTAO` e `IPM/NUEXO` são baixados.

Ao terminar, a DAG dispara e aguarda a DAG separada
`extracao_relatorios_tramitando_spu`. Ela reutiliza o mesmo perfil persistido,
o mesmo pool `nfse_portal` e a sessão recém-validada para localizar processos
`FINALIZADO` ou `TRAMITANDO` cujo número seja de 2024 em diante. Neles, baixa
somente nomes no formato exato `RELATORIO_<remessa>_<id>.pdf` e extrai remessa,
paciente, guia, conta, atendimento, competência e valor. O identificador da
DAG foi preservado por compatibilidade operacional. Documentos já carregados
são ignorados pelo `documento_id`; anexos novos do mesmo processo continuam
elegíveis em execuções posteriores.

Os dados do relatório são validados contra `HPC_V_CONTA_ATENDIMENTO` na ordem
remessa/conta/atendimento, remessa/conta/guia, remessa/atendimento/guia e
remessa/conta. Uma conta resolvida fornece diretamente o processo, a remessa e
todos os seus itens hospitalares. Em seguida, guia, conta e serviço associam os
campos do demonstrativo TRUE aos itens da HPC. As sete regras anteriores ficam
restritas ao fallback de demonstrativos que não possuem relatório SPU válido;
vínculos ambíguos continuam pendentes em vez de serem atribuídos por
aproximação.

O mart `processos_relatorios_itens_ipm` disponibiliza, no mesmo registro, os
dados de processo/relatório, os campos analíticos da HPC e, quando existentes,
`numero_protocolo`, `codigo_servico`, `codigo_glosa`, `codigo_beneficiario`,
`referencia`, `valor_protocolo`, `valor_glosa_protocolo`, `valor_processado` e
`valor_liberado` do demonstrativo TRUE.

O log informa cada página consultada, os totais de processos novos e já
carregados, cada lote enviado ao PostgreSQL e o andamento individual dos
processos na etapa de PDFs. Um status não reconhecido é armazenado como
`DESCONHECIDO` e gera um alerta no log, sem interromper toda a paginação.

Os PDFs são obtidos pelo endpoint autenticado de cada item da árvore, sem
cliques no visualizador. `pdfplumber` processa todas as páginas, ignora
cabeçalhos repetidos e reúne as linhas da tabela de contas quando ela continua
na página seguinte.

### Tabelas dlt

| Tabela | Conteúdo |
| --- | --- |
| `processos_ipm` | Uma linha por processo, com status atual, tipo/assunto, abertura e motivo |
| `processos_historico_ipm` | Histórico idempotente das situações observadas |
| `processos_ipm_saude_cogestao` | Tabela multipágina dos PDFs `IPM/SAUDECOGESTAO` |
| `processos_empenho_ipm` | Banco, código da conta, código da agência e conta dos EMPENHOS em `IPM/NUEXO` |
| `processos_nota_fiscal_ipm` | Número, chave de acesso e prestador das NFS-e em `IPM/NUEXO` |
| `processos_relatorios_ipm` | Destino canônico único das contas extraídas de processos `FINALIZADO` ou `TRAMITANDO` desde 2024 |

A antiga tabela mista foi migrada para as duas tabelas específicas e preservada
como `processos_ipm_nuexo_legacy`; a DAG não realiza novas cargas nela.
Em EMPENHO, o antigo valor composto `banco_agencia` é armazenado separadamente
em `codigo_conta` e `codigo_agencia`; o nome da instituição permanece em
`banco`.

Os pares repetidos do PDF de cogestão são diferenciados por
`valor_aprovado_producao`, `valor_glosado_producao`,
`valor_aprovado_protocolo` e `valor_glosado_protocolo`. Identificadores são
texto, datas são `date` e valores são `decimal(18,2)`.
O layout de revisão também preenche `nr_origem` e `processo_recurso`, interpreta
`Valor Aprovado Rev` como valor aprovado do protocolo, `Valor Liberado` como
valor aprovado do resumo e `R$ -` como zero.

### Incrementalidade

Antes de materializar ou baixar PDFs, a DAG consulta
`processos_ipm`. Um número já carregado com o mesmo status é
ignorado. Quando um processo muda de `TRAMITANDO` para `FINALIZADO`, essa nova
situação é processada uma única vez para que os PDFs e o motivo da finalização
não sejam perdidos.

Na execução incremental padrão, a listagem — ordenada pelo SPU dos registros
mais recentes para os mais antigos — é interrompida depois de duas páginas
consecutivas contendo somente processos já carregados e sem mudança de status.
Uma página com processo novo, status alterado ou detalhamento finalizado
pendente zera esse contador. Assim, a DAG não percorre novamente todo o
histórico em cada execução. Para uma reconciliação deliberada de todas as
páginas, use `{"varredura_completa": true}` no disparo manual.

`detalhes_finalizados_extraidos` só recebe `true` depois que os documentos
disponíveis dos setores alvo são processados. Setores ausentes e formatos
históricos sem todos os campos são registrados no log sem interromper o lote;
processos finalizados sem o botão `VISUALIZAR PROCESSO` são concluídos sem
documentos, pois não há árvore materializada disponível;
falhas técnicas de navegação ou download mantêm o processo pendente para a
próxima tentativa. As tabelas usam chaves determinísticas e carga
`merge`; a DAG também usa `max_active_runs=1`.

### Autenticação

As credenciais reais permanecem apenas no `.env`, ignorado pelo Git. O SPU
usa uma validação reCAPTCHA invisível: no acesso humano ela normalmente não
mostra desafio, mas passa a exigir imagens quando detecta automação no login.
Por isso a DAG nunca envia o formulário. Ao receber um redirecionamento para
`/auth/login`, ela fecha a navegação headless, abre uma janela Chromium
visível usando o mesmo perfil persistente, preenche `SPU_LOGIN`,
`SPU_PASSWORD` e **Relembrar?** e aguarda a ação humana. O usuário conclui o
reCAPTCHA, se solicitado, e clica em **Entrar**. Quando o portal sair da tela
de login, a janela é fechada automaticamente e a operação interrompida é
executada novamente na mesma tarefa.

O `docker-compose.override.yml` executa um desktop virtual Xvfb em um sidecar
separado, protegido por senha VNC, e compartilha somente o socket X11 com o
scheduler. Quando a tela de login é detectada, a tarefa publica um estado
temporário com identificador e validade do desafio. O frontend do Receita
Certa consulta esse estado e abre automaticamente um modal com o navegador do
SPU. A senha VNC é fornecida pelo backend e não é solicitada nem exibida ao
usuário.

Por padrão, o noVNC publica a porta somente em `127.0.0.1` e também fica
disponível na rede Docker compartilhada `receita_certa_automation`. O acesso
HTTP e WebSocket é encaminhado pelo frontend, que valida a sessão do Receita
Certa e a origem da conexão. Quando os projetos estiverem no mesmo host, crie
essa rede uma vez antes de subir os dois projetos:

```bash
docker network create receita_certa_automation
```

Quando Airflow e Receita Certa estiverem em hosts diferentes, configure
`SPU_NOVNC_BIND_ADDRESS` com o IP privado do servidor Airflow e aponte
`SPU_NOVNC_INTERNAL_URL` do frontend para esse IP e porta. Restrinja no
firewall o acesso à porta para o IP privado do servidor do Receita Certa.

Configure o mesmo valor de `RECEITA_CERTA_SPU_NOVNC_PASSWORD`, com exatamente oito
caracteres, nos `.env` de `prj_web_nfs` e `prj_glosas`. A senha é transportada
no fragmento local da URL do iframe, que não é enviado ao servidor HTTP, e é
usada pelo noVNC para conectar automaticamente. As credenciais do SPU são
usadas apenas para preencher os campos no navegador e não são escritas no log,
no URL HTTP nem em arquivos auxiliares.

O mesmo fluxo pode ser iniciado manualmente para preparar ou validar o perfil:

```bash
.venv/bin/python -m nfs_fortaleza.spu_auth \
  --browser-profile downloads/spu/browser_profile \
  --executable-path /usr/bin/chromium-browser
```

O utilitário também preenche os dados, aguarda o login e valida a primeira
página. O perfil contém cookies de autenticação: não o versione nem o envie
por e-mail. No Docker, o volume nomeado `nfse_data` mantém esse perfil após
reinícios. Um lock de arquivo impede que a extração headless e a renovação
visível abram o mesmo perfil simultaneamente.

Se o sidecar noVNC for deliberadamente desativado, configure
`SPU_AUTO_RENEW_SESSION=false` e renove o perfil com o utilitário em uma
máquina que tenha navegador visível. `SPU_STORAGE_STATE_PATH` continua
disponível para ambientes que já provisionem um arquivo Playwright
`storage_state`.

### Variáveis do SPU

| Variável | Padrão/uso |
| --- | --- |
| `SPU_PORTAL_URL` | `https://spuvirtual.sepog.fortaleza.ce.gov.br` |
| `SPU_MATERIALIZER_URL` | `https://spumaterializar.sepog.fortaleza.ce.gov.br` |
| `SPU_LOGIN` / `SPU_PASSWORD` | Credenciais da pessoa jurídica |
| `SPU_POSTGRES_CONN_ID` | `postgres_prontocardio` |
| `SPU_EXTRACTION_SCHEDULE` | `0 5 * * *` |
| `SPU_DOWNLOADS_DIR` | `/usr/local/airflow/data/spu` |
| `SPU_BROWSER_PROFILE_DIR` | `/usr/local/airflow/data/spu/browser_profile` |
| `SPU_STORAGE_STATE_PATH` | `/usr/local/airflow/data/spu/auth_state.json` |
| `SPU_BROWSER_HEADLESS` | `true` na DAG |
| `SPU_AUTO_RENEW_SESSION` | `true`; abre a janela de login quando a sessão expira |
| `SPU_AUTH_TIMEOUT_SECONDS` | `1800`; tempo para o usuário concluir o login |
| `RECEITA_CERTA_AUTOMATION_NETWORK` | rede Docker externa compartilhada com o frontend; padrão `receita_certa_automation` |
| `SPU_RECAPTCHA_STATUS_PATH` | estado temporário lido pelo modal; padrão `/tmp/.X11-unix/spu-recaptcha/status.json` |
| `RECEITA_CERTA_SPU_NOVNC_PASSWORD` | senha interna de exatamente 8 caracteres; use o mesmo valor no Receita Certa |
| `SPU_NOVNC_BIND_ADDRESS` | bind do noVNC; padrão seguro `127.0.0.1`; em hosts distintos, use o IP privado do Airflow |
| `SPU_NOVNC_PORT` | porta do noVNC; padrão `6080` |
| `SPU_NOVNC_SCREEN` | resolução e profundidade; padrão `1600x900x24` |
| `SPU_PAGE_TIMEOUT_SECONDS` | `90` |
| `SPU_DOWNLOAD_DELAY_SECONDS` | `0.75` segundo entre downloads; respostas 429 usam retentativa exponencial |
| `SPU_PROCESS_BATCH_SIZE` | `50` processos por carga durante a paginação |
| `SPU_PDF_BATCH_SIZE` | `20` processos finalizados por carga de PDFs |

Um disparo manual pode limitar páginas para diagnóstico ou selecionar
processos específicos. `varredura_completa` desativa somente a interrupção
incremental por páginas já conhecidas:

```json
{
  "numero_processos": ["P193251/2026"],
  "max_pages": 3,
  "varredura_completa": false
}
```

Sem payload, a DAG usa a paginação incremental.

## Materialização das glosas IPM com dbt

A DAG `materializacao_glosas_ipm` é acionada ao término das cargas do
demonstrativo IPM e dos processos SPU. A integração não usa Airflow Datasets.
Os dois disparos são seguros porque a DAG aceita somente uma execução ativa;
a execução posterior recompõe os resultados com o estado mais recente das
duas fontes.

Primeiro, a DAG copia o recorte necessário da
`DBAMV.HPC_V_CONTA_ATENDIMENTO` para tabelas intermediárias no mesmo
PostgreSQL/Railway utilizado pela aplicação:

| Tabela | Finalidade |
| --- | --- |
| `api_prontocardio_staging.ipm_remessas_oracle` | Total da remessa por competência, calculado pelo `MAX(vl_total_conta)` interno e `SUM` externo |
| `api_prontocardio_staging.ipm_itens_oracle` | Contas e lançamentos Oracle necessários às sete regras |

O intervalo Oracle é limitado do primeiro mês ao mês posterior às glosas
positivas existentes em `demonstrativo_processos_ipm`. A troca do estágio é
transacional: uma falha antes do `commit` mantém a versão anterior disponível.

Depois, `dbt build` cria as tabelas de trabalho no schema
`api_prontocardio_intermediate`. A associação demonstrativo/processo lê
exclusivamente `api_prontocardio.demonstrativo_processos_ipm`; processo e
remessa são associados por competência, valor do protocolo e total calculado
da remessa. As sete regras são avaliadas em prioridade e somente uma conta
segura é aceita. Uma linha ambígua ou não encontrada não cancela as demais
linhas da remessa.

Os resultados finais são:

| Tabela | Conteúdo |
| --- | --- |
| `api_prontocardio.glosas_ipm_vinculadas` | Itens associados com processo, remessa, paciente, conta e lançamento |
| `api_prontocardio.glossas_nao_vinculadas_ipm` | Itens sem vínculo seguro e o motivo da pendência |

As conexões são `IPM_POSTGRES_CONN_ID` e `IPM_ORACLE_CONN_ID`. Não existe
banco local para esta carga; ambos devem apontar para as fontes efetivas e o
destino PostgreSQL deve ser o Railway.

O Oracle é aberto em Thick Mode com o Instant Client 19.23 instalado na
imagem. `ORACLE_CLIENT_LIB_DIR` permite alterar o diretório, cujo padrão é
`/opt/oracle/instantclient_19_23`. Esse modo é necessário para o verifier de
senha legado utilizado pelo ambiente hospitalar.

## Testes

Execute a suíte sem emitir notas:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Valide também a imagem e a integridade das DAGs:

```bash
astro dev parse
```

Os testes cobrem payloads das DAGs, regras de elegibilidade, proteção contra
reemissão, leitura da planilha, ações JSF, consulta por número, filtros da
extração e normalização do demonstrativo IPM, incrementalidade do SPU,
tabelas multipágina e campos de EMPENHO/NFS-e.


### Maida: demonstrativos sem planilha no Resumo

A `extracao_glosas_maida` prefere os XLSX do Resumo. Na ausência deles,
processa os demonstrativos dos lotes com glosas não recursadas e recursadas,
percorrendo todas as páginas. O formato `DemonstrativoAnaliseDeContas` é
reconhecido por `Valor glosado` e `Motivo da glosa`; a guia utilizada é a da
operadora. São preservados lote, código, carteira, data de execução,
quantidade e valores apresentado, apurado e glosado. Linhas sem glosa
continuam excluídas. A carga usa o merge existente em
`demonstrativo_conta_ipm` e aciona `materializacao_glosas_ipm`, que alimenta
o Receita Certa. Não há nova tabela nem migration.

Se os lotes estiverem indisponíveis e não houver XLSX no Resumo, a extração
falha para permitir retry, em vez de concluir uma carga incompleta.
Após implantar, reprocessar as competências anteriormente ignoradas com
`{"competencias": ["08/2026", "09/2026", "10/2026"]}` na DAG e conferir a
materialização antes de validar os cards no Receita Certa.


### Guia do lançamento ISSEC na materialização

A carga intermediária prefere a guia do próprio lançamento para o ISSEC,
identificada por conta, lançamento, atendimento e procedimento no ambulatório
ou conta/lançamento/procedimento na internação. Na ausência dela, conserva a
guia da view HPC. O filtro das guias Maida é aplicado depois dessa resolução.
Outros convênios mantêm o comportamento anterior. Não há associação por nome,
descrição de medicamento ou valor do item: o procedimento ainda deve coincidir.
Após implantação, executar `materializacao_glosas_ipm` para atualizar os vínculos
existentes, preservando rastreabilidade e tratativas.


### Códigos Brasíndice dos demonstrativos Maida

A carga intermediária mantém `ipm_brasindice_oracle`, com equivalências
numéricas únicas de `DBAMV.IMP_BRA.CD_TISS` para `CD_PRO_FAT`. Códigos que
apontam para mais de um procedimento são excluídos. Na associação Maida do
ISSEC (46), esse cadastro complementa CD_PRO_FAT/CD_TUSS sem substituir os
códigos originais e sem multiplicar lançamentos. Guia, carteira e unicidade
do contexto continuam obrigatórias conforme as regras existentes.
A tabela é criada/atualizada na transação da carga HPC antes do dbt;
implantar e executar a DAG completa `materializacao_glosas_ipm`.
