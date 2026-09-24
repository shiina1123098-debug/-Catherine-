import discord
from discord.ext import commands
from google import genai
import os
import re
import io
import asyncio
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

@bot.event
async def on_ready():
    print(f"✨ {bot.user} está conectada y lista")

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
    await ctx.reply(f"❌ Error al ejecutar el comando: {error}")

@bot.command(name="mp3")
async def mp3(ctx, *, entrada: str = None):
    """Busca (o recibe un link de) un video de YouTube y manda el audio como mp3"""
    if not entrada:
        await ctx.reply("Decime qué buscar, o pasame un link. Ejemplo: `!mp3 bruh sound effect`")
        return

    if not RAPIDAPI_KEY:
        await ctx.reply("❌ Falta configurar RAPIDAPI_KEY en las variables de entorno de Render.")
        return

    aviso = await ctx.reply("Buscando el video, dame un segundo...")

    match = re.search(r"(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})", entrada)

    try:
        async with aiohttp.ClientSession() as session:
            if match:
                video_id = match.group(1)
            else:
                # No es un link: buscarlo con la YouTube Data API oficial
                if not YOUTUBE_API_KEY:
                    await aviso.edit(content="❌ Falta configurar YOUTUBE_API_KEY para poder buscar por texto (o pasame directamente un link).")
                    return

                params_busqueda = {
                    "part": "snippet",
                    "q": entrada,
                    "type": "video",
                    "maxResults": 1,
                    "key": YOUTUBE_API_KEY,
                }
                async with session.get("https://www.googleapis.com/youtube/v3/search", params=params_busqueda) as resp_busqueda:
                    data_busqueda = await resp_busqueda.json()

                items = data_busqueda.get("items", [])
                if not items:
                    error_msg = data_busqueda.get("error", {}).get("message")
                    await aviso.edit(content=f"❌ No encontré nada para eso.{f' ({error_msg})' if error_msg else ''}")
                    return

                video_id = items[0]["id"]["videoId"]

            await aviso.edit(content="Convirtiendo el audio, dame un segundo más...")

            headers = {
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": RAPIDAPI_HOST,
            }

            mp3_url = None
            titulo = "audio"

            # La API tarda unos segundos en procesar el video, reintentamos unas cuantas veces
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
                    await aviso.edit(content=f"❌ No se pudo convertir: {data.get('msg', 'error desconocido')}")
                    return

            if not mp3_url:
                await aviso.edit(content="❌ Tardó demasiado en procesar el video, probá de nuevo en un rato.")
                return

            # Reintentar la descarga unas cuantas veces: a veces el link tarda
            # un instante en estar disponible del todo, aunque la API ya diga "ok"
            contenido = None
            for intento in range(5):
                async with session.get(mp3_url, headers={"User-Agent": "Mozilla/5.0"}) as resp_mp3:
                    if resp_mp3.status == 200 and "audio" in resp_mp3.headers.get("Content-Type", ""):
                        contenido = await resp_mp3.read()
                        break
                    texto_error = (await resp_mp3.text())[:200]
                    status_actual = resp_mp3.status
                await asyncio.sleep(2)

            if contenido is None:
                await aviso.edit(content=f"❌ El link del audio no respondió bien (status {status_actual}): {texto_error}\n\nLink que dio la API: {mp3_url}")
                return

            tamaño_mb = len(contenido) / (1024 * 1024)
            if tamaño_mb > 9.5:
                await aviso.edit(content=f"❌ **{titulo}** pesa {tamaño_mb:.1f}MB, es demasiado grande para Discord (límite ~10MB).")
                return

            await aviso.edit(content=f"Listo: **{titulo}**")
            nombre_archivo = re.sub(r'[\\/*?:"<>|]', "", titulo)[:80] or "audio"
            await ctx.send(file=discord.File(io.BytesIO(contenido), filename=f"{nombre_archivo}.mp3"))

    except Exception as e:
        await aviso.edit(content=f"❌ Hubo un error: {str(e)}")

# Iniciar el bot
bot.run(os.environ.get("DISCORD_TOKEN"))
