"""`curator analisar`, `curator fila` e `curator pasta-local`: worker de download e análise."""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import typer

from curator.audio import fila, proxies
from curator.config import caminhos, carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comum import aviso, banco, falhar

fila_app = typer.Typer(help="Fila de download e análise.", no_args_is_help=True)
app.add_typer(fila_app, name="fila")


def enfileirar(con) -> int:
    from curator.audio.analisador import VERSAO_ANALISADOR
    from curator.audio.modelos import hash_modelos

    return fila.enfileirar_biblioteca(con, VERSAO_ANALISADOR, hash_modelos())


def _horario(texto: str) -> datetime:
    """HH:MM de hoje (ou de amanhã, se já passou), no fuso local."""
    h, m = (int(x) for x in texto.split(":"))
    agora = datetime.now().astimezone()
    alvo = datetime.combine(agora.date(), time(h, m), agora.tzinfo)
    return (alvo if alvo > agora else alvo + timedelta(days=1)).astimezone(UTC)


@app.command()
def analisar(
    limite: int | None = typer.Option(
        None, "--limite", "-n", help="Para depois de N faixas (em cada etapa)."
    ),
    ate: str | None = typer.Option(None, "--ate", help="Para no horário HH:MM (local)."),
    aguardar: bool = typer.Option(
        False,
        "--aguardar",
        help="Com saídas em quarentena ou sem cota, espera em vez de parar (modo serviço).",
    ),
    guardar_audio: bool = typer.Option(
        False,
        "--guardar-audio",
        help="Experimental: guarda o áudio analisado para reanalisar sem baixar (o mesmo que [audio] guardar).",
    ),
    # TODO(#92): remover o --so-extras (e rever o --sem-extras) quando a biblioteca inteira tiver os extras. Ele só
    # existe para passar os extras nas faixas analisadas antes da #88; as análises novas já os rodam na mesma passada.
    so_extras: bool = typer.Option(
        False,
        "--so-extras",
        help="Só as análises extras (#88) que faltam, a partir dos áudios guardados (sem rede). Provisória (#92).",
    ),
    sem_extras: bool = typer.Option(False, "--sem-extras", help="Não calcula as análises extras (#88)."),
) -> None:
    """Consome a fila (baixa, analisa, grava e apaga ou guarda o áudio) e depois completa as análises extras.

    Os extras ([analise] extras: tags do MSD-MusiCNN, YAMNet, MAEST) vêm na mesma passada para as faixas novas; nas
    já analisadas, rodam sobre os áudios guardados (#84/#90), sem rede e sem reabrir a fila."""
    try:
        from curator.audio.analisador import Analisador
    except ImportError:
        falhar("o worker precisa do extra de análise: `uv sync --extra analise`")
    from curator.audio.trava import OutroWorkerRodando, trava_worker
    from curator.audio.worker import Compartilhado, instalar_sinais

    cfg = carregar_config()
    comp = Compartilhado(limite)
    instalar_sinais(comp)
    try:
        with trava_worker(caminhos().dados_dir / "worker.lock"):
            nomes = [] if sem_extras else _extras_com_modelos(cfg["analise"]["extras"])
            if so_extras:  # só os modelos dos extras: o núcleo (~2 GB) nem carrega
                from curator.audio.extras import AnalisadorExtras

                extras_an = (
                    AnalisadorExtras(caminhos().modelos_dir, nomes, verificar_checksums=False)
                    if nomes
                    else None
                )
            else:
                analisador = _carregar_analisador(Analisador, nomes)
                _relatar(_rodar_worker(analisador, cfg, comp, limite, ate, aguardar, guardar_audio))
                extras_an = analisador.extras
            if extras_an is not None and not comp.evento_parar.is_set():
                _complementar(extras_an, comp, limite, ate)
    except OutroWorkerRodando as e:
        typer.echo(f"{e}; nada a fazer.")  # código 0: o timer não registra falha
        return


