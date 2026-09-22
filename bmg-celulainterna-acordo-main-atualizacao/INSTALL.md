# Instalação do zero — bmg-celulainterna-acordo

Guia passo-a-passo para instalar o robô num PC novo (PC da Andreza ou outro).

---

## Pré-requisitos (instalar UMA vez por PC)

### 1. Python 3.10 ou superior

1. Baixe em https://www.python.org/downloads/
2. **No instalador, MARQUE a opção `Add python.exe to PATH`** antes de clicar em Install Now. Isso é crítico — sem isso o `setup.ps1` não acha o Python.
3. Confira no PowerShell:
   ```powershell
   python --version
   ```
   Tem que mostrar `Python 3.10.x` ou maior.

### 2. Microsoft Edge

Já vem instalado no Windows 10/11. Se não tiver, baixe em https://www.microsoft.com/edge.

### 3. Git

1. Baixe em https://git-scm.com/download/win
2. Aceite os defaults do instalador.
3. Confira:
   ```powershell
   git --version
   ```

### 4. GitHub CLI (opcional, mas recomendado)

Facilita a autenticação. Baixe em https://cli.github.com/. Depois rode:
```powershell
gh auth login
```
e escolha `GitHub.com` → `HTTPS` → autenticar via navegador. Pronto, daí em diante o git puxa/empurra sem pedir senha.

---

## Clonar o projeto

Abra o PowerShell e rode:

```powershell
mkdir C:\Robos -ErrorAction SilentlyContinue
cd C:\Robos
git clone https://github.com/LD-Soc-Advogados/bmg-celulainterna-acordo.git
cd bmg-celulainterna-acordo
```

> Se ainda não tem acesso ao repo, o Adriano precisa te adicionar como collaborator na org `LD-Soc-Advogados` antes desse passo.

---

## Rodar o setup automático

Na pasta do projeto, rode:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Isso vai:

- ✓ Verificar Python 3.10+
- ✓ Verificar Microsoft Edge
- ✓ Criar o `.venv` (ambiente Python isolado do projeto)
- ✓ Instalar todas as dependências (Playwright, pdfplumber, openpyxl, curl_cffi, etc.)
- ✓ Instalar o driver do Edge usado pelo Playwright
- ✓ Criar o `.env` a partir do `.env.example`
- ✓ Criar as pastas `saida/`, `downloads/`, `logs/`

Demora 1-2 min.

---

## Configurar o `.env`

Abra o arquivo `.env` no Notepad ou no editor da sua preferência e ajuste:

```ini
# Onde salvar os PDFs baixados. No escritório, aponta pro drive de rede.
# No PC da Andreza, pode ser um drive mapeado ou caminho local pra teste.
DOWNLOADS_DIR=X:\ACORDO\ACORDO - CEDULA INTERNA
```

Se não tiver acesso ao drive `X:` ainda, deixe `DOWNLOADS_DIR=downloads` (local) que funciona.

---

## Como usar (rotina diária)

### Toda execução:

1. **Coloque a planilha do banco na raiz do projeto.** Nome no padrão `Planilha Envio de casos - Cedula Interna XXXX.xlsx`. O robô pega automaticamente a mais recente por data de modificação.

2. **Abra o Edge dedicado e logue:**
   - Duplo-clique em `abrir-edge-bmg.bat`
   - No Edge que abrir, faça login no e-Xyon (usuário + senha + captcha)
   - Espere chegar na tela principal do BMG
   - **Deixe esse Edge aberto durante toda a execução do robô.**

3. **Abra a interface gráfica:**
   - Duplo-clique em `iniciar.bat` → abre uma janela com:
     - **Campo da planilha** (já vem com a última usada / mais recente)
     - **Modo de execução** (botões de rádio: tudo / só mensagem / só downloads / etc.)
     - **`--forcar`** (checkbox pra reprocessar CIVs já capturadas)
     - **CIV única** (campo opcional pra testar UMA CIV)
     - **Botão verde "INICIAR ROBO"**
     - **Botões auxiliares**: Abrir Edge BMG / Pasta de saída / Pasta de logs

4. **Acompanhe o log** na janela (em tempo real, fundo preto estilo terminal). Quando terminar, aparece um popup com contadores OK / JA_FECHADO / ATENCAO + botões pra abrir relatório, log e pasta de saída.

5. **Resultados** ficam em:
   - `saida/financeiro_AAAA-MM-DD_HH-MM.xlsx`
   - `downloads/<NOME_CLIENTE>/` (PDFs)
   - `logs/AAAA-MM-DD_HH-MM-SS.log` (toda a saída do console — útil pra debug)

### Modos disponíveis:

