# Sistema de Conferência de Chamados — RPA

Automação de validação cruzada entre planilha Google Sheets e o sistema CA Service Desk Manager (CA SDM).

## O que faz

- Lê todos os chamados com status **PENDENTE** na coluna H da planilha de controle
- Para cada chamado, busca as informações no **CA SDM** automaticamente via Selenium
- Preenche a **coluna E** (Data de Abertura) se estiver vazia
- Preenche a **coluna G** (Data de Resolução) quando o status for Resolvido, Concluído ou Encerrado
- Identifica o tipo de chamado pelo prefixo do ID:
  - `I` → Incidente
  - `R` → Solicitação
  - `P` → Problema

## Tecnologias

- **Python 3.x**
- **Flask + Flask-SocketIO** — interface web com logs em tempo real
- **Selenium** — automação do navegador
- **gspread** — integração com Google Sheets API
- **python-dotenv** — gerenciamento de variáveis de ambiente

## Configuração

### 1. Clone o repositório

```bash
git clone git@github.com:irislenisson/conferenciachamados.git
cd conferenciachamados
```

### 2. Crie o ambiente virtual e instale as dependências

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

### 3. Configure as variáveis de ambiente

Crie um arquivo `.env` na raiz do projeto:

```
CA_EMAIL=seu.usuario
CA_PASSWORD=sua_senha
```

### 4. Configure as credenciais do Google Sheets

Coloque o arquivo `credentials.json` (conta de serviço Google) na raiz do projeto.  
> ⚠️ **Nunca versione este arquivo.** Ele está listado no `.gitignore`.

### 5. Execute

```bash
.venv\Scripts\python.exe app.py
```

Acesse: [http://localhost:5000](http://localhost:5000)

## Estrutura do projeto

```
├── app.py              # Servidor Flask + WebSocket
├── scraper.py          # Lógica de automação (Selenium + gspread)
├── requirements.txt    # Dependências Python
├── .gitignore          # Arquivos ignorados pelo Git
├── .env                # (NÃO versionar) Credenciais CA SDM
├── credentials.json    # (NÃO versionar) Credenciais Google API
├── static/
│   ├── css/style.css   # Estilos da interface
│   ├── js/script.js    # Lógica frontend + WebSocket
│   └── favicon.png     # Ícone da aplicação
└── templates/
    └── index.html      # Página principal
```

## Segurança

> ⚠️ Os arquivos `.env` e `credentials.json` contêm credenciais sensíveis e estão **excluídos do controle de versão** via `.gitignore`. Nunca os compartilhe ou versione.
