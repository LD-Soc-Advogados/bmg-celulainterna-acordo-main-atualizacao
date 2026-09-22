"""
Helpers de acesso aos frames aninhados do e-Xyon.

A árvore real do site:
  page > iframe[name=FraDetalhe] > frame[name=fraConsulta]
                                     > frame[name=fraPesquisa]    (form de busca)
                                     > frame[name=fraResultado]   (lista de clientes)
                                     > frame[name=Frame_Processo] (página do cliente)
                                     > frame[name=fraFichas]      (abas: Mensagens, Andamentos, ...)
                                     > frame[name=fraVazio]       (conteúdo da aba ativa)
                                       > iframe[name=ifraMsg]     (lista de mensagens, dentro de fraVazio)

`fraFichas` e `fraVazio` se alternam: ao clicar numa aba (ex.: Mensagens),
o e-Xyon troca `fraFichas` por `fraVazio` no DOM. O painel de leitura
(#txtMsg) fica em `fraVazio`, e a lista clicável de mensagens fica no
`ifraMsg` dentro de `fraVazio`.

NUNCA refazer essa parte — está validado em produção pelo bmg-extrator
e pela gravacao_mensagem.py de 19/05 (fichas/vazio/ifraMsg).
"""
from pathlib import Path

from playwright.sync_api import Page


def _frame_pesquisa(page: Page):
    """Retorna o frame com o form de busca (cmbBusca + txtPesquisa)."""
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="fraPesquisa"]').content_frame
    )


def _frame_resultado(page: Page):
    """Retorna o frame onde aparece a lista de resultados (clientes encontrados)."""
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="fraResultado"]').content_frame
    )


def _frame_cliente(page: Page):
    """Retorna o frame da página do cliente (Frame_Processo).

    Mesma chain que `_frame_processo` interna usada por
    abrir_processo_do_cliente — extraída pra cá pra evitar duplicação.
    """
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="Frame_Processo"]').content_frame
    )


def _frame_novo_processo(page: Page):
    """Frame com a página do processo selecionado (após clicar o CNJ).

    `Frame_Processo` lista os processos do cliente; ao clicar no link do
    CNJ, o e-Xyon carrega o processo escolhido em `Frame_Novo_Processo`
    (chain `FraDetalhe > fraConsulta > Frame_Novo_Processo`).

    É aqui que mora o botão `#btnDocumento` que abre o popup com a lista
    de documentos do Zeev. Descoberto via gravacao_documentos.py de 19/05.
    """
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="Frame_Novo_Processo"]').content_frame
    )


def _frame_fichas(page: Page):
    """Frame com as abas do processo (Mensagens, Andamentos, ...).

    Visível logo após o CNJ ser clicado em `_frame_cliente`.
    Some quando uma aba é selecionada (substituído por `fraVazio`).
    """
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="fraFichas"]').content_frame
    )


def _frame_vazio(page: Page):
    """Frame com o conteúdo da aba ativa (substitui `fraFichas` ao clicar aba).

    Contém o painel de leitura `#txtMsg` (quando a aba Mensagens está ativa)
    e o iframe `ifraMsg` com a lista clicável de mensagens.
    """
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="fraVazio"]').content_frame
    )


def _frame_ifra_msg(page: Page):
    """Lista de mensagens (iframe interno dentro de `fraVazio`).

    Cada linha clicável é um assunto de mensagem; clicar abre a
    mensagem no painel `#txtMsg` (que fica em `_frame_vazio`).
    """
    return (
        page.locator('iframe[name="FraDetalhe"]').content_frame
        .locator('frame[name="fraConsulta"]').content_frame
        .locator('frame[name="fraVazio"]').content_frame
        .locator('iframe[name="ifraMsg"]').content_frame
    )


def localizar_em_frames(page: Page, seletores: list):
    """Procura uma lista de seletores em todos os frames da página.
    Retorna (locator, frame, seletor) do primeiro hit, ou (None, None, None)."""
    for frame in page.frames:
        for sel in seletores:
            try:
                loc = frame.locator(sel)
                if loc.count() > 0:
                    return loc.first, frame, sel
            except Exception:
                continue
    return None, None, None


def listar_estrutura_de_frame(frame, salvar_em: Path = None):
    """Imprime inputs/selects/botões de um frame específico (útil pra debug
    quando um seletor novo não bate)."""
    linhas = []
    def out(s: str):
        print(s)
        linhas.append(s)

    out(f"\n--- Estrutura do frame '{frame.name}' ---")
    out(f"URL: {frame.url}\n")
    try:
        inputs = frame.evaluate(
            """() => Array.from(document.querySelectorAll('input')).map(i => ({
                name: i.name || '', id: i.id || '', type: i.type || '',
                placeholder: i.placeholder || ''
            }))"""
        )
        selects = frame.evaluate(
            """() => Array.from(document.querySelectorAll('select')).map(s => ({
                name: s.name || '', id: s.id || '',
                options: Array.from(s.options).map(o => ({value: o.value, text: o.text}))
            }))"""
        )
        botoes = frame.evaluate(
            """() => Array.from(
                document.querySelectorAll('button, input[type=submit], input[type=button], a.btn')
            ).map(b => ({
                tag: b.tagName, type: b.type || '',
                text: (b.innerText || b.value || '').trim().slice(0,60),
                id: b.id || '', name: b.name || ''
            }))"""
        )
    except Exception as e:
        out(f"  erro ao ler: {e}")
        return

    out(f"  Inputs ({len(inputs)}):")
    for i in inputs:
        out(f"    type={i['type']:10} name={i['name']:25} id={i['id']:25} placeholder='{i['placeholder']}'")
    out(f"  Selects ({len(selects)}):")
    for s in selects:
        out(f"    name={s['name']:25} id={s['id']:25}")
        for o in s['options']:
            out(f"      OPCAO value='{o['value']}' text='{o['text']}'")
    out(f"  Botoes ({len(botoes)}):")
    for b in botoes:
        out(f"    tag={b['tag']:6} type={b['type']:10} id={b['id']:20} text='{b['text']}'")

    if salvar_em is not None:
        try:
            salvar_em.parent.mkdir(parents=True, exist_ok=True)
            salvar_em.write_text("\n".join(linhas), encoding="utf-8")
            print(f">>> Estrutura do frame salva em: {salvar_em}")
        except Exception as e:
            print(f"(falhou ao salvar: {e})")