def _relatar(rel) -> None:
    partes = (
        ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(rel.contagem.items())) or "nada processado"
    )
    typer.echo(f"{partes} · parada: {rel.motivo_parada}")
    if rel.segundos_por_faixa:
        media = sum(rel.segundos_por_faixa) / len(rel.segundos_por_faixa)
        typer.echo(f"tempo médio por faixa analisada: {media:.1f} s (download + análise)")
    if rel.proxima_liberacao:
        typer.echo(f"próxima saída disponível: {rel.proxima_liberacao.astimezone():%d/%m %H:%M}")


def _extras_com_modelos(nomes: list[str]) -> list[str]:
    """Os extras pedidos cujos modelos estão instalados; os outros ficam de fora com um aviso (o timer segue)."""
    from curator.audio import extras, modelos

    ok = []
    for n in extras.validar(nomes):
        if problemas := modelos.verificar(caminhos().modelos_dir, nomes=set(extras.EXTRAS[n].modelos)):
            aviso(f"extra {n} desligado nesta execução ({problemas[0]}); rode `curator modelos baixar`")
        else:
            ok.append(n)
    return ok


def _carregar_analisador(Analisador, nomes: list[str]):  # noqa: N803
    typer.echo(f"carregando modelos… (extras: {', '.join(nomes) or 'nenhum'})", err=True)
    try:
        return Analisador(caminhos().modelos_dir, extras=nomes)
    except RuntimeError as e:
        falhar(str(e))


def _rodar_worker(
    analisador, cfg: dict, comp, limite: int | None, ate: str | None, aguardar=False, guardar=False
):
    """Um worker por saída de rede ativa (direta + [[proxy]] do config), downloads em paralelo."""
    from curator.audio import guardados, provedores
    from curator.audio.worker import rodar_em_paralelo

    con = banco()
    if guardar:
        cfg["audio"]["guardar"] = True
    if cfg["audio"]["guardar"]:
        typer.echo(f"experimental: os áudios analisados ficam em {guardados.pasta(cfg)}", err=True)
    if n := enfileirar(con):
        typer.echo(f"{n} faixa(s) nova(s) na fila")
    proxies.sincronizar_config(con, cfg)
    saidas = [s["nome"] for s in proxies.estado(con, datetime.now(UTC))]
    typer.echo(f"{len(saidas)} saída(s): {', '.join(saidas)}", err=True)
    return rodar_em_paralelo(
        caminhos().banco, cfg, analisador, provedores.montar,
        limite=limite, ate=_horario(ate) if ate else None,
        ao_processar=lambda p: typer.echo(p.linha(), err=True), compartilhado=comp, aguardar=aguardar,
    )  # fmt: skip


def _complementar(analisador_extras, comp, limite: int | None, ate: str | None) -> None:
    """Extras que faltam nas faixas já analisadas com áudio guardado: sem rede, uma faixa por vez."""
    from curator.audio import extras

    con = banco()
    fim = _horario(ate) if ate else None
    pend = extras.pendentes(con, analisador_extras.hashes)
    if not pend:
        return
    typer.echo(f"análises extras: {len(pend)} faixa(s) com áudio guardado e extras faltando", err=True)
    c = extras.complementar(
        con, analisador_extras, limite=limite,
        deve_parar=lambda: comp.evento_parar.is_set() or (fim is not None and datetime.now(UTC) >= fim),
        ao_processar=lambda p: typer.echo(p.linha(), err=True),
    )  # fmt: skip
    typer.echo(f"extras: {c['faixas']} faixa(s), {c['erros']} com erro, {c['sem arquivo']} sem arquivo")


