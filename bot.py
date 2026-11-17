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

# Cores dos cartões (embeds)
COR_PADRAO = discord.Color.from_rgb(88, 101, 242)
COR_OK = discord.Color.from_rgb(67, 181, 129)
COR_ERRO = discord.Color.from_rgb(237, 66, 69)
COR_AVISO = discord.Color.from_rgb(250, 166, 26)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

FFMPEG_OPTS = {"options": "-vn"}

filas = {}          # guild_id -> deque de faixas (dicionários)
tocando_agora = {}  # guild_id -> faixa que está tocando
canais_texto = {}   # guild_id -> canal de texto do último comando
sozinho_desde = {}  # guild_id -> momento em que ficou sozinho
inativo_desde = {}  # guild_id -> momento em que parou de tocar


# ---------- Utilidades ----------

def get_fila(guild_id):
    return filas.setdefault(guild_id, deque())


def apagar(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


def curto(texto, limite=45):
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def formatar_duracao(segundos):
    if not segundos:
        return "—"
    segundos = int(segundos)
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def link_da_faixa(faixa, limite=80):
    titulo = curto(faixa["titulo"], limite)
    if faixa.get("url"):
        return f"[{titulo}]({faixa['url']})"
    return titulo


def criar_embed(titulo, descricao=None, cor=COR_PADRAO):
    return discord.Embed(title=titulo, description=descricao, color=cor)


def embed_erro(mensagem):
    return criar_embed("❌ Algo deu errado", mensagem, COR_ERRO)


def embed_aviso(mensagem):
    return criar_embed("⚠️ Atenção", mensagem, COR_AVISO)


def embed_faixa(titulo_embed, faixa, cor=COR_PADRAO, extra=None):
    e = criar_embed(titulo_embed, link_da_faixa(faixa), cor)
    e.add_field(name="Duração", value=formatar_duracao(faixa.get("duracao")), inline=True)
    if faixa.get("canal"):
        e.add_field(name="Canal", value=curto(faixa["canal"], 40), inline=True)
    if extra:
        e.add_field(name=extra[0], value=extra[1], inline=True)
    if faixa.get("thumb"):
        e.set_thumbnail(url=faixa["thumb"])
    if faixa.get("pedido_por"):
        e.set_footer(
            text=f"Pedido por {faixa['pedido_por']}",
            icon_url=faixa.get("pedido_avatar"),
        )
    return e


def limpar_servidor(guild_id):
    """Apaga a fila (e os arquivos temporários) e zera os contadores."""
    fila = get_fila(guild_id)
    for faixa in fila:
        apagar(faixa["caminho"])
    fila.clear()
    tocando_agora.pop(guild_id, None)
    sozinho_desde.pop(guild_id, None)
    inativo_desde.pop(guild_id, None)


# ---------- Busca e download ----------

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
    """Baixa o áudio e devolve um dicionário com os dados da música.
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
                return {
                    "caminho": ydl.prepare_filename(info),
                    "titulo": info.get("title") or "Sem título",
                    "url": info.get("webpage_url"),
                    "duracao": info.get("duration"),
                    "canal": info.get("uploader") or info.get("channel"),
                    "thumb": info.get("thumbnail"),
                }
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


# ---------- Reprodução ----------

async def anunciar_faixa(guild_id, faixa):
    """Avisa no canal de texto qual música começou a tocar."""
    canal = canais_texto.get(guild_id)
    if canal is None:
        return
    try:
        await canal.send(embed=embed_faixa("🎶 Tocando agora", faixa))
    except discord.HTTPException:
        pass


def tocar_proxima(guild, anunciar=False):
    """Tira a próxima música da fila e toca."""
    vc = guild.voice_client
    fila = get_fila(guild.id)

    if vc is None or not vc.is_connected():
        return
    if not fila:
        tocando_agora.pop(guild.id, None)
        return

    faixa = fila.popleft()
    tocando_agora[guild.id] = faixa

    def depois(erro):
        if erro:
            print(f"Erro ao tocar: {erro}")
        apagar(faixa["caminho"])
        # esta função roda em outra thread, então volta para o loop do bot
        client.loop.call_soon_threadsafe(tocar_proxima, guild, True)

    vc.play(discord.FFmpegPCMAudio(faixa["caminho"], **FFMPEG_OPTS), after=depois)

    if anunciar:
        client.loop.create_task(anunciar_faixa(guild.id, faixa))


async def sair_do_canal(vc, motivo, texto):
    canal = canais_texto.get(vc.guild.id)
    print(f"Saindo do canal ({vc.guild.name}): {motivo}")
    limpar_servidor(vc.guild.id)
    await vc.disconnect()
    if canal:
        try:
            await canal.send(embed=criar_embed("👋 Até mais!", texto, COR_AVISO))
        except discord.HTTPException:
            pass


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
                await sair_do_canal(
                    vc, "ficou sozinho",
                    "Saí do canal de voz porque ficou todo mundo ausente.",
                )
                continue
        else:
            sozinho_desde.pop(gid, None)

        # 2) nada tocando por muito tempo
        if vc.is_playing() or vc.is_paused():
            inativo_desde.pop(gid, None)
        else:
            inativo_desde.setdefault(gid, agora)
            if agora - inativo_desde[gid] >= TEMPO_INATIVO:
                await sair_do_canal(
                    vc, "ficou inativo",
                    "Saí do canal de voz por inatividade. Use `/tocar` para me chamar de volta.",
                )


# ---------- Eventos ----------

@client.event
async def on_voice_state_update(member, antes, depois):
    # se o bot for desconectado/expulso do canal, limpa a fila daquele servidor
    if member.id == client.user.id and depois.channel is None:
        limpar_servidor(member.guild.id)


@client.event
async def on_ready():
    await tree.sync()
    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/tocar")
    )
    if not vigiar_canais.is_running():
        vigiar_canais.start()
    print(f"Bot online como {client.user}")


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    print(f"Erro no comando: {error}")
    e = embed_erro("Ocorreu um erro inesperado. Tente novamente.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
    except discord.HTTPException:
        pass


# ---------- Comandos ----------

@tree.command(name="ping", description="Mostra a latência do bot")
async def ping(interaction: discord.Interaction):
    ms = round(client.latency * 1000)
    e = criar_embed("🏓 Pong!", f"Estou online. Latência: **{ms} ms**", COR_OK)
    await interaction.response.send_message(embed=e)


@tree.command(name="tom", description="Toca um bipe de teste por 5 segundos")
async def tom(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            embed=embed_aviso("Entre em um canal de voz primeiro."), ephemeral=True
        )
        return

    canal = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await canal.connect()
    elif vc.channel != canal:
        await vc.move_to(canal)

    if vc.is_playing() or vc.is_paused():
        await interaction.response.send_message(
            embed=embed_aviso("Já estou tocando algo. Espere terminar ou use `/pular`."),
            ephemeral=True,
        )
        return

    fonte = discord.FFmpegPCMAudio(
        "sine=frequency=440:duration=5",
        before_options="-f lavfi",
        options="-vn",
    )
    vc.play(fonte)
    await interaction.response.send_message(
        embed=criar_embed("🔊 Teste de áudio", "Tocando um bipe de 5 segundos.", COR_PADRAO)
    )


@tree.command(name="tocar", description="Toca uma música ou adiciona à fila")
@app_commands.describe(busca="Nome da música, link do YouTube ou link de faixa do Spotify")
async def tocar(interaction: discord.Interaction, busca: str):
    if not interaction.user.voice:
        await interaction.response.send_message(
            embed=embed_aviso("Entre em um canal de voz primeiro."), ephemeral=True
        )
        return

    await interaction.response.defer()
    canais_texto[interaction.guild.id] = interaction.channel

    # links do Spotify: descobre o nome da música e busca no YouTube
    if "open.spotify.com" in busca:
        busca = await asyncio.to_thread(spotify_para_busca, busca)
        if busca is None:
            await interaction.followup.send(
                embed=embed_erro(
                    "Não consegui ler esse link do Spotify. "
                    "Por enquanto só links de **faixa** funcionam."
                )
            )
            return

    canal = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await canal.connect()
    elif vc.channel != canal:
        await vc.move_to(canal)

    try:
        faixa = await asyncio.to_thread(baixar, busca)
    except Exception as e:
        print(f"Erro ao baixar '{busca}': {e}")
        await interaction.followup.send(
            embed=embed_erro(
                "Não consegui achar ou baixar essa música.\n"
                "Tente digitar o nome da música e do artista."
            )
        )
        return

    faixa["pedido_por"] = interaction.user.display_name
    faixa["pedido_avatar"] = interaction.user.display_avatar.url

    fila = get_fila(interaction.guild.id)
    ocupado = vc.is_playing() or vc.is_paused()
    fila.append(faixa)

    if ocupado:
        e = embed_faixa(
            "➕ Adicionado à fila", faixa, COR_OK, extra=("Posição", f"#{len(fila)}")
        )
    else:
        tocar_proxima(interaction.guild)
        e = embed_faixa("🎶 Tocando agora", faixa)
    await interaction.followup.send(embed=e)


@tree.command(name="fila", description="Mostra a fila de músicas")
async def mostrar_fila(interaction: discord.Interaction):
    gid = interaction.guild.id
    fila = get_fila(gid)
    atual = tocando_agora.get(gid)

    if not atual and not fila:
        await interaction.response.send_message(
            embed=criar_embed(
                "📭 A fila está vazia",
                "Use `/tocar` para adicionar músicas.",
                COR_AVISO,
            ),
            ephemeral=True,
        )
        return

    e = criar_embed("🎵 Fila de músicas")
    if atual:
        e.add_field(
            name="Tocando agora",
            value=f"{link_da_faixa(atual, 60)} `{formatar_duracao(atual.get('duracao'))}`",
            inline=False,
        )
    if fila:
        linhas = [
            f"`{i}.` {curto(f['titulo'])} `{formatar_duracao(f.get('duracao'))}`"
            for i, f in enumerate(list(fila)[:10], start=1)
        ]
        if len(fila) > 10:
            linhas.append(f"... e mais {len(fila) - 10}")
        e.add_field(name=f"Próximas ({len(fila)})", value="\n".join(linhas), inline=False)

    await interaction.response.send_message(embed=e)


@tree.command(name="pular", description="Pula para a próxima música")
async def pular(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message(
            embed=embed_aviso("Não há nada tocando agora."), ephemeral=True
        )
        return

    atual = tocando_agora.get(interaction.guild.id)
    vc.stop()  # o 'after' toca a próxima automaticamente
    descricao = f"Pulei **{curto(atual['titulo'])}**." if atual else "Música pulada."
    await interaction.response.send_message(
        embed=criar_embed("⏭️ Música pulada", descricao, COR_OK)
    )


@tree.command(name="pausar", description="Pausa a música")
async def pausar(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message(
            embed=criar_embed("⏸️ Pausado", "Use `/retomar` para continuar.", COR_PADRAO)
        )
    else:
        await interaction.response.send_message(
            embed=embed_aviso("Não há nada tocando agora."), ephemeral=True
        )


@tree.command(name="retomar", description="Retoma a música")
async def retomar(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message(
            embed=criar_embed("▶️ Retomado", "A música voltou a tocar.", COR_OK)
        )
    else:
        await interaction.response.send_message(
            embed=embed_aviso("Não há nada pausado."), ephemeral=True
        )


@tree.command(name="parar", description="Para a música, limpa a fila e sai do canal")
async def parar(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message(
            embed=embed_aviso("Não estou em um canal de voz."), ephemeral=True
        )
        return

    limpar_servidor(interaction.guild.id)
    await vc.disconnect()
    await interaction.response.send_message(
        embed=criar_embed("⏹️ Parado", "Fila limpa e saí do canal de voz.", COR_OK)
    )


client.run(TOKEN, log_level=logging.INFO)