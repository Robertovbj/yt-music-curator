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
    limite: int | None = typer.Option(None, "--limite", "-n", help="Para depois de N faixas."),
    ate: str | None = typer.Option(None, "--ate", help="Para no horário HH:MM (local)."),
) -> None:
    """Consome a fila: baixa o áudio, analisa com o Essentia, grava e apaga o áudio."""
    try:
        from curator.audio.analisador import Analisador
    except ImportError:
        falhar("o worker precisa do extra de análise: `uv sync --extra analise`")
    from curator.audio import provedores
    from curator.audio.worker import Worker

    cfg, con = carregar_config(), banco()
    if n := enfileirar(con):
        typer.echo(f"{n} faixa(s) nova(s) na fila")
    typer.echo("carregando modelos…", err=True)
    try:
        analisador = Analisador(caminhos().modelos_dir)
    except RuntimeError as e:
        falhar(str(e))
    worker = Worker(con, cfg, analisador, provedores.montar(cfg, con))
    worker.instalar_sinais()
    rel = worker.rodar(limite=limite, ate=_horario(ate) if ate else None)

    partes = (
        ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(rel.contagem.items())) or "nada processado"
    )
    typer.echo(f"{partes} · parada: {rel.motivo_parada}")
    if rel.segundos_por_faixa:
        media = sum(rel.segundos_por_faixa) / len(rel.segundos_por_faixa)
        typer.echo(f"tempo médio por faixa analisada: {media:.1f} s (download + análise)")
    if rel.proxima_liberacao:
        typer.echo(f"próxima saída disponível: {rel.proxima_liberacao.astimezone():%d/%m %H:%M}")


@fila_app.command("status")
def status() -> None:
    """Pendentes, erros, quarentenas e estimativa de dias para esvaziar a fila."""
    cfg, con = carregar_config(), banco()
    proxies.sincronizar_config(con, cfg)
    r = fila.resumo(con, cfg["fila"]["max_tentativas"])
    typer.echo("fila: " + (", ".join(f"{k}: {v}" for k, v in sorted(r["por_status"].items())) or "vazia"))
    if r["erros_por_tipo"]:
        typer.echo("erros: " + ", ".join(f"{k}: {v}" for k, v in r["erros_por_tipo"].items()))
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