| Modo | Quando usar |
|------|-------------|
| **COMPLETO** | Pipeline inteiro de uma vez: Tarefa 1 + 2 + advogados (DJEN) + contato (CNA). O robô encadeia tudo sozinho — o operador não precisa rodar cada etapa. **Modo recomendado.** Precisa do Edge logado no e-Xyon. |
| **Tudo** | Tarefa 1 (dados da aba Financeiro) + Tarefa 2 (downloads), sem os advogados |
| **Só Tarefa 1** | Extrai todos os campos da aba Financeiro (Cod Lançamento, Data de Pagamento, Valor, Saldo, Honorários, etc.) do lançamento mais recente |
| **Só Tarefa 2** | Já capturou mensagens, agora só baixar PDFs |
| **Só Tarefa 3** | PDFs já baixados, agora extrair contatos do advogado (offline, não precisa Edge) |
| **Só Tarefa 4** | Advogados + OAB do polo Ativo via API do DJEN (não precisa Edge nem login; gera `saida/advogados_djen_*.xlsx`) |
| **Só Tarefa 5** | Contato (telefone/e-mail/endereço) dos advogados via CNA. Lê a saída da Tarefa 4 e gera `saida/advogados_djen_cna_*.xlsx`. **Abre o próprio navegador** (não precisa do Edge dedicado nem de login). Campos vêm vazios para advogados que não os cadastraram no CNA. |

### O que a Tarefa 2 baixa (regra de prioridade)

- **Principais**: PETIÇÃO INICIAL e CÓPIA INTEGRAL (aceita nomes fora do padrão como "INICIAL" ou "INTEGRAL" soltos).
- Se **as duas** estão no processo → baixa **só essas 2**.
- Se **falta alguma** → baixa também UM complementar: CITAÇÃO (preferida) ou, se não houver, INTIMAÇÃO.

---

## Mantendo o robô atualizado

Quando o Adriano ou você fizer melhorias no robô (em qualquer PC), todo mundo precisa atualizar o seu clone local. Esse é o fluxo:

### Para BAIXAR atualizações de outros PCs (o caso mais comum):

```powershell
cd C:\Robos\bmg-celulainterna-acordo
git pull
```

Se aparecer "Already up to date.", já está atualizado. Se baixou algo, talvez precise rodar `setup.ps1` de novo (raramente, só se mudaram as dependências).

### Para MANDAR suas melhorias pra outros PCs:

Depois de testar uma mudança que você (ou o Claude no seu PC) fez:

```powershell
git add -A
git commit -m "descreva o que mudou"
git push
```

Os outros PCs vão pegar com `git pull`.

> **Importante:** **NUNCA** edite arquivos diretamente nas pastas `saida/`, `downloads/`, `logs/`, ou na planilha de input — esses estão no `.gitignore` e não vão pro repositório. Só código vai (`*.py`, `*.bat`, `*.ps1`, `*.md`, etc.).

---

## Solução de problemas

| Sintoma | O que fazer |
|---|---|
| `Python nao encontrado no PATH` no setup | Reinstalar Python marcando "Add to PATH". |
| `ambiente virtual nao encontrado em .venv` no iniciar.bat | Rodar `setup.ps1`. |
| Robô não acha o Edge | Conferir que `abrir-edge-bmg.bat` foi executado e que tem aba do BMG aberta. |
| `Mensagem ja capturada em execucao anterior` (status JA_FECHADO) | Idempotência. Pra reprocessar, use opção 6 do menu com modo "4=tudo + forcar". |
| Sessão expirou no meio do lote | Faça login de novo no Edge e rode de novo — idempotência pula tudo que já foi feito. |
| `git pull` reclama de conflito | Chama o Adriano. Provavelmente alguém esqueceu de comitar antes de editar. |

---

## Estrutura de pastas no PC (depois de instalado)

```
C:\Robos\bmg-celulainterna-acordo\
├── .venv\                  Ambiente Python isolado (criado pelo setup.ps1)
├── .env                    Suas configs (criado pelo setup.ps1)
├── core\                   Código compartilhado
├── fluxos\                 Lógica de cada tarefa
├── saida\                  Planilhas geradas (gitignored)
├── downloads\              PDFs por cliente (gitignored)
├── logs\                   Logs (gitignored)
├── main.py                 Entrypoint
├── iniciar.bat             ← Duplo-clique aqui pra rodar o menu
├── abrir-edge-bmg.bat      ← Duplo-clique aqui pra abrir o Edge
├── setup.ps1               Rode UMA vez ao instalar
└── Planilha Envio de casos*.xlsx   ← Você coloca aqui (gitignored)
```
