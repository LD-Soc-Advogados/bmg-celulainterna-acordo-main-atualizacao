"""
Tarefa 3 - Extracao de contatos do(s) advogado(s) do Autor.

Por CIV: itera os PDFs em <DOWNLOADS_DIR>/<NOME_CLIENTE>/ e usa o
core/extrator_contatos pra ler info de cada um. Mescla os resultados
(prioridade: peticao_inicial.pdf > procuracao > intimacao > demais),
remove duplicatas por OAB, e gera UMA linha em saida/contatos_advogados.xlsx.

NAO depende de Edge nem de sessao Zeev. Roda offline nas pastas que
ja existem do Lote (Fase E+F). Pode ser invocada via:
    python main.py --so-extracao
"""
from datetime import datetime
from pathlib import Path

from core.config import DOWNLOADS_DIR
from core.relatorio import (
    ResultadoLinha,
    STATUS_OK,
    STATUS_ERRO,
    STATUS_JA_FECHADO,
)
from core.processo import extrair_estado_do_cnj
from core.extrator_contatos import (
    extrair_de_pdf,
    Advogado,
    RelatorioPdf,
)
from core.saida_contatos import (
    LinhaContato,
    registrar_contato,
    ja_extraido,
)
from fluxos.baixar_documentos import slug_cliente, slug_pasta_processo  # reusa o slug NTFS-safe


NOME_FLUXO = "Extrair contatos advogado"


# Ordem de prioridade pra leitura: lemos peticao primeiro (mais provavel
# de ter o timbre/OAB), depois procuracao, intimacao, e por fim o resto.
_PRIORIDADE_NOMES = [
    "procuracao", "procuração",
    "peticao_inicial", "petição_inicial",
    "intimacao", "intimação",
    "citacao", "citação",
    "copia_integral", "cópia_integral",
]


def _ordem_dos_pdfs(pdfs: list) -> list:
    """Ordena PDFs por prioridade de leitura (procuracao primeiro)."""
    def prio(p):
        nome = p.stem.lower()
        for i, padrao in enumerate(_PRIORIDADE_NOMES):
            if padrao in nome:
                return i
        return 99
    return sorted(pdfs, key=prio)


def _mesclar_advogados(rels: list) -> list:
    """Junta advogados de varios relatorios PDF, dedup por OAB.

    Prioridade: PRIMEIRO advogado que aparece com aquela OAB ganha.
    """
    vistos = {}  # oab -> Advogado
    for rel in rels:
        for adv in rel.advogados:
            chave = adv.oab or f"_no_oab_{adv.nome}"
            if not chave or chave in vistos:
                # Se ja temos esse, tenta enriquecer campos vazios
                if chave in vistos:
                    existente = vistos[chave]
                    if not existente.nome and adv.nome:
                        existente.nome = adv.nome
                    if not existente.email and adv.email:
                        existente.email = adv.email
                    if not existente.telefone and adv.telefone:
                        existente.telefone = adv.telefone
                    if not existente.celular and adv.celular:
                        existente.celular = adv.celular
                continue
            vistos[chave] = adv
    return list(vistos.values())


def _avaliar_confianca(advogados: list, nenhum_extraido: bool) -> str:
    """ALTA: 1+ adv com nome E (OAB ou email).
    MEDIA: 1+ adv com OAB mas nome em branco.
    BAIXA: so emails soltos / nada estruturado.
    """
    if not advogados or nenhum_extraido:
        return "BAIXA"
    for a in advogados:
        if a.nome and (a.oab or a.email):
            return "ALTA"
    if any(a.oab for a in advogados):
        return "MEDIA"
    return "BAIXA"


def processar_extracao(civ: str, cliente: str, cnj: str) -> tuple:
    """Le todos os PDFs da pasta do cliente, monta LinhaContato.

    Retorna (ResultadoLinha, LinhaContato_or_None).
    LinhaContato so vem != None se houve PDFs pra processar (mesmo que
    a extracao tenha sido vazia - ai marca observacao).
    """
    estado = extrair_estado_do_cnj(cnj) if cnj else ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _resultado(status: str, msg: str, linha_contato=None):
        rl = ResultadoLinha(
            civ=civ, cliente=cliente, cnj=cnj, estado=estado,
            fluxo=NOME_FLUXO, status=status, mensagem=msg,
            timestamp=timestamp,
        )
        return (rl, linha_contato)

    pasta = Path(DOWNLOADS_DIR) / slug_pasta_processo(cliente, cnj, civ)
    if not pasta.exists() or not pasta.is_dir():
        return _resultado(
            STATUS_JA_FECHADO,
            f"Pasta nao existe ({pasta.name}) - sem PDFs pra processar",
        )

    pdfs = sorted(pasta.glob("*.pdf"))
    if not pdfs:
        return _resultado(
            STATUS_JA_FECHADO,
            f"Nenhum PDF na pasta {pasta.name}",
        )

    # Le na ordem de prioridade
    pdfs_ordenados = _ordem_dos_pdfs(pdfs)
    rels: list = []
    ocr_pendentes: list = []
    erros: list = []
    for pdf in pdfs_ordenados:
        try:
            rel = extrair_de_pdf(pdf)
        except Exception as e:
            erros.append(f"{pdf.name}: {type(e).__name__}: {e}")
            continue
        rels.append(rel)
        if rel.ocr_necessario:
            ocr_pendentes.append(pdf.name)

    advogados_mesclados = _mesclar_advogados(rels)
    # Pega email_parte_autora do 1o PDF que teve algum (geralmente peticao)
    email_parte_autora = next(
        (r.email_parte_autora for r in rels if r.email_parte_autora), ""
    )
    # Fonte: nomes dos PDFs que contribuiram com advogado
    fontes = [r.pdf_path.name for r in rels if r.advogados]
    fonte_str = ", ".join(fontes) if fontes else (
        "(nenhum dado extraido)" if not ocr_pendentes else "(todos requerem OCR)"
    )

    nenhum_extraido = not advogados_mesclados and not email_parte_autora
    confianca = _avaliar_confianca(advogados_mesclados, nenhum_extraido)

    obs_parts = []
    if ocr_pendentes:
        obs_parts.append(f"OCR pendente em: {', '.join(ocr_pendentes)}")
    if erros:
        obs_parts.append(f"erros: {'; '.join(erros)}")
    if nenhum_extraido and not ocr_pendentes:
        obs_parts.append("Nao encontrado (regex nao bateu - verificar manualmente)")

    linha = LinhaContato(
        civ=civ, cliente=cliente, cnj=cnj,
        advogados=advogados_mesclados,
        email_parte_autora=email_parte_autora,
        fonte=fonte_str,
        confianca=confianca,
        ocr_pendente=", ".join(ocr_pendentes),
        observacao=" | ".join(obs_parts) if obs_parts else "",
        capturado_em=timestamp,
    )

    if nenhum_extraido and not ocr_pendentes:
        return _resultado(
            STATUS_OK,  # ainda eh OK porque marcamos a linha pra acao manual
            "Nao encontrado (verificar manualmente)",
            linha,
        )

    if nenhum_extraido and ocr_pendentes:
        return _resultado(
            STATUS_OK,
            f"OCR pendente em {len(ocr_pendentes)} PDF(s)",
            linha,
        )

    return _resultado(
        STATUS_OK,
        f"Extraidos {len(advogados_mesclados)} advogado(s); confianca={confianca}",
        linha,
    )


def registrar_se_extraido(linha_contato, caminho_xlsx: Path) -> None:
    """Helper - registra a linha na planilha de contatos."""
    if linha_contato is not None:
        registrar_contato(caminho_xlsx, linha_contato)
