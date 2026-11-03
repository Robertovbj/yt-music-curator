"""`curator mcp`: sobe o servidor MCP em stdio (para o Claude Code / Claude Desktop)."""

from curator.interfaces.cli import app


@app.command()
def mcp() -> None:
    """Servidor MCP em stdio. Registro: claude mcp add yt-music-curator -- uv --directory <repo> run curator mcp"""
    from curator.interfaces.mcp import criar_servidor

    criar_servidor().run("stdio")
