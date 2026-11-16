import os
import re
import html
import uuid
import time
import asyncio
import logging
import urllib.request
from collections import deque

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
import yt_dlp

load_dotenv(override=True)
TOKEN = os.getenv("DISCORD_TOKEN")

PASTA = "musicas"
os.makedirs(PASTA, exist_ok=True)

# Tempo (em segundos) até o bot sair sozinho do canal
TEMPO_SOZINHO = 60   # ninguém mais no canal de voz
TEMPO_INATIVO = 300  # nada tocando (5 minutos)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

FFMPEG_OPTS = {"options": "-vn"}

filas = {}          # guild_id -> deque de (caminho_do_arquivo, titulo)
tocando_agora = {}  # guild_id -> titulo
sozinho_desde = {}  # guild_id -> momento em que ficou sozinho
inativo_desde = {}  # guild_id -> momento em que parou de tocar


def get_fila(guild_id):
    return filas.setdefault(guild_id, deque())


def apagar(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


def limpar_servidor(guild_id):
    """Apaga a fila (e os arquivos temporários) e zera os contadores."""
    fila = get_fila(guild_id)
    for caminho, _ in fila:
        apagar(caminho)
    fila.clear()
    tocando_agora.pop(guild_id, None)
    sozinho_desde.pop(guild_id, None)
    inativo_desde.pop(guild_id, None)


def buscar_urls(query: str):
    """Procura até 5 vídeos no YouTube. Se a busca voltar vazia, tenta de
    novo tirando uma palavra de cada vez (o YouTube às vezes não devolve
    nada para certos títulos)."""
    palavras = query.split()
    variantes = [query]
    if len(palavras) > 2:
        for i in range(len(palavras)):
            variantes.append(" ".join(palavras[:i] + palavras[i + 1:]))

    for v in variantes[:10]:
        try:
            with yt_dlp.YoutubeDL(
                {"quiet": True, "extract_flat": True, "skip_download": True}
            ) as ydl:
                resultado = ydl.extract_info(f"ytsearch5:{v}", download=False)
        except Exception as e:
            print(f"Erro na busca '{v}': {e}")
            continue

        urls = [
            e["url"] for e in (resultado.get("entries") or []) if e and e.get("url")
        ]
        if urls:
            if v != query:
                print(f"Busca '{query}' veio vazia; usando '{v}'")
            return urls
    return []


def baixar(query: str):
    """Baixa o áudio e devolve (caminho_do_arquivo, titulo).
    Em buscas por nome, tenta até 5 resultados até um funcionar."""
    if re.match(r"https?://", query):
        alvos = [query]
    else:
        alvos = buscar_urls(query)
        if not alvos:
            raise ValueError("nenhum resultado para essa busca")

    ultimo_erro = None
    for alvo in alvos:
        nome = uuid.uuid4().hex
        opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "outtmpl": os.path.join(PASTA, f"{nome}.%(ext)s"),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(alvo, download=True)
                return ydl.prepare_filename(info), info["title"]
        except Exception as e:
            ultimo_erro = e
    raise ultimo_erro


def spotify_para_busca(url: str):
    """Lê o título da página pública da faixa e devolve um texto de busca."""
    if "/track/" not in url:
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            pagina = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Erro ao abrir Spotify: {e}")
        return None

    m = re.search(r"<title>(.*?)</title>", pagina, re.S)
    if not m:
        return None
    texto = html.unescape(m.group(1))
    texto = texto.replace("| Spotify", "")
    texto = re.sub(
        r"song and lyrics by|song by|música e letra de", " ", texto, flags=re.I
    )
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto + " audio"


def tocar_proxima(guild):
    """Tira a próxima música da fila e toca."""
    vc = guild.voice_client
    fila = get_fila(guild.id)

    if vc is None or not vc.is_connected():
        return
    if not fila:
        tocando_agora.pop(guild.id, None)
        return

    caminho, titulo = fila.popleft()
    tocando_agora[guild.id] = titulo

    def depois(erro):
        if erro:
            print(f"Erro ao tocar: {erro}")
        apagar(caminho)
        # esta função roda em outra thread, então volta para o loop do bot
        client.loop.call_soon_threadsafe(tocar_proxima, guild)

    vc.play(discord.FFmpegPCMAudio(caminho, **FFMPEG_OPTS), after=depois)


async def sair_do_canal(vc, motivo):
    print(f"Saindo do canal ({vc.guild.name}): {motivo}")
    limpar_servidor(vc.guild.id)
    await vc.disconnect()


@tasks.loop(seconds=15)
async def vigiar_canais():
    """A cada 15 segundos confere se o bot está sozinho ou parado."""
    agora = time.time()
    for vc in list(client.voice_clients):
        if not vc.is_connected() or vc.channel is None:
            continue
        gid = vc.guild.id

        # 1) sozinho no canal (só o próprio bot)
        outros = [uid for uid in vc.channel.voice_states if uid != client.user.id]
        if not outros:
            sozinho_desde.setdefault(gid, agora)
            if agora - sozinho_desde[gid] >= TEMPO_SOZINHO:
                await sair_do_canal(vc, "ficou sozinho")
                continue
        else:
            sozinho_desde.pop(gid, None)

        # 2) nada tocando por muito tempo
        if vc.is_playing() or vc.is_paused():
            inativo_desde.pop(gid, None)
        else:
            inativo_desde.setdefault(gid, agora)
            if agora - inativo_desde[gid] >= TEMPO_INATIVO:
                await sair_do_canal(vc, "ficou inativo")


@client.event
async def on_voice_state_update(member, antes, depois):
    # se o bot for desconectado/expulso do canal, limpa a fila daquele servidor
    if member.id == client.user.id and depois.channel is None:
        limpar_servidor(member.guild.id)


@client.event
async def on_ready():
    await tree.sync()
    if not vigiar_canais.is_running():
        vigiar_canais.start()
    print(f"Bot online como {client.user}")


@tree.command(name="ping", description="Testa se o bot está vivo")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓")


@tree.command(name="tom", description="Toca um bipe de teste por 5 segundos")
async def tom(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "Entre em um canal de voz.", ephemeral=True
        )
        return

    canal = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await canal.connect()
    elif vc.channel != canal:
        await vc.move_to(canal)

    fonte = discord.FFmpegPCMAudio(
        "sine=frequency=440:duration=5",
        before_options="-f lavfi",
        options="-vn",
    )
    vc.play(fonte)
    await interaction.response.send_message("🔊 Tocando tom de teste")


@tree.command(name="tocar", description="Toca uma música ou adiciona à fila")
async def tocar(interaction: discord.Interaction, busca: str):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "Entre em um canal de voz primeiro.", ephemeral=True
        )
        return

    await interaction.response.defer()

    # links do Spotify: descobre o nome da música e busca no YouTube
    if "open.spotify.com" in busca:
        busca = await asyncio.to_thread(spotify_para_busca, busca)
        if busca is None:
            await interaction.followup.send(
                "Não consegui ler esse link do Spotify. Só links de faixa funcionam. 😕"
            )
            return

    canal = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await canal.connect()
    elif vc.channel != canal:
        await vc.move_to(canal)

    try:
        caminho, titulo = await asyncio.to_thread(baixar, busca)
    except Exception as e:
        await interaction.followup.send(
            "Não consegui achar ou baixar essa música. 😕 "
            "Tente digitar o nome da música e do artista."
        )
        print(f"Erro ao baixar '{busca}': {e}")
        return

    fila = get_fila(interaction.guild.id)
    ocupado = vc.is_playing() or vc.is_paused()
    fila.append((caminho, titulo))

    if ocupado:
        await interaction.followup.send(
            f"➕ Adicionado à fila (posição {len(fila)}): **{titulo}**"
        )
    else:
        tocar_proxima(interaction.guild)
        await interaction.followup.send(f"🎶 Tocando: **{titulo}**")