@fila_app.command("status")
def status() -> None:
    """Pendentes, erros, quarentenas e estimativa de dias para esvaziar a fila."""
    cfg, con = carregar_config(), banco()
    proxies.sincronizar_config(con, cfg)
    r = fila.resumo(con, cfg["fila"]["max_tentativas"])
    typer.echo("fila: " + (", ".join(f"{k}: {v}" for k, v in sorted(r["por_status"].items())) or "vazia"))
    if r["erros_por_tipo"]:
        typer.echo("erros: " + ", ".join(f"{k}: {v}" for k, v in r["erros_por_tipo"].items()))
    if r["sem_audio_premium"]:
        typer.echo(
            f"sem áudio só por serem do Music Premium: {r['sem_audio_premium']} "
            "(nenhum outro provedor teve a faixa; `curator fila repetir-sem-audio` tenta de novo)"
        )
    if r["candidatas_pendentes"]:
        typer.echo(
            f"candidatas de recomendação (modo lento) na fila: {r['candidatas_pendentes']} "
            "(só baixam depois da biblioteca)"
        )
    if r["esgotados"]:
        aviso(f"{r['esgotados']} faixa(s) esgotaram as {cfg['fila']['max_tentativas']} tentativas")
    agora = datetime.now(UTC)
    saidas = proxies.estado(con, agora)
    for s in saidas:
        q = s["quarentena_ate"]
        em_q = q and datetime.fromisoformat(q) > agora
        estado = (
            f"quarentena até {datetime.fromisoformat(q).astimezone():%d/%m %H:%M}" if em_q else "disponível"
        )
        typer.echo(
            f"  saída {s['nome']:<12} {s['url']:<28} hoje {s['uso_hoje']}/{s['cota_diaria']} · "
            f"{s['sucessos']} ok / {s['falhas']} falhas · {estado}"
        )
    from curator.audio import extras

    hashes = {n: extras.hash_extra(n) for n in extras.validar(cfg["analise"]["extras"])}
    if hashes:
        feitos = extras.resumo(con)
        partes = [
            f"{n} {feitos[n].get('ok', 0)}"
            + (f" (+{feitos[n]['erro']} com erro)" if feitos[n].get("erro") else "")
            for n in hashes
        ]
        typer.echo(
            f"análises extras: {', '.join(partes)} · {len(extras.pendentes(con, hashes))} faixa(s) com áudio "
            "guardado esperando (`curator analisar --so-extras`)"
        )
    a_fazer = r["por_status"].get("pendente", 0) + r["por_status"].get("erro", 0) - r["esgotados"]
    capacidade = sum(s["cota_diaria"] for s in saidas)
    if a_fazer and capacidade:
        typer.echo(f"estimativa: {a_fazer} faixa(s) ÷ {capacidade}/dia ≈ {-(-a_fazer // capacidade)} dia(s)")


@fila_app.command("enfileirar")
def cmd_enfileirar() -> None:
    """Põe na fila as faixas da biblioteca sem análise válida (o `sincronizar` já faz isso)."""
    typer.echo(f"{enfileirar(banco())} faixa(s) nova(s) na fila")


@fila_app.command("repetir-sem-audio")
def repetir() -> None:
    """Devolve à fila as faixas marcadas sem áudio (ex.: depois de configurar um provedor novo)."""
    typer.echo(f"{fila.repetir_sem_audio(banco())} faixa(s) de volta à fila")


@app.command("pasta-local")
def pasta_local(
    pasta: Path | None = typer.Argument(None, help="Padrão: [pasta_local].caminho do config."),
) -> None:
    """Indexa a pasta local de música (tags e, se houver `fpcalc`, fingerprint)."""
    from curator.audio.provedores.pasta_local import indexar

    pasta = pasta or (Path(p).expanduser() if (p := carregar_config()["pasta_local"]["caminho"]) else None)
    if pasta is None or not pasta.is_dir():
        falhar('informe uma pasta existente ou configure [pasta_local] caminho = "..." no config.toml')
    c = indexar(banco(), pasta)
    typer.echo(
        f"pasta local: {c['novos']} indexado(s), {c['inalterados']} inalterado(s), {c['removidos']} removido(s)"
    )
