"""
Handler generico para desafios "nao sou um robo" (reCAPTCHA v2 ou
similar) que aparecem no meio de um fluxo Playwright.

Isolado de qualquer fluxo especifico (CNA, tribunais, etc.) pra
reaproveitamento - o padrao se repete em varias automacoes deste
projeto sempre que um site de terceiro exige o desafio.

PRINCIPIO (vale pra QUALQUER consumidor deste modulo):
  NAO tentamos resolver/burlar o desafio via software - nem servico de
  terceiro, nem OCR, nem qualquer outra forma de automatizar o clique.
  Ele existe pra ser resolvido por uma PESSOA. Automatizar isso
  violaria os termos de uso do site alvo e arriscaria bloquear o
  acesso do escritorio. O papel deste modulo e so:
    1) DETECTAR o desafio na tela,
    2) AVISAR um humano (console + apito),
    3) ESPERAR ele resolver na janela ja aberta, e
    4) RETOMAR sozinho quando o desafio sumir.
"""
import threading
import time


def beep(padrao=((880, 400), (660, 400))):
    """Apito pra chamar atencao de quem estiver perto do PC.

    Roda em thread DAEMON: winsound.Beep e sincrono e pode travar se o
    dispositivo de audio estiver ocupado. Como isso pode se repetir
    periodicamente durante uma espera longa, um travamento aqui nunca
    deve segurar o fluxo principal - por isso a thread e daemon e
    qualquer excecao e engolida."""
    def _tocar():
        try:
            import winsound
            for freq, dur in padrao:
                winsound.Beep(freq, dur)
        except Exception:
            pass

    try:
        threading.Thread(target=_tocar, daemon=True).start()
    except Exception:
        pass


def desafio_visivel(page, texto_desafio: str) -> bool:
    """True se um texto de desafio (ex.: 'nao e um robo') esta visivel
    na pagina agora."""
    try:
        loc = page.get_by_text(texto_desafio, exact=False)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def aguardar_humano(page, texto_desafio: str, contexto: str = "",
                     nome_site: str = "o site",
                     timeout_s: float = 300.0,
                     intervalo_check_s: float = 2.0,
                     intervalo_aviso_s: float = 30.0) -> bool:
    """Se o desafio estiver na tela, PAUSA e espera uma pessoa resolver
    no navegador (que ja esta aberto e visivel).

    Retorna True se nao havia desafio ou se ele foi resolvido; False
    se o tempo acabou sem ninguem resolver.

    O robo NAO resolve o desafio - so avisa, espera e retoma depois.
    """
    if not desafio_visivel(page, texto_desafio):
        return True

    print()
    print("=" * 72)
    print(f"  !! {nome_site.upper()} PEDIU CONFIRMACAO 'NAO SOU UM ROBO' !!")
    print("=" * 72)
    if contexto:
        print(f"  Parou em: {contexto}")
    print("  ACAO NECESSARIA - na JANELA DO NAVEGADOR que esta aberta:")
    print("    1. Marque o quadradinho 'Nao sou um robo'.")
    print("    2. Se aparecer desafio de imagens, resolva.")
    print("    3. Pronto - o robo detecta sozinho e continua daqui.")
    if timeout_s >= 60:
        _espera_txt = f"{int(timeout_s / 60)} minuto(s)"
    else:
        _espera_txt = f"{int(timeout_s)} segundos"
    print(f"  Aguardando ate {_espera_txt}...")
    print("  (o que ja foi consultado ate agora JA ESTA SALVO)")
    print("=" * 72)
    print()
    beep()

    inicio = time.monotonic()
    ultimo_aviso = inicio
    try:
        page.bring_to_front()
    except Exception:
        pass

    while time.monotonic() - inicio < timeout_s:
        time.sleep(intervalo_check_s)
        if not desafio_visivel(page, texto_desafio):
            decorrido = int(time.monotonic() - inicio)
            print(f"  [{nome_site}] desafio resolvido em {decorrido}s - "
                  f"retomando.\n")
            time.sleep(1.0)
            return True
        agora = time.monotonic()
        if agora - ultimo_aviso >= intervalo_aviso_s:
            restante = int(timeout_s - (agora - inicio))
            print(f"  [{nome_site}] ainda aguardando o 'Nao sou um robo'... "
                  f"({restante}s restantes)")
            beep()
            ultimo_aviso = agora

    print(f"  [{nome_site}] ninguem resolveu o desafio em {int(timeout_s)}s.")
    return False
