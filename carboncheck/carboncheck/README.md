<div align="center">
  <img src="./logo.png" alt="CarbonCheck" width="112">
  <h1>CarbonCheck</h1>
  <p><strong>Inventário e acompanhamento de emissões de transportadoras</strong></p>
  <p>
    Aplicação web para registrar consumos, estimar emissões de gases de efeito estufa
    e apoiar a revisão de inventários por verificadores, reguladores e administradores.
  </p>
</div>

<p align="center">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="Flask" src="https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white">
  <img alt="Interface em português" src="https://img.shields.io/badge/idioma-Portugu%C3%AAs%20(Brasil)-2E7D32">
</p>

---

## Para que serve

O CarbonCheck ajuda transportadoras a organizar seus dados de atividade e estimar suas emissões em **CO₂ equivalente (CO₂e)**. Cada organização cria um inventário para um ano, registra os consumos por mês e acompanha o processo de análise. O sistema também oferece um painel consolidado para perfis de verificação, regulação e administração.

O projeto é uma ferramenta de apoio: os fatores de emissão incluídos são **simplificados e ilustrativos**. Os resultados não substituem um inventário validado, uma verificação independente nem uma avaliação de conformidade regulatória.

## O que você pode fazer

| Perfil | Principais ações |
|---|---|
| **Organização / transportadora** | Criar inventários anuais, registrar emissões e informações operacionais opcionais, acompanhar a análise e baixar relatórios. |
| **Verificador** | Consultar inventários submetidos, registrar pareceres e aprovar, solicitar ajustes ou reprovar. |
| **Órgão regulador** | Consultar inventários e acessar o painel consolidado das transportadoras. |
| **Administrador** | Acessar o painel, aprovar solicitações de perfis privilegiados e gerenciar contas. |

### Fluxo de um inventário

1. A organização cria um inventário para o ano de apuração.
2. Registra cada fonte de emissão no mês em que ocorreu. O formulário informa a unidade, oferece dicas e apresenta uma estimativa de CO₂e.
3. Quando os dados estiverem prontos, envia o inventário para análise.
4. Um verificador registra o parecer: **aprovar**, **solicitar ajuste** ou **reprovar**.
5. A organização acompanha o status e, se necessário, corrige e reenvia o inventário.

## Emissões e indicadores de transporte

As fontes disponíveis cobrem atividades que podem ocorrer em uma transportadora:

| Categoria | Exemplos de fontes disponíveis |
|---|---|
| **1 — Emissões diretas** | Diesel, gasolina, etanol, biodiesel, GLP, gás natural/GNV, vazamento de gases refrigerantes e estimativas por distância de veículos da frota. |
| **2 — Energia importada** | Eletricidade consumida. |
| **3 — Transporte** | Fretes contratados rodoviários, ferroviários, aquaviários e aéreos, quando aplicável. |
| **4 — Produtos e resíduos** | Resíduos enviados para aterro. |

Informe a **quantidade** na unidade exibida ao selecionar uma fonte (por exemplo, litros, kWh, kg, km ou tonelada-quilômetro). Só registre os meses em que houve atividade. A estimativa é calculada a partir da quantidade e do fator associado à fonte; a interface e os relatórios exibem kgCO₂e para valores abaixo de uma tonelada e tCO₂e a partir de uma tonelada.

Os campos operacionais — como distância, viagens, carga, frota e refrigeração — são **opcionais**. Eles ajudam a contextualizar a operação, mas não são necessários para calcular emissões e não alimentam os indicadores de emissão do painel.

> **Evite dupla contagem:** não lance o combustível de um veículo e também uma estimativa por distância para a mesma atividade. Escolha o método que melhor representa os dados disponíveis.

### Painel executivo

O painel reúne dados das transportadoras e apresenta indicadores, evolução anual, distribuição por categoria, ranking e andamento das verificações. **Somente inventários aprovados** entram nos totais e gráficos de emissão. O quadro de andamento mostra separadamente os inventários submetidos ou analisados; rascunhos não são incluídos. É possível filtrar os indicadores por ano.

## Como executar localmente

Requisitos: **Python 3.12** e `pip`.

No terminal, a partir da pasta que contém este README:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
CARBONCHECK_DB_BACKEND=sqlite python app.py
```

Abra [http://127.0.0.1:5000](http://127.0.0.1:5000). Para encerrar, use `Ctrl+C`; para sair do ambiente virtual, execute `deactivate`.

O banco SQLite é criado automaticamente em `instance/carboncheck.sqlite3`. No modo local, o app disponibiliza estas contas de demonstração (senha para todas: `demo1234`):

| Perfil | E-mail |
|---|---|
| Organização | `org@carboncheck.com.br` |
| Verificador | `verificador@carboncheck.com.br` |
| Órgão regulador | `regulador@carboncheck.com.br` |
| Administrador | `admin@carboncheck.com.br` |

Use as contas demo somente para desenvolvimento. Cadastros aceitam e-mails `@carboncheck.com.br`; senhas precisam ter pelo menos 8 caracteres, uma letra maiúscula, uma minúscula e um caractere especial. Solicitações de verificador e regulador precisam da aprovação de um administrador; contas de organização são ativadas imediatamente.

### Executar os testes

Com o ambiente virtual ativado, dentro da pasta do projeto:

```bash
python -m unittest -v
```

## Dados e banco de dados

O SQLite é adequado para desenvolvimento e é usado por padrão quando não há configuração MySQL. Para forçar SQLite, defina `CARBONCHECK_DB_BACKEND=sqlite`. Para armazenar o banco em outro caminho, defina `CARBONCHECK_DB`.

O app também oferece suporte a MySQL. Configure a conexão por variáveis de ambiente (`MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER` e `MYSQL_PASSWORD`) ou por um arquivo local `~/.config/carboncheck/mysql.cnf`:

```ini
[client]
host=127.0.0.1
port=3306
database=carboncheck
user=usuario_do_app
password=sua_senha
```

Proteja o arquivo de configuração (`chmod 600 ~/.config/carboncheck/mysql.cnf`) e selecione o backend explicitamente:

```bash
CARBONCHECK_DB_BACKEND=mysql python app.py
```

As tabelas são criadas automaticamente na primeira execução. Para migrar uma base SQLite existente, pare o servidor, faça uma cópia de segurança do arquivo `instance/carboncheck.sqlite3` e execute `python migrate_sqlite_to_mysql.py` com as credenciais do MySQL configuradas. O migrador exige tabelas de destino vazias e preserva o arquivo SQLite original.

## Criar contas administrativas

Com as dependências instaladas, dentro da pasta do projeto, crie a primeira conta de administrador pelo terminal:

```bash
flask --app app create-admin
```

O comando solicita nome, e-mail e senha. Para criar uma conta de verificador ou regulador, use `flask --app app create-user`. Os comandos utilizam o mesmo backend e as mesmas configurações de banco definidos nas variáveis de ambiente. Em produção, configure `APP_ENV=production`, gere uma `CARBONCHECK_SECRET_KEY` aleatória com pelo menos 32 caracteres, mantenha as credenciais fora do código e configure HTTPS e backups do banco. Não utilize as contas demo em produção.

## API

As rotas protegidas usam um token JWT no cabeçalho `Authorization: Bearer <token>`.

| Método | Rota | Uso |
|---|---|---|
| `POST` | `/api/auth/cadastro` | Cadastro de organização ou solicitação de perfil privilegiado. |
| `POST` | `/api/auth/login` | Autenticação. |
| `GET` | `/api/fatores` | Lista fontes e fatores de emissão disponíveis. |
| `POST`, `GET` | `/api/inventarios` | Cria e lista inventários. |
| `GET` | `/api/inventarios/{id}` | Consulta um inventário autorizado. |
| `POST`, `DELETE` | `/api/inventarios/{id}/lancamentos` | Adiciona ou remove lançamentos de emissão. |
| `POST`, `DELETE` | `/api/inventarios/{id}/operacoes` | Adiciona ou remove indicadores operacionais opcionais. |
| `POST` | `/api/inventarios/{id}/submeter` | Envia o inventário para verificação. |
| `POST` | `/api/inventarios/{id}/verificacao` | Registra decisão e parecer do verificador. |
| `GET` | `/api/inventarios/{id}/relatorio` | Gera relatório; use `?formato=pdf` ou `?formato=json`. |
| `GET` | `/api/historico` | Consulta histórico e status de conformidade. |
| `GET` | `/api/painel/transporte?ano=2026` | Consulta os indicadores consolidados do ano. |
| `/api/admin/*` | — | Gerencia solicitações e contas; acesso administrativo. |

## Limites e uso responsável

- Os fatores de emissão presentes no código são **ilustrativos** e não devem ser usados como referência oficial para declarações regulatórias sem validação e substituição por fatores adequados à metodologia, ao período e à operação.
- O fluxo de verificação implementado é simplificado e não substitui verificação independente.
- O sistema não implementa nem comprova conformidade com a **ISO 14001**. Ele apoia partes do registro e da análise de inventários, em linha com conceitos de quantificação e verificação de emissões associados às **ISO 14064-1** e **ISO 14064-3**; não equivale a certificação.
