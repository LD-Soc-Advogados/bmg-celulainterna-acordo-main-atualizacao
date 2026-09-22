# bmg-celulainterna-acordo

Robô Python (Playwright + Edge CDP) que, a partir de uma planilha de CIVs
**autorizadas a acordo** pela Célula Interna do Banco BMG:

1. Entra em cada CIV no e-Xyon, abre o processo e vai na aba **Mensagens**.
2. Localiza a mensagem com assunto **"PEDIDO DE PAGAMENTO – CONDENAÇÃO
   INTIMAÇÃO"** (ou **"PEDIDO DE PAGAMENTO"**) e copia o conteúdo do
   "Painel de Leitura" para uma planilha de saída (`CIV | mensagem`).
3. Vai em **Documentos** e baixa, via Zeev, a **petição inicial** e a
   **cópia integral do processo**, salvando em
   `downloads/<NOME_CLIENTE>/`.

Projeto irmão de [bmg-extrator](https://github.com/LD-Soc-Advogados/bmg-extrator)
(download de documentos) e
[bmg-workflow-closer](https://github.com/adrianolucchesi/bmg-workflow-closer)
(fechamento de workflows — ainda no user pessoal, transferência pendente).
Reaproveita login manual + CDP via Edge dedicado.

## Status

**Fase F2.4** — Tarefa 2 validada em batch de 5 CIVs (4 baixados +
1 já existia + 0 erros; incluiu CIV1327098 com 2 docs: petição + intimação).
Fluxo via **HTTP direto** (igual `bmg-extrator`): popup única para ler a
tabela `tbl_ListaDocs`, extrai `id_doc`/`p4` de `onclick="Baixar(...)"`,
baixa via Zeev (curl_cffi + cookies do Edge), descompacta ZIP→PDF.

Bootstrap automático do cookie Zeev: se a sessão não tiver
`ConvergeSilentLogin_*`, o robô abre a popup `#btnDocumento` e
clica no 1º disquete via JS — o servidor seta o cookie em 1-2s, o
robô recapture, e o lote segue. Sem necessidade de clique manual prévio.

Pronto pra rodar lote completo dos 75 CIVs.

## Arquitetura prevista

```
bmg-celulainterna-acordo/
├── core/                  # Reaproveitado/adaptado de bmg-extrator e bmg-workflow-closer
│   └── captcha.py         # Handler genérico "espera humano resolver" (reCAPTCHA v2)
├── fluxos/                # Lógica específica de cada tarefa
├── main.py                # Entrypoint
├── abrir-edge-bmg.bat     # Abre Edge dedicado (porta 9222)
├── Planilha Envio de casos*.xlsx  # Input (operador coloca, gitignored)
├── saida/                 # Planilha de saída CIV+mensagem (gitignored)
└── downloads/             # PDFs por cliente (gitignored)
```

## Pré-requisitos

- Windows 10/11
- Python 3.10+
- Microsoft Edge

## Instalação (PC novo)

```powershell
cd C:\Robos\bmg-celulainterna-acordo
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install msedge
Copy-Item .env.example .env
```

## Uso diário (previsto)

1. Operador coloca a planilha autorizada (ex.:
   `Planilha Envio de casos - Cedula Interna 1905.xlsx`) na raiz.
2. Duplo-clique em `abrir-edge-bmg.bat`, faz login no e-Xyon.
3. Roda: `.venv\Scripts\python.exe main.py`
4. Saída:
   - `saida/mensagens_acordo.xlsx` (CIV + mensagem)
   - `downloads/<NOME_CLIENTE>/` com os PDFs (Tarefa 2 — Fase F)
   - `_LOTE_RELATORIO_{data}.txt` na raiz

### Flags úteis de teste

```powershell
.venv\Scripts\python.exe main.py --civ CIV1387277      # so 1 CIV
.venv\Scripts\python.exe main.py --limite 3            # so as 3 primeiras
.venv\Scripts\python.exe main.py --planilha outro.xlsx # planilha alternativa
.venv\Scripts\python.exe main.py --so-mensagem         # so Tarefa 1 (mensagem + financeiro)
.venv\Scripts\python.exe main.py --so-downloads        # so Tarefa 2 (PDFs)
.venv\Scripts\python.exe main.py --so-extracao         # so Tarefa 3 (contatos dos PDFs)
.venv\Scripts\python.exe main.py --so-djen             # so Tarefa 4 (advogados + OAB via DJEN)
.venv\Scripts\python.exe main.py --so-cna              # so Tarefa 5 (contato via CNA)
```

### Ordem RECOMENDADA das tarefas

O DJEN (Tarefa 4) é a **fonte primária do nome + OAB atual do advogado**
do Autor (API pública do CNJ). O CNA (Tarefa 5) complementa com telefone/
endereço. A extração do PDF (Tarefa 3) é complemento pra email/telefone do
timbre. Ordem ideal:

```powershell
1) .venv\Scripts\python.exe main.py                  # Tarefas 1+2 (mensagem + downloads)
2) .venv\Scripts\python.exe main.py --so-djen        # Tarefa 4 (advogados + OAB)
3) .venv\Scripts\python.exe main.py --so-cna         # Tarefa 5 (contato dos advogados)
4) .venv\Scripts\python.exe main.py --so-extracao    # Tarefa 3 (contatos do PDF)
```

Cada saída vai pra arquivo separado em `saida/`. Uma futura Fase I vai
gerar uma planilha consolidada por CIV cruzando os 3 outputs.

## Pasta de destino dos PDFs

O `.env` controla onde os PDFs são salvos. No PC do Adriano (escritório),
o drive `X:` (rótulo "BANCOS") aponta pra rede; configurado como:

```ini
DOWNLOADS_DIR=X:\ACORDO\ACORDO - CEDULA INTERNA
```

O robô cria uma subpasta por cliente: `X:\...\ACORDO - CEDULA INTERNA\FRANCISCA OLIVEIRA DA SILVA\peticao_inicial.pdf`.

Em PC novo, o default é `downloads/` (local, dentro do projeto).

## Protocolo de codegen (Fase D)

Os 3 pontos de seletor pendentes na Tarefa 1 (`fluxos/capturar_mensagem.py`)
precisam ser capturados manualmente. Procedimento:

1. **No Edge dedicado** (já aberto via `abrir-edge-bmg.bat` e logado):
   - Navegue manualmente: Jurídico → Pesquisa Causa → busca CIV de teste
     (use `CIV1387277` da planilha de 19/05) → clica no cliente → clica
     em PROCESSO [1] → clica no número do CNJ. Você deve estar na página
     do processo aberto.
   - **PARE AQUI.** Não navegue mais nada manualmente.

2. **Em outra janela do PowerShell**, rode o codegen:
   ```powershell
   cd C:\Robos\bmg-celulainterna-acordo
   .venv\Scripts\python.exe -m playwright codegen --target python --channel msedge -o gravacao_mensagem.py https://juridico.bancobmg.com.br/Paginas/Principal/_FSet_Abertura.asp
   ```
   Isso abre **outro Edge** novo. Logue de novo nesse Edge (não tem como
   reusar o login do dedicado pro codegen). Repita a navegação até a
   página do processo, e então faça os 3 cliques que faltam:
   - Click na aba "Mensagens".
   - Click na linha de mensagem com assunto "PEDIDO DE PAGAMENTO – CONDENAÇÃO INTIMAÇÃO"
     (ou só "PEDIDO DE PAGAMENTO").
   - Selecione/copie o conteúdo do box "Painel de Leitura" (basta clicar
     dentro do box pro codegen capturar o seletor).
   Feche o codegen.

3. **Abra `gravacao_mensagem.py`** e:
   - Se a senha aparecer em texto puro, **substitua por `<USUARIO>` / `<SENHA>`** antes
     de me mandar.
   - Manda o arquivo (vai entrar pra Fase D — consolido os seletores
     em `fluxos/capturar_mensagem.py`).

> O `.gitignore` já protege `gravacao*.py` — esses arquivos nunca vão pro repo.

## Tarefa 4 — Advogados via DJEN (API pública)

Consulta os advogados do polo Autor pela API pública do Comunica PJE
(`comunicaapi.pje.jus.br`), por CNJ. Não precisa de Edge nem login.
Resolve por confiança: **ENCONTRADO** (comunicação só do Autor),
**INFERIDO** (mapeamento posicional parte↔advogado em comunicação mista,
identificando o Banco pelo nome), **INCERTO** (não deu pra separar — lista
todos com aviso) e **NÃO ENCONTRADO**. Saída: `saida/advogados_djen_*.xlsx`.

> A tentativa anterior via PDPJ/jus.br (antiga "Tarefa 4") foi descartada —
> o DJEN cobre o mesmo objetivo sem exigir login.

## Tarefa 5 — Contato dos advogados via CNA

Lê a saída da Tarefa 4 e busca telefone/endereço profissional no CNA
(`cna.oab.org.br`) dos advogados ENCONTRADO/INFERIDO. Precisa do Edge
aberto (sem login no e-Xyon). Saída: `saida/advogados_djen_cna_*.xlsx`.

### Captcha do CNA

O CNA é protegido por reCAPTCHA v3 (com fallback v2 — o desafio "não sou
um robô"). O robô **nunca tenta resolver/burlar** esse desafio (violaria
os termos do CNA e arriscaria bloquear o acesso do escritório) — a
lógica de detectar, avisar e esperar um humano resolver mora em
`core/captcha.py`, reaproveitável por outros fluxos do projeto que
encontrem o mesmo tipo de desafio. Escalonamento:

1. **1ª tentativa**: se o desafio aparecer, o robô não incomoda
   ninguém — fecha o navegador e tenta de novo no outro canal
   (`msedge` ↔ `chromium`), cada um com reputação própria de reCAPTCHA.
2. **2ª tentativa**: se o desafio voltar mesmo assim, o robô pausa,
   apita e espera (até 5 min) alguém resolver na janela já aberta,
   retomando sozinho quando detecta que sumiu.

### Coluna `Status_CNA` e retomada entre execuções

Cada linha da saída grava um `Status_CNA` explícito:

| Status | Significado |
|---|---|
| `OK_COM_CONTATO` | Consulta certa, achou telefone/e-mail/endereço |
| `OK_SEM_CONTATO` | Consulta certa, advogado não cadastrou contato no CNA |
| `SEM_REGISTRO_CNA` | Busca rodou e não achou a inscrição (negativo definitivo) |
| `PENDENTE` | Não deu pra confirmar (captcha não resolvido, rate-limit, timeout) |
| `NAO_CONSULTADO_CNA` | Nem chegou a ir pro CNA (Status do DJEN veio INCERTO/NÃO ENCONTRADO/ERRO/sem OAB) |

Isso resolve a ambiguidade de antes, onde um captcha travado e um
advogado genuinamente sem contato cadastrado ficavam **indistinguíveis**
(ambos com as colunas em branco).

A cada execução, a Tarefa 5 lê **todas** as saídas
`advogados_djen_cna_*.xlsx` já geradas e pula quem já tem `Status_CNA`
definitivo (`OK_*` ou `SEM_REGISTRO_CNA`) — só reconsulta gente nova ou
quem ficou `PENDENTE`. Assim um captcha travando o lote não faz perder
o progresso: basta rodar a Tarefa 5 de novo (quantas vezes precisar)
que ela converge sozinha, consultando cada vez menos gente. Use
`--forcar` pra ignorar esse cache e reconsultar todo mundo do zero.

### Blacklist de advogados

`blacklist_advogados.txt` lista advogados que **nunca** são tratados como
do Autor (sócios LD + bancas conhecidas do Banco). Editável pela GUI
(botão "Blacklist advogados") ou direto no arquivo.

**Reuso em código.** A fonte única é o módulo `core/blacklist.py`. Qualquer
script novo (varredura de PDFs, cruzamento, análise ad hoc) que precise
distinguir advogado do Autor vs. advogado do Banco deve importar daqui:

```python
from core.blacklist import carregar_blacklist, esta_no_bmg

bl = carregar_blacklist()  # cache local do loop
for adv in advogados_extraidos:
    if esta_no_bmg(adv.nome, adv.oab, bl):
        continue
    # ...tratar como advogado do Autor
```

Não reimplemente essa lógica em outro lugar — quando o operador adiciona
uma banca do BMG pela GUI, essa mudança precisa valer pra **todo** consumidor,
não só o DJEN. `fluxos/consultar_djen.py` e `core/extrator_contatos.py` já
consomem daqui.

## Protocolo de codegen (Fase F2 — Tarefa 2)

A Tarefa 2 tem 2 stubs em `fluxos/baixar_documentos.py` que precisam de
seletores capturados via codegen. Procedimento:

1. **No Edge dedicado** (já aberto e logado), navegue manualmente até a
   página do processo (use `CIV1387277`, mesma CIV de teste da Tarefa 1).
   **Pare aqui.**

2. **Em outra janela do PowerShell**, rode:
   ```powershell
   cd C:\Robos\bmg-celulainterna-acordo
   .venv\Scripts\python.exe -m playwright codegen --target python --channel msedge -o gravacao_documentos.py https://juridico.bancobmg.com.br/Paginas/Principal/_FSet_Abertura.asp
   ```
   No Edge novo do codegen: logue, repita a navegação até a página do
   processo, e então faça:
   - Click na aba **Documentos**.
   - Click no botão de download (disquete) de **uma** das linhas — pode
     ser qualquer documento (PETIÇÃO INICIAL é o melhor, mas qualquer
     um serve). O importante é eu ver a estrutura do `onclick`/`href`.
   - **Bônus** (se conseguir): hover o mouse sobre as outras linhas
     (ou clicar em mais um disquete) — assim eu vejo se o padrão se
     repete.
   Feche o codegen.

3. **Mascare a senha** se aparecer (`<USUARIO>` / `<SENHA>`) e me manda
   o `gravacao_documentos.py`.

Eu consolido em `fluxos/baixar_documentos.py` (Fase F2), testa com
`--civ CIV1387277`, e aí roda o lote completo dos 75.

## Roadmap

| Fase | Status | Entrega |
|---|---|---|
| A | ✓ | Bootstrap |
| B | ✓ | `core/` adaptado dos irmãos |
| C | ✓ | Esqueleto Tarefa 1 (mensagem) + protocolo codegen |
| D | ✓ | Seletores Tarefa 1 (fraFichas/fraVazio/ifraMsg/#txtMsg) |
| F (prep) | ✓ | Esqueleto Tarefa 2 + main multi-tarefa + DOWNLOADS_DIR rede |
| F2 | ✓ | Seletores Tarefa 2 (codegen — abordagem UI, depreciada) |
| F2.2 | ✓ | Refluxo HTTP direto (igual extrator); 1.6MB PDF validado |
| F2.3 | ✓ | Warmup passivo + safe dialog dismiss |
| F2.4 | ✓ | Bootstrap UUID via JS click no disquete (5/5 CIVs OK) |
| E+F | ✓ | Lote completo validado em 81 + 85 CIVs |
| G | ✓ | Extração info advogado (pdfplumber + regex) |
| G2 | ✓ | Fixes: blacklist sócios, domínios institucionais, tel fallback, planilha datada, CNJ opcional |
| H (jus.br) | ✗ | Descartada — PDPJ exigia login; substituída pelo DJEN |
| Fin | ✓ | Captura aba Financeiro (Data Pagamento + Saldo) na Tarefa 1 |
| DJEN | ✓ | Tarefa 4: advogados + OAB via API Comunica PJE (ENCONTRADO/INFERIDO/INCERTO) |
| CNA | ✓ | Tarefa 5: telefone/endereço via cna.oab.org.br + blacklist editável |
| CNA2 | ✓ | `core/captcha.py` reutilizável + coluna `Status_CNA` + retomada automática entre execuções (captcha não trava mais o progresso do lote) |
| GUI | ✓ | Janela tkinter + logging + botão Atualizar do GitHub + editor de blacklist |
| I | – | Refactor / docs / Tesseract OCR (fica pra depois) |

## Versão

Aparece no início de cada execução: `>>> bmg-celulainterna-acordo vXX <<<`.