@tree.command(name="fila", description="Mostra a fila de músicas")
async def mostrar_fila(interaction: discord.Interaction):
    fila = get_fila(interaction.guild.id)
    atual = tocando_agora.get(interaction.guild.id)

    if not atual and not fila:
        await interaction.response.send_message("A fila está vazia.", ephemeral=True)
        return

    linhas = []
    if atual:
        linhas.append(f"🎶 **Tocando agora:** {atual}")
    for i, (_, titulo) in enumerate(list(fila)[:10], start=1):
        linhas.append(f"{i}. {titulo}")
    if len(fila) > 10:
        linhas.append(f"... e mais {len(fila) - 10}")

    await interaction.response.send_message("\n".join(linhas))


@tree.command(name="pular", description="Pula para a próxima música")
async def pular(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # o 'after' toca a próxima automaticamente
        await interaction.response.send_message("⏭️ Pulada")
    else:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)


@tree.command(name="pausar", description="Pausa a música")
async def pausar(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Pausado")
    else:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)


@tree.command(name="retomar", description="Retoma a música")
async def retomar(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Retomado")
    else:
        await interaction.response.send_message("Nada pausado.", ephemeral=True)


@tree.command(name="parar", description="Para a música, limpa a fila e sai do canal")
async def parar(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    limpar_servidor(interaction.guild.id)

    if vc:
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Parado")
    else:
        await interaction.response.send_message("Não estou em um canal.", ephemeral=True)


client.run(TOKEN, log_level=logging.INFO)