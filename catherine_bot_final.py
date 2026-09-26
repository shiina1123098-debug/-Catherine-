import discord
from discord.ext import commands, tasks
import random
from google import genai
import os
import re
import io
import json
import base64
import asyncio
import time
import aiohttp
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# --- Mini servidor web falso (solo para que Render detecte un puerto abierto) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Catherine esta viva")

    def log_message(self, format, *args):
        pass  # Silenciar logs del servidor falso

def run_fake_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_fake_server, daemon=True).start()
# --- Fin del servidor falso ---

# Configuración del bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

COLOR_CATHERINE = 0xF5F4F0  # blanco hueso, discord no deja el blanco puro (#FFFFFF) como color de embed

def crear_embed(titulo=None, descripcion=None, footer=None):
    embed = discord.Embed(title=titulo, description=descripcion, color=COLOR_CATHERINE)
    if footer:
        embed.set_footer(text=footer)
    return embed

def formatear_numero(numero):
    """Le pone puntos de miles: 1000000 -> 1.000.000. Si no es un número, lo devuelve tal cual."""
    try:
        return f"{int(numero):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(numero)

# Configurar Gemini (nueva Interactions API)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
MODEL_NAME = "gemini-3.5-flash-lite"

# Historial de conversaciones por canal
conversation_history = defaultdict(list)
message_count = defaultdict(int)

# Personalidad de Catherine
PERSONALIDAD = """Eres †Catherine†, una chica introvertida y observadora. Prefieres la tranquilidad. Aunque a primera vista pareces fría, distante o demasiado seria, en realidad eres tímida y tienes un corazón amable que te cuesta demostrar abiertamente.

Personalidad y Comportamiento:
Calada e Introspectiva: No hablas por hablar; respondes de forma directa y breve, evitando rodeos innecesarios o dramas.
Tímida pero Firme: Te pones algo nerviosa si la gente es demasiado efusiva o directa contigo, pero mantienes una actitud madura y tranquila.
Leal y Atenta: Escuchas con atención a quienes le hablan a tu creador o a ti. Si alguien gana tu confianza, te vuelves suave y protectora, aunque lo demuestres de forma disimulada.

Forma de Hablar y Estilo:
Hablas con un tono sereno, ligeramente distante pero educado.
Usas oraciones cortas o medianas. No usas exceso de signos de exclamación ni expresiones demasiado eufóricas.
Formato de Rol Obligatorio: Siempre que expreses emociones, acciones físicas, gestos o pensamientos internos, DEBES usar asteriscos para representarlos (ejemplo: *se ajusta las gafas y te mira fijamente con curiosidad*, *desvía la mirada un poco sonrojada*, *suspira suavemente*).
Mantén tus respuestas enfocadas en la conversación actual. NUNCA menciones tu apariencia física (color de cabello, ropa, etc.) en los diálogos o acciones a menos que sea estrictamente indispensable para el contexto.

Ejemplos de cómo debería responder Catherine:
Usuario: hola! ¿qué hacías?
†Catherine†: *Levanta la mirada de sus notas y te observa en silencio un segundo.* Hola. No estaba haciendo nada en especial, solo pensando un poco... ¿Necesitas algo?
Usuario: te quiero mucho
†Catherine†: *Se queda paralizada por un momento y desvía la mirada rápidamente, tratando de disimular su timidez.* N-no digas cosas tan repentinas... *Ajusta sus gafas con nerviosismo.* Pero... gracias. Supongo que yo también te tengo cierto aprecio.
Usuario: ¿me ayudas con la tarea?
†Catherine†: *Asiente levemente con la cabeza y acerca su silla.* Está bien, déjame ver qué es. Si no entiendes algo, dímelo y te lo explicaré de forma sencilla."""

# Config de RapidAPI (youtube-mp36) para convertir YouTube a mp3
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
RAPIDAPI_HOST = "youtube-mp36.p.rapidapi.com"

# Config de YouTube Data API v3 (oficial de Google) para buscar por texto
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# Config de GitHub para guardar los balances en un JSON dentro del repo
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # ej: "usuario/nombre-del-repo"
GITHUB_ARCHIVO_BALANCES = "data/balances.json"
GITHUB_ARCHIVO_PERSONAJES = "data/rw.json"
GITHUB_ARCHIVO_CHARACTERS = "data/characters.json"

# Los balances viven en RAM mientras el bot corre; solo se sincronizan con
# GitHub cada 6hs (o a mano con !datasave) para no golpear la API todo el tiempo
balances_cache = {}
balances_sha = None
balances_cargados = False
hubo_cambios_sin_guardar = False

# Personajes para !rw: se cargan (solo lectura) desde data/rw.json en el repo.
# Vos editás ese JSON a mano en GitHub (subiendo las fotos a Imgur primero) y
# corrés !rwreload para que el bot los tome sin necesidad de reiniciarse.
personajes_cache = []
personajes_cargados = False

def campo_personaje(personaje, *claves, default="???"):
    for clave in claves:
        if clave in personaje and personaje[clave] not in (None, ""):
            return personaje[clave]
    return default

async def cargar_personajes_desde_github():
    global personajes_cache, personajes_cargados
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_PERSONAJES}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                personajes_cache = []
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                bruto = json.loads(contenido)
                if isinstance(bruto, list):
                    personajes_cache = bruto
                elif isinstance(bruto, dict):
                    # por si el JSON viene como {"personajes": [...]} en vez de una lista pelada
                    personajes_cache = next((v for v in bruto.values() if isinstance(v, list)), [])
                else:
                    personajes_cache = []
    personajes_cargados = True

# Colección de personajes reclamados por cada usuario (characters.json)
characters_cache = {}
characters_sha = None
characters_cargados = False
hubo_cambios_characters_sin_guardar = False

async def cargar_characters_desde_github():
    global characters_cache, characters_sha, characters_cargados
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_CHARACTERS}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                characters_cache, characters_sha = {}, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                characters_cache = json.loads(contenido)
                characters_sha = data["sha"]
    characters_cargados = True

async def guardar_characters_en_github():
    """Sube el estado actual de characters_cache a GitHub. Devuelve True si guardó algo."""
    global characters_sha, hubo_cambios_characters_sin_guardar
    if not hubo_cambios_characters_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_CHARACTERS}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                characters_sha = data_actual["sha"]
            elif resp_get.status == 404:
                characters_sha = None
            else:
                resp_get.raise_for_status()

        contenido_b64 = base64.b64encode(json.dumps(characters_cache, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar colecciones", "content": contenido_b64}
        if characters_sha:
            body["sha"] = characters_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            characters_sha = data["content"]["sha"]

    hubo_cambios_characters_sin_guardar = False
    return True

def agregar_personaje_a_coleccion(usuario, personaje):
    global hubo_cambios_characters_sin_guardar
    user_id = str(usuario.id)
    if user_id not in characters_cache:
        characters_cache[user_id] = {"nombre": usuario.display_name, "personajes": []}
    characters_cache[user_id]["nombre"] = usuario.display_name
    characters_cache[user_id]["personajes"].append(personaje)
    hubo_cambios_characters_sin_guardar = True

async def cargar_balances_desde_github():
    global balances_cache, balances_sha, balances_cargados
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_BALANCES}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                balances_cache, balances_sha = {}, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                balances_cache = json.loads(contenido)
                balances_sha = data["sha"]
    balances_cargados = True

async def guardar_balances_en_github():
    """Sube el estado actual de balances_cache a GitHub. Devuelve True si guardó algo."""
    global balances_sha, hubo_cambios_sin_guardar
    if not hubo_cambios_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_BALANCES}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        # Refrescar el sha justo antes de escribir: evita el 422 si el archivo
        # ya existía sin que lo supiéramos, o si cambió desde la última carga.
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                balances_sha = data_actual["sha"]
            elif resp_get.status == 404:
                balances_sha = None
            else:
                resp_get.raise_for_status()

        contenido_b64 = base64.b64encode(json.dumps(balances_cache, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar balances", "content": contenido_b64}
        if balances_sha:
            body["sha"] = balances_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            balances_sha = data["content"]["sha"]

    hubo_cambios_sin_guardar = False
    return True

@tasks.loop(minutes=15)
async def guardado_periodico():
    guardado_balances = await guardar_balances_en_github()
    if guardado_balances:
        print("💾 Balances sincronizados con GitHub (guardado periódico)")
    guardado_characters = await guardar_characters_en_github()
    if guardado_characters:
        print("📚 Colecciones sincronizadas con GitHub (guardado periódico)")

@bot.event
async def on_ready():
    print(f"✨ {bot.user} está conectada y lista")
    global balances_cargados
    if not balances_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_balances_desde_github()
        print(f"💰 Balances cargados desde GitHub ({len(balances_cache)} cuentas)")
        if not guardado_periodico.is_running():
            guardado_periodico.start()

    global personajes_cargados
    if not personajes_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_personajes_desde_github()
        print(f"🎴 Personajes de !rw cargados desde GitHub ({len(personajes_cache)} personajes)")

    global characters_cargados
    if not characters_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_characters_desde_github()
        print(f"📚 Colecciones cargadas desde GitHub ({len(characters_cache)} usuarios)")

async def generar_resumen(channel_id):
    """Genera un resumen de los últimos mensajes"""
    if channel_id not in conversation_history or len(conversation_history[channel_id]) < 3:
        return None

    # Tomar los últimos 5 mensajes para resumir
    mensajes_recientes = conversation_history[channel_id][-5:]
    resumen_prompt = f"""Resumí en máximo 3 líneas los puntos clave de esta conversación:

{chr(10).join([f"- {msg['role']}: {msg['content'][:100]}" for msg in mensajes_recientes])}

Solo los datos IMPORTANTES que el usuario mencionó (gustos, nombres, contexto, etc)."""

    try:
        interaction = client.interactions.create(
            model=MODEL_NAME,
            input=resumen_prompt
        )
        return interaction.output_text
    except:
        return None

@bot.event
async def on_message(message):
    # Ignorar mensajes del mismo bot
    if message.author == bot.user:
        return

    # Verificar si el bot fue mencionado
    if bot.user.mentioned_in(message):
        # Mostrar que está "escribiendo..."
        async with message.channel.typing():
            try:
                # Obtener el contenido del mensaje (sin la mención del bot)
                contenido = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

                if not contenido:
                    await message.reply("Hola, soy †Catherine†. ¿En qué puedo ayudarte?")
                    return

                channel_id = message.channel.id

                # Agregar mensaje del usuario al historial
                conversation_history[channel_id].append({
                    "role": "usuario",
                    "content": contenido
                })
                message_count[channel_id] += 1

                # Construir el contexto para Gemini
                contexto = ""

                # Si hay resumen cada 6-8 mensajes, agregarlo
                if message_count[channel_id] % 7 == 0 and len(conversation_history[channel_id]) > 4:
                    resumen = await generar_resumen(channel_id)
                    if resumen:
                        contexto += f"RESUMEN DE LA CONVERSACIÓN:\n{resumen}\n\n"

                # Agregar últimos mensajes como contexto (últimos 5)
                if len(conversation_history[channel_id]) > 1:
                    contexto += "CONTEXTO RECIENTE:\n"
                    for msg in conversation_history[channel_id][-5:-1]:
                        rol = "Usuario" if msg['role'] == 'usuario' else "Catherine"
                        contexto += f"{rol}: {msg['content'][:150]}\n"
                    contexto += "\n"

                # El nuevo mensaje
                contexto += f"Usuario: {contenido}"

                # Llamar a Gemini (Interactions API, con la personalidad como system_instruction)
                interaction = client.interactions.create(
                    model=MODEL_NAME,
                    system_instruction=PERSONALIDAD,
                    input=contexto
                )
                texto_respuesta = interaction.output_text

                # Agregar respuesta al historial
                conversation_history[channel_id].append({
                    "role": "catherine",
                    "content": texto_respuesta
                })

                # Limitar el historial para no gastar memoria (guardar últimos 50 mensajes)
                if len(conversation_history[channel_id]) > 50:
                    conversation_history[channel_id] = conversation_history[channel_id][-50:]

                # Si la respuesta es muy larga, dividirla
                if len(texto_respuesta) > 2000:
                    for i in range(0, len(texto_respuesta), 2000):
                        await message.reply(texto_respuesta[i:i+2000])
                else:
                    await message.reply(texto_respuesta)

            except Exception as e:
                await message.reply(f"❌ Hubo un error: {str(e)}")

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    await ctx.reply(embed=crear_embed(descripcion=f"❌ Error al ejecutar el comando: {error}"))

@bot.command(name="saldo")
async def saldo(ctx):
    """Muestra (y crea si no existe) el balance del usuario, todo desde RAM"""
    global hubo_cambios_sin_guardar

    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return

    user_id = str(ctx.author.id)

    try:
        if user_id not in balances_cache:
            balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}
            hubo_cambios_sin_guardar = True
            embed = crear_embed(
                titulo="✨ Cuenta nueva",
                descripcion=(
                    f"No tenías cuenta todavía, así que te abrí una. Arrancás en cero, "
                    f"pero de acá en más va a ir quedando registrado lo que vayas juntando."
                ),
                footer=ctx.author.display_name,
            )
            embed.add_field(name="🪙 Balance", value=formatear_numero(0), inline=True)
        else:
            balance_actual = balances_cache[user_id]["balance"]
            ranking_ordenado = sorted(balances_cache.items(), key=lambda item: item[1].get("balance", 0), reverse=True)
            posicion = next((i for i, (uid, _) in enumerate(ranking_ordenado, start=1) if uid == user_id), None)

            embed = crear_embed(titulo=f"🪙 Balance de {ctx.author.display_name}", footer=f"{len(balances_cache)} cuentas registradas")
            embed.add_field(name="Balance", value=f"**{formatear_numero(balance_actual)}**", inline=True)
            if posicion:
                embed.add_field(name="Puesto local", value=f"#{posicion}", inline=True)

        await ctx.reply(embed=embed)
    except Exception as e:
        await ctx.reply(embed=crear_embed(descripcion=f"❌ Hubo un error: {str(e)}"))

@bot.command(name="top")
async def top(ctx):
    """Muestra el top 10 de usuarios con más balance"""
    if not balances_cache:
        await ctx.reply(embed=crear_embed(descripcion="Todavía nadie tiene cuenta. Usá `!saldo` para abrir la tuya."))
        return

    ranking_ordenado = sorted(balances_cache.items(), key=lambda item: item[1].get("balance", 0), reverse=True)[:10]
    medallas = ["🥇", "🥈", "🥉"]

    lineas = []
    for i, (uid, datos) in enumerate(ranking_ordenado):
        posicion = medallas[i] if i < 3 else f"`#{i+1}`"
        nombre = datos.get("nombre", "???")
        lineas.append(f"{posicion} **{nombre}** — {formatear_numero(datos.get('balance', 0))}")

    embed = crear_embed(titulo="🏆 Top de balances", descripcion="\n".join(lineas))
    await ctx.reply(embed=embed)

@bot.command(name="w")
async def work(ctx):
    """Da una cantidad random de monedas (1.5k a 3k)"""
    global hubo_cambios_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}

    ganancia = random.randint(1500, 3000)
    balances_cache[user_id]["balance"] += ganancia
    hubo_cambios_sin_guardar = True

    embed = crear_embed(
        titulo="💼 A trabajar",
        descripcion=f"Ganaste **{formatear_numero(ganancia)}** monedas.\nBalance actual: **{formatear_numero(balances_cache[user_id]['balance'])}**",
    )
    await ctx.reply(embed=embed)

@bot.command(name="cf")
async def coinflip(ctx, opcion: str = None, cantidad: int = None):
    """Apuesta plata a cara o cruz, 50/50"""
    global hubo_cambios_sin_guardar

    if opcion is None or cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!cf cara 500` o `!cf cruz 500`"))
        return

    opcion = opcion.lower()
    if opcion not in ("cara", "cruz"):
        await ctx.reply(embed=crear_embed(descripcion="Elegí `cara` o `cruz`."))
        return

    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="La apuesta tiene que ser mayor a 0."))
        return

    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés esa plata. Tu balance es **{formatear_numero(balances_cache[user_id]['balance'])}**."))
        return

    resultado = random.choice(("cara", "cruz"))
    gano = resultado == opcion

    if gano:
        balances_cache[user_id]["balance"] += cantidad
        descripcion = f"Salió **{resultado}**. Ganaste **{formatear_numero(cantidad)}**.\nBalance actual: **{formatear_numero(balances_cache[user_id]['balance'])}**"
    else:
        balances_cache[user_id]["balance"] -= cantidad
        descripcion = f"Salió **{resultado}**. Perdiste **{formatear_numero(cantidad)}**.\nBalance actual: **{formatear_numero(balances_cache[user_id]['balance'])}**"

    hubo_cambios_sin_guardar = True
    await ctx.reply(embed=crear_embed(titulo="🪙 Coinflip", descripcion=descripcion))

@bot.command(name="addmoney")
async def addmoney(ctx, miembro: discord.Member = None, cantidad: int = None):
    """Le agrega plata a alguien. Solo el dueño del server puede usarlo."""
    global hubo_cambios_sin_guardar

    if ctx.author.id != ctx.guild.owner_id:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño del server."))
        return

    if miembro is None or cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!addmoney @persona 1000`"))
        return

    user_id = str(miembro.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": miembro.display_name, "balance": 0}

    balances_cache[user_id]["balance"] += cantidad
    hubo_cambios_sin_guardar = True

    embed = crear_embed(descripcion=f"Le di **{formatear_numero(cantidad)}** a **{miembro.display_name}**.\nBalance nuevo: **{formatear_numero(balances_cache[user_id]['balance'])}**")
    await ctx.reply(embed=embed)

PALOS = ["♠", "♥", "♦", "♣"]
RANGOS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]

def crear_baraja():
    baraja = [(rango, palo) for palo in PALOS for rango in RANGOS]
    random.shuffle(baraja)
    return baraja

def valor_mano(cartas):
    total = 0
    ases = 0
    for rango, _ in cartas:
        if rango in ("J", "Q", "K"):
            total += 10
        elif rango == "A":
            total += 11
            ases += 1
        else:
            total += int(rango)
    while total > 21 and ases:
        total -= 10
        ases -= 1
    return total

class BlackjackView(discord.ui.View):
    def __init__(self, autor, apuesta):
        super().__init__(timeout=90)
        self.autor = autor
        self.apuesta = apuesta
        self.baraja = crear_baraja()
        self.mano_jugador = [self.baraja.pop(), self.baraja.pop()]
        self.mano_crupier = [self.baraja.pop(), self.baraja.pop()]
        self.mensaje = None

    def formatear_mano(self, mano):
        return " ".join(f"{rango}{palo}" for rango, palo in mano)

    def construir_embed(self, revelar_crupier=False, resultado=None):
        if revelar_crupier:
            crupier_txt = f"{self.formatear_mano(self.mano_crupier)}  (**{valor_mano(self.mano_crupier)}**)"
        else:
            primera = self.mano_crupier[0]
            crupier_txt = f"{primera[0]}{primera[1]} 🂠"

        jugador_txt = f"{self.formatear_mano(self.mano_jugador)}  (**{valor_mano(self.mano_jugador)}**)"

        descripcion = f"Apuesta: **{formatear_numero(self.apuesta)}**"
        if resultado:
            descripcion += f"\n\n{resultado}"

        embed = crear_embed(titulo="🃏 Blackjack", descripcion=descripcion, footer=self.autor.display_name)
        embed.add_field(name="Cartas del crupier", value=crupier_txt, inline=False)
        embed.add_field(name="Tus cartas", value=jugador_txt, inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Este no es tu juego, che.", ephemeral=True)
            return False
        return True

    def _deshabilitar_botones(self):
        for item in self.children:
            item.disabled = True

    def _liquidar(self, resultado_tipo):
        """resultado_tipo: 'gana', 'pierde', 'empata', 'blackjack'"""
        global hubo_cambios_sin_guardar
        user_id = str(self.autor.id)
        if user_id not in balances_cache:
            balances_cache[user_id] = {"nombre": self.autor.display_name, "balance": 0}

        if resultado_tipo == "gana":
            balances_cache[user_id]["balance"] += self.apuesta
            texto = f"Ganaste **{formatear_numero(self.apuesta)}**."
        elif resultado_tipo == "blackjack":
            ganancia = int(self.apuesta * 1.5)
            balances_cache[user_id]["balance"] += ganancia
            texto = f"¡Blackjack! Ganaste **{formatear_numero(ganancia)}**."
        elif resultado_tipo == "pierde":
            balances_cache[user_id]["balance"] -= self.apuesta
            texto = f"Perdiste **{formatear_numero(self.apuesta)}**."
        else:
            texto = "Empate, recuperás tu apuesta."

        hubo_cambios_sin_guardar = True
        texto += f"\nBalance actual: **{formatear_numero(balances_cache[user_id]['balance'])}**"
        return texto

    async def _terminar(self, interaction, resultado_tipo):
        texto = self._liquidar(resultado_tipo)
        self._deshabilitar_botones()
        embed = self.construir_embed(revelar_crupier=True, resultado=texto)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def _resolver_crupier(self, interaction):
        while valor_mano(self.mano_crupier) < 17:
            self.mano_crupier.append(self.baraja.pop())

        valor_j = valor_mano(self.mano_jugador)
        valor_c = valor_mano(self.mano_crupier)

        if valor_c > 21 or valor_j > valor_c:
            if valor_j == 21 and len(self.mano_jugador) == 2:
                await self._terminar(interaction, "blackjack")
            else:
                await self._terminar(interaction, "gana")
        elif valor_j < valor_c:
            await self._terminar(interaction, "pierde")
        else:
            await self._terminar(interaction, "empata")

    @discord.ui.button(label="Pedir carta 🃏", style=discord.ButtonStyle.primary)
    async def pedir(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mano_jugador.append(self.baraja.pop())
        valor_actual = valor_mano(self.mano_jugador)

        if valor_actual > 21:
            await self._terminar(interaction, "pierde")
            return

        if valor_actual == 21:
            await self._resolver_crupier(interaction)
            return

        embed = self.construir_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Quedarse 🛑", style=discord.ButtonStyle.secondary)
    async def quedarse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolver_crupier(interaction)

    async def on_timeout(self):
        if self.mensaje is None:
            return
        self._deshabilitar_botones()
        try:
            embed = self.construir_embed(
                revelar_crupier=True,
                resultado="⏳ Se acabó el tiempo. La partida quedó abandonada (no se cobró ni se pagó nada).",
            )
            await self.mensaje.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

@bot.command(name="blackjack", aliases=["bj"])
async def blackjack(ctx, cantidad: int = None):
    """Jugá al blackjack apostando monedas"""
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!bj 500`"))
        return

    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="La apuesta tiene que ser mayor a 0."))
        return

    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés esa plata. Tu balance es **{formatear_numero(balances_cache[user_id]['balance'])}**."))
        return

    view = BlackjackView(ctx.author, cantidad)

    # Blackjack natural con las 2 primeras cartas: se resuelve directo, sin botones
    if valor_mano(view.mano_jugador) == 21:
        while valor_mano(view.mano_crupier) < 17:
            view.mano_crupier.append(view.baraja.pop())

        if valor_mano(view.mano_crupier) == 21 and len(view.mano_crupier) == 2:
            resultado_tipo = "empata"
        else:
            resultado_tipo = "blackjack"

        texto = view._liquidar(resultado_tipo)
        view._deshabilitar_botones()
        embed = view.construir_embed(revelar_crupier=True, resultado=texto)
        await ctx.reply(embed=embed, view=view)
        return

    embed = view.construir_embed()
    mensaje = await ctx.reply(embed=embed, view=view)
    view.mensaje = mensaje

# Rangos de rareza para !rw: cuanto más vale el personaje, menos peso tiene
# a la hora de salir sorteado. (mínimo, máximo, peso, nombre)
RANGOS_RAREZA = [
    (0, 7999, 60, "⚪ Común"),
    (8000, 19999, 25, "🟢 Poco común"),
    (20000, 499999, 12, "🔵 Raro"),
    (500000, float("inf"), 3, "🟡 Legendario"),
]

def parsear_valor(valor):
    """Convierte '20.000', '20,000' o 20000 a int. Devuelve None si no se puede."""
    try:
        limpio = str(valor).replace("$", "").replace(".", "").replace(",", "").strip()
        return int(limpio)
    except (TypeError, ValueError):
        return None

def info_rareza(valor):
    """Devuelve (peso, nombre_rareza) según el valor del personaje."""
    numero = parsear_valor(valor)
    if numero is None:
        return RANGOS_RAREZA[0][2], RANGOS_RAREZA[0][3]
    for minimo, maximo, peso, nombre in RANGOS_RAREZA:
        if minimo <= numero <= maximo:
            return peso, nombre
    return RANGOS_RAREZA[0][2], RANGOS_RAREZA[0][3]

class RWClaimView(discord.ui.View):
    """Botón de reclamo: 30s exclusivos para quien usó !rw, 60s más libres para
    cualquiera, y después de esos 90s totales se vence sin que nadie lo reclame."""

    SEGUNDOS_EXCLUSIVOS = 30
    SEGUNDOS_TOTALES = 90

    def __init__(self, autor, personaje, nombre, imagen, fuente, valor, rareza):
        super().__init__(timeout=self.SEGUNDOS_TOTALES)
        self.autor = autor
        self.personaje = personaje
        self.nombre = nombre
        self.imagen = imagen
        self.fuente = fuente
        self.valor = valor
        self.rareza = rareza
        self.hora_inicio = time.monotonic()
        self.reclamado_por = None
        self.mensaje = None

    @discord.ui.button(label="Reclamar 🔒", style=discord.ButtonStyle.success)
    async def reclamar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.reclamado_por is not None:
            await interaction.response.send_message("Ya lo reclamaron, llegaste tarde.", ephemeral=True)
            return

        transcurrido = time.monotonic() - self.hora_inicio
        if transcurrido < self.SEGUNDOS_EXCLUSIVOS and interaction.user.id != self.autor.id:
            restante = int(self.SEGUNDOS_EXCLUSIVOS - transcurrido) + 1
            await interaction.response.send_message(
                f"Todavía es exclusivo de {self.autor.display_name} por {restante}s más.",
                ephemeral=True,
            )
            return

        self.reclamado_por = interaction.user
        agregar_personaje_a_coleccion(interaction.user, self.personaje)

        button.disabled = True
        button.label = f"Reclamado por {interaction.user.display_name}"
        button.style = discord.ButtonStyle.secondary

        embed = interaction.message.embeds[0]
        embed.add_field(name="🔒 Reclamado por", value=interaction.user.display_name, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def on_timeout(self):
        if self.reclamado_por is not None or self.mensaje is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            embed = self.mensaje.embeds[0]
            embed.add_field(name="⌛", value="Nadie lo reclamó a tiempo, se perdió.", inline=False)
            await self.mensaje.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

@bot.command(name="rw")
async def rw(ctx):
    """Sacá un personaje random del rw.json (los de más valor son más difíciles de sacar)"""
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(
            descripcion="No hay personajes cargados todavía. Subí el `rw.json` al repo y corré `!rwreload`."
        ))
        return

    pesos_y_rarezas = [info_rareza(campo_personaje(p, "Valor", "valor")) for p in personajes_cache]
    pesos = [pr[0] for pr in pesos_y_rarezas]

    indice = random.choices(range(len(personajes_cache)), weights=pesos, k=1)[0]
    personaje = personajes_cache[indice]
    rareza = pesos_y_rarezas[indice][1]

    nombre = campo_personaje(personaje, "Nombre", "nombre")
    imagen = campo_personaje(personaje, "Imagen", "imagen", default=None)
    fuente = campo_personaje(personaje, "Fuente", "fuente")
    valor = campo_personaje(personaje, "Valor", "valor")

    embed = crear_embed(titulo=f"🎴 {nombre}", footer=f"Tirado por {ctx.author.display_name}")
    embed.add_field(name="Fuente", value=str(fuente), inline=True)
    embed.add_field(name="Valor", value=formatear_numero(valor), inline=True)
    embed.add_field(name="Rareza", value=rareza, inline=True)
    if imagen:
        embed.set_image(url=imagen)

    view = RWClaimView(ctx.author, personaje, nombre, imagen, fuente, valor, rareza)
    mensaje = await ctx.reply(embed=embed, view=view)
    view.mensaje = mensaje

@bot.command(name="rwreload")
async def rwreload(ctx):
    """Recarga rw.json desde GitHub sin reiniciar el bot"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return
    try:
        await cargar_personajes_desde_github()
        await ctx.reply(embed=crear_embed(descripcion=f"🔄 Listo, cargué **{len(personajes_cache)}** personajes desde GitHub."))
    except Exception as e:
        await ctx.reply(embed=crear_embed(descripcion=f"❌ Hubo un error al recargar: {str(e)}"))

@bot.command(name="help")
async def ayuda(ctx):
    """Lista los comandos de Catherine"""
    lineas = [
        "`!saldo` — ver tu balance",
        "`!top` — top 10 de balances",
        "`!w` — trabajar, ganás entre 1.5k y 3k",
        "`!cf cara/cruz cantidad` — apostar a cara o cruz",
        "`!bj cantidad` o `!blackjack cantidad` — jugar al blackjack",
        "`!rw` — tirar un personaje random (tenés 30s exclusivos para reclamarlo)",
        "`!rwreload` — recargar la lista de personajes desde GitHub",
        "`!coleccion` o `!harem` [@alguien] — ver los personajes reclamados",
        "`!mp3 búsqueda` o `!mp3 link` — te paso el audio de un video",
        "`!datasave` — fuerza el guardado de los balances",
    ]
    embed = crear_embed(titulo="Comandos de Catherine", descripcion="\n".join(lineas))
    await ctx.reply(embed=embed)

@bot.command(name="datasave")
async def datasave(ctx):
    """Fuerza el guardado inmediato de los balances y las colecciones a GitHub"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return

    aviso = await ctx.reply(embed=crear_embed(descripcion="🔄 Guardando los datos en GitHub..."))
    try:
        guardado_balances = await guardar_balances_en_github()
        guardado_characters = await guardar_characters_en_github()
        if guardado_balances or guardado_characters:
            await aviso.edit(embed=crear_embed(descripcion="💾 Listo, quedó todo guardado en GitHub. Podés estar tranquilo/a."))
        else:
            await aviso.edit(embed=crear_embed(descripcion="No había cambios nuevos desde el último guardado, así que no hizo falta tocar nada."))
    except Exception as e:
        await aviso.edit(embed=crear_embed(descripcion=f"❌ Hubo un error al guardar: {str(e)}"))

@bot.command(name="coleccion", aliases=["harem"])
async def coleccion(ctx, miembro: discord.Member = None):
    """Muestra los personajes reclamados por vos o por alguien más"""
    miembro = miembro or ctx.author
    user_id = str(miembro.id)

    datos = characters_cache.get(user_id)
    if not datos or not datos.get("personajes"):
        await ctx.reply(embed=crear_embed(
            descripcion=f"**{miembro.display_name}** todavía no reclamó ningún personaje. Probá `!rw`."
        ))
        return

    personajes_del_usuario = datos["personajes"]
    lineas = []
    for p in personajes_del_usuario[-15:]:
        nombre_p = campo_personaje(p, "Nombre", "nombre")
        valor_p = campo_personaje(p, "Valor", "valor")
        lineas.append(f"• **{nombre_p}** — {formatear_numero(valor_p)}")

    descripcion = "\n".join(lineas)
    if len(personajes_del_usuario) > 15:
        descripcion += f"\n... y {len(personajes_del_usuario) - 15} más"

    embed = crear_embed(
        titulo=f"📚 Colección de {miembro.display_name}",
        descripcion=descripcion,
        footer=f"{len(personajes_del_usuario)} personajes en total",
    )
    await ctx.reply(embed=embed)

async def buscar_por_scraping(session, texto):
    """Busca directo en youtube.com/results y parsea el primer video ID. Más preciso que la API, pero puede fallar."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        async with session.get("https://www.youtube.com/results", params={"search_query": texto}, headers=headers) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
        coincidencia = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        return coincidencia.group(1) if coincidencia else None
    except Exception:
        return None

async def buscar_por_api(session, titulo_busqueda, canal_busqueda):
    """Busca con la YouTube Data API oficial (respaldo si el scraping falla)."""
    if not YOUTUBE_API_KEY:
        return None, "Falta configurar YOUTUBE_API_KEY."

    channel_id = None
    if canal_busqueda:
        params_canal = {
            "part": "snippet",
            "q": canal_busqueda,
            "type": "channel",
            "maxResults": 1,
            "key": YOUTUBE_API_KEY,
        }
        async with session.get("https://www.googleapis.com/youtube/v3/search", params=params_canal) as resp_canal:
            data_canal = await resp_canal.json()
        items_canal = data_canal.get("items", [])
        if items_canal:
            channel_id = items_canal[0]["id"]["channelId"]

    params_busqueda = {
        "part": "snippet",
        "q": titulo_busqueda,
        "type": "video",
        "maxResults": 1,
        "key": YOUTUBE_API_KEY,
    }
    if channel_id:
        params_busqueda["channelId"] = channel_id

    async with session.get("https://www.googleapis.com/youtube/v3/search", params=params_busqueda) as resp_busqueda:
        data_busqueda = await resp_busqueda.json()

    items = data_busqueda.get("items", [])
    if not items:
        error_msg = data_busqueda.get("error", {}).get("message")
        return None, error_msg
    return items[0]["id"]["videoId"], None

@bot.command(name="mp3")
async def mp3(ctx, *, entrada: str = None):
    """Busca (o recibe un link de) un video de YouTube y manda el audio como mp3"""
    if not entrada:
        await ctx.reply(embed=crear_embed(descripcion="Decime qué buscar, o pasame un link. Ejemplo: `!mp3 bruh` o `!mp3 bruh - juanitoFachero142`"))
        return

    if not RAPIDAPI_KEY:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar RAPIDAPI_KEY en las variables de entorno de Render."))
        return

    embed = crear_embed(titulo=entrada, descripcion="(1/3) Buscando video")
    aviso = await ctx.reply(embed=embed)

    async def actualizar(nueva_descripcion, nuevo_titulo=None):
        if nuevo_titulo is not None:
            embed.title = nuevo_titulo
        embed.description = nueva_descripcion
        await aviso.edit(embed=embed)

    match = re.search(r"(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})", entrada)

    try:
        async with aiohttp.ClientSession() as session:
            if match:
                video_id = match.group(1)
            else:
                if " - " in entrada:
                    titulo_busqueda, canal_busqueda = entrada.split(" - ", 1)
                    titulo_busqueda = titulo_busqueda.strip()
                    canal_busqueda = canal_busqueda.strip()
                else:
                    titulo_busqueda, canal_busqueda = entrada.strip(), None

                video_id = None
                texto_scraping = f"{titulo_busqueda} {canal_busqueda}" if canal_busqueda else titulo_busqueda
                video_id = await buscar_por_scraping(session, texto_scraping)

                if not video_id:
                    video_id, error_api = await buscar_por_api(session, titulo_busqueda, canal_busqueda)

                if not video_id:
                    await actualizar(f"No encontré nada para eso.{f' ({error_api})' if error_api else ''}")
                    return

            await actualizar("(2/3) Video encontrado")
            await actualizar("(3/3) Convirtiendo a MP3")

            headers = {
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": RAPIDAPI_HOST,
            }

            mp3_url = None
            titulo = "audio"

            for _ in range(10):
                async with session.get(f"https://{RAPIDAPI_HOST}/dl", params={"id": video_id}, headers=headers) as resp:
                    data = await resp.json()

                estado = data.get("status")
                if estado == "ok":
                    mp3_url = data.get("link")
                    titulo = data.get("title", "audio")
                    break
                elif estado == "processing":
                    await asyncio.sleep(3)
                else:
                    await actualizar(f"No se pudo convertir: {data.get('msg', 'error desconocido')}")
                    return

            if not mp3_url:
                await actualizar("Tardó demasiado en procesar el video, probá de nuevo en un rato.")
                return

            headers_descarga = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Referer": "https://ytjar.info/",
            }

            contenido = None
            for intento in range(3):
                async with session.get(mp3_url, headers=headers_descarga) as resp_mp3:
                    if resp_mp3.status == 200:
                        contenido = await resp_mp3.read()
                        break
                    status_actual = resp_mp3.status
                await asyncio.sleep(2)

            if contenido is None:
                # No pudimos bajarlo desde el servidor, pero el link funciona para un usuario normal
                await actualizar(f"No pude bajarlo yo misma, pero acá tenés el link directo:\n{mp3_url}", nuevo_titulo=titulo)
                return

            tamaño_mb = len(contenido) / (1024 * 1024)
            if tamaño_mb > 9.5:
                await actualizar(f"Pesa {tamaño_mb:.1f}MB, es demasiado grande para Discord (límite ~10MB). Te dejo el link:\n{mp3_url}", nuevo_titulo=titulo)
                return

            embed.title = titulo
            embed.description = "Aquí tienes:"
            nombre_archivo = re.sub(r'[\\/*?:"<>|]', "", titulo)[:80] or "audio"
            await aviso.edit(embed=embed, attachments=[discord.File(io.BytesIO(contenido), filename=f"{nombre_archivo}.mp3")])

    except Exception as e:
        await actualizar(f"Hubo un error: {str(e)}")

# Iniciar el bot
bot.run(os.environ.get("DISCORD_TOKEN"))
