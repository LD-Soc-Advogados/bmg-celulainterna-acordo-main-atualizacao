"""
Migracao unica: renomeia pastas legadas de download pra incluir o CNJ,
resolvendo o bug de homonimos que misturava PDFs entre processos.

Contexto:
    Ate o commit desta migracao, `slug_pasta_processo` era so
    `slug_cliente(cliente)`. Se dois autores homonimos aparecessem em
    CIVs diferentes, os PDFs caiam na MESMA pasta e um sobrescrevia o
    outro. A idempotencia por tamanho (>1KB) piorava: pulava download
    legitimo por casar arquivo do outro processo.

O que este script faz:
    1. Le TODAS as `Planilha Envio de casos*.xlsx` da raiz.
    2. Monta o mapa `slug_cliente(cliente) -> [(civ, cnj), ...]`.
    3. Escaneia cada subpasta em DOWNLOADS_DIR:
       - 1 CNJ conhecido    -> renomeia `NOME/` para `NOME - CNJ/`.
       - >=2 CNJs conhecidos -> HOMONIMO SUSPEITO: NAO renomeia, so
         reporta. Voce vai precisar rebaixar essas CIVs pra separar
         os documentos.
       - 0 CNJs conhecidos   -> pasta orfa (CIV nao aparece em nenhuma
         planilha atual). Deixa como esta e reporta.

Uso:
    # Dry-run (nao renomeia, so mostra o plano)
    .venv\\Scripts\\python.exe -m scripts.migrar_pastas_com_cnj

    # Aplica de fato
    .venv\\Scripts\\python.exe -m scripts.migrar_pastas_com_cnj --apply
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Import direto do core (o script eh chamado com -m scripts.migrar_pastas_com_cnj
# a partir da raiz do projeto)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import DOWNLOADS_DIR, ROOT  # noqa: E402
from core.planilha_entrada import ler_planilha_casos  # noqa: E402
from fluxos.baixar_documentos import (  # noqa: E402
    slug_cliente,
    slug_cnj,
    slug_pasta_processo,
)


def _coletar_civs_das_planilhas(root: Path):
    """Le TODAS as `Planilha Envio de casos*.xlsx` da raiz e devolve mapa
        slug_cliente(cliente) -> list[(civ, cnj)]
    deduplicado por (civ, cnj_slug)."""
    planilhas = sorted(
        p for p in root.glob("Planilha Envio de casos*.xlsx")
        if not p.name.startswith("~$")
    )
    if not planilhas:
        print(f"AVISO: nenhuma 'Planilha Envio de casos*.xlsx' em {root}")
        return {}

    mapa = defaultdict(list)
    vistos = set()
    for planilha in planilhas:
        try:
            casos = ler_planilha_casos(planilha)
        except Exception as e:
            print(f"  ! Falha lendo {planilha.name}: {type(e).__name__}: {e}")
            continue
        for c in casos:
            cliente = getattr(c, "cliente", "") or ""
            civ = getattr(c, "civ", "") or ""
            cnj = slug_cnj(getattr(c, "cnj", "") or "")
            chave = slug_cliente(cliente)
            if not chave:
                continue
            dedup = (civ, cnj)
            if dedup in vistos:
                continue
            vistos.add(dedup)
            mapa[chave].append((civ, cnj))
        print(f"  * {planilha.name}: {len(casos)} caso(s) lidos")
    return dict(mapa)


def _pasta_ja_tem_cnj(nome: str) -> bool:
    """True se o nome da pasta ja segue o padrao 'X - CNJ' novo (contem
    ' - ' seguido de digitos/pontos/hifens que parecem CNJ)."""
    if " - " not in nome:
        return False
    parte_final = nome.rsplit(" - ", 1)[-1]
    if not parte_final:
        return False
    # CNJ tem >=15 chars entre digito/ponto/hifen
    return (len(parte_final) >= 15
            and all(c.isdigit() or c in ".-" for c in parte_final)
            and any(c.isdigit() for c in parte_final))


def _executar(base: Path, apply: bool):
    if not base.exists():
        print(f"ERRO: DOWNLOADS_DIR nao existe: {base}")
        return 1
    if not base.is_dir():
        print(f"ERRO: DOWNLOADS_DIR nao eh diretorio: {base}")
        return 1

    print(f">>> DOWNLOADS_DIR: {base}")
    print(f">>> Modo: {'APLICAR (rename)' if apply else 'DRY-RUN (so relata)'}")
    print()
    print(">>> Coletando CIVs das planilhas...")
    mapa = _coletar_civs_das_planilhas(ROOT)
    if not mapa:
        print("Sem dados de planilha - nada a fazer.")
        return 1
    print(f">>> {sum(len(v) for v in mapa.values())} (civ, cnj) unicas em "
          f"{len(mapa)} slug(s) de cliente distintos")
    print()

    subs = sorted([p for p in base.iterdir() if p.is_dir()],
                  key=lambda p: p.name.lower())
    print(f">>> {len(subs)} subpasta(s) em DOWNLOADS_DIR pra examinar")
    print()

    renomear = []       # (pasta_orig, nome_novo, civ, cnj_ou_marcador)
    homonimos = []      # (pasta_orig, list[(civ, cnj)])
    orfaos = []         # pasta_orig
    ja_migradas = []    # pasta_orig

    for sub in subs:
        nome = sub.name
        if _pasta_ja_tem_cnj(nome):
            ja_migradas.append(sub)
            continue

        # Chave do lookup: MESMO transformador que produziu a pasta antiga.
        entradas = mapa.get(nome, [])
        if not entradas:
            orfaos.append(sub)
            continue

        com_cnj = [(civ, cnj) for civ, cnj in entradas if cnj]
        # Se todos os CNJs sao vazios (planilha sem CNJ): usa CIV como
        # sufixo pra ainda garantir unicidade. So eh homonimo suspeito
        # se houver >1 CIV distinta na mesma pasta.
        if not com_cnj:
            civs_distintas = sorted({civ for civ, _ in entradas})
            if len(civs_distintas) == 1:
                civ_unica = civs_distintas[0]
                nome_novo = slug_pasta_processo(nome, "", civ_unica)
                renomear.append((sub, nome_novo, civ_unica, "(sem CNJ)"))
            else:
                homonimos.append((sub, entradas))
            continue

        # Dedup por cnj_slug (mesma CIV pode ter aparecido em varias
        # planilhas com o mesmo CNJ).
        cnjs_distintos = sorted({cnj for _, cnj in com_cnj})
        if len(cnjs_distintos) == 1:
            civ_unica = com_cnj[0][0]
            cnj = cnjs_distintos[0]
            nome_novo = slug_pasta_processo(nome, cnj, civ_unica)
            renomear.append((sub, nome_novo, civ_unica, cnj))
        else:
            homonimos.append((sub, com_cnj))

    print("=" * 70)
    print(f"RESUMO")
    print("=" * 70)
    print(f"  Ja migradas (padrao 'NOME - CNJ'): {len(ja_migradas)}")
    print(f"  A renomear (CNJ ou CIV conhecidos):{len(renomear)}")
    print(f"  HOMONIMOS (>=2 CIVs, contaminadas):{len(homonimos)}")
    print(f"  Orfas (nao aparecem em planilha):  {len(orfaos)}")
    print()

    if renomear:
        print("--- A RENOMEAR ---")
        for sub, novo, civ, cnj in renomear[:50]:
            print(f"  {sub.name}  -->  {novo}   ({civ})")
        if len(renomear) > 50:
            print(f"  ... mais {len(renomear) - 50} pasta(s)")
        print()

    if homonimos:
        print("--- HOMONIMOS SUSPEITOS (documentos misturados; rebaixar!) ---")
        for sub, entradas in homonimos:
            civs = ", ".join(f"{civ}={cnj}" for civ, cnj in entradas)
            print(f"  {sub.name}")
            print(f"    -> CIVs distintas: {civs}")
        print()

    if orfaos:
        print("--- ORFAS (CIV nao aparece em nenhuma planilha atual) ---")
        for sub in orfaos[:20]:
            print(f"  {sub.name}")
        if len(orfaos) > 20:
            print(f"  ... mais {len(orfaos) - 20}")
        print()

    if not apply:
        print("(dry-run: rode de novo com --apply pra executar os renames)")
        return 0

    if not renomear:
        print("Nada a renomear.")
        return 0

    print("=" * 70)
    print("APLICANDO RENAMES")
    print("=" * 70)
    ok, falhas = 0, []
    for sub, novo, civ, _ in renomear:
        destino = sub.parent / novo
        if destino.exists():
            falhas.append(f"{sub.name}: destino ja existe ({destino.name})")
            continue
        try:
            sub.rename(destino)
            print(f"  OK {sub.name}  -->  {destino.name}   ({civ})")
            ok += 1
        except Exception as e:
            falhas.append(f"{sub.name}: {type(e).__name__}: {e}")

    print()
    print(f">>> {ok}/{len(renomear)} pasta(s) renomeada(s)")
    if falhas:
        print(f">>> {len(falhas)} falha(s):")
        for f in falhas:
            print(f"     - {f}")
    return 0 if not falhas else 2


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true",
                   help="Executa os renames (default: dry-run).")
    args = p.parse_args(argv)
    return _executar(Path(DOWNLOADS_DIR), args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
