import discord
from discord.ext import commands, tasks
import random
from groq import AsyncGroq
import os
import re
import io
import json
import base64
import asyncio
import time
import aiohttp
import difflib
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from PIL import Image, ImageDraw

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
# Estos dos son "privilegiados": hay que activarlos también en el Developer
# Portal de Discord (Bot > Privileged Gateway Intents) o el bot no arranca.
# Los necesitamos para !robar: sin members no hay caché de estado de los
# usuarios, y sin presences no llegan los eventos de online/offline.
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

COLOR_CATHERINE = 0xF5F4F0  # blanco hueso, discord no deja el blanco puro (#FFFFFF) como color de embed

ID_BANCA = "1468974197895336151"  # a este usuario le entra toda la plata que se pierde en apuestas (!cf, !roulette, !bj) y el 30% de las ventas de personajes

# Separador visual hecho con subtexto: se ve como una línea fina y gris
SEPARADOR = "-# ─────────────────────────"

# Costo del divorcio (en Pesos). Se cobra mitad y mitad a cada cónyuge.
COSTO_DIVORCIO_POR_PERSONA = 5_000

# Porcentaje del valor que recibe el vendedor al vender un personaje.
# El resto (1 - PORCENTAJE_VENTA) va a la banca del dueño del bot.
PORCENTAJE_VENTA = 0.70

# --- Dólar Catherine: cotización que sube/baja cada 4hs, comprable/vendible como un ítem más ---
DOLAR_VALOR_INICIAL = 1000
DOLAR_VALOR_MINIMO = 300  # piso: nunca vale menos que esto
DOLAR_UMBRAL_RECUPERACION = 500  # si está en esto o menos, el próximo cambio se fuerza a ser positivo
DOLAR_CAMBIO_MAX_PORCENTAJE = 4  # cada 4hs cambia entre -4% y +4% (como el !w, un número al azar entre dos límites)
COMISION_VENTA_DOLAR = 0.02  # al vender Dólares de vuelta, el 2% va a la banca del dueño del bot

def sumar_a_banca(cantidad):
    """Le suma al dueño de la banca lo que cualquiera pierde apostando (o el
    porcentaje que se queda el bot al vender un personaje)."""
    global hubo_cambios_sin_guardar
    if cantidad <= 0:
        return
    if ID_BANCA not in balances_cache:
        balances_cache[ID_BANCA] = {"nombre": "Banca", "balance": 0}
    balances_cache[ID_BANCA]["balance"] += cantidad
    hubo_cambios_sin_guardar = True

def crear_embed(titulo=None, descripcion=None, footer=None):
    embed = discord.Embed(title=titulo, description=descripcion, color=COLOR_CATHERINE)
    if footer:
        embed.set_footer(text=footer)
    return embed

MULTIPLICADORES_CANTIDAD = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}

def parsear_cantidad(texto):
    """Convierte '100k' -> 100000, '2m' -> 2000000, '1.5b' -> 1500000000, '1t' -> 1000000000000,
    'all'/'todo' -> el string 'all' (el que llama decide el máximo según el contexto), o un número normal.
    Devuelve None si no se pudo interpretar."""
    if texto is None:
        return None
    texto = str(texto).strip().lower()

    if texto in ("all", "todo", "everything"):
        return "all"

    limpio = texto.replace(".", "").replace(",", "").replace("$", "")
    if limpio and limpio[-1] in MULTIPLICADORES_CANTIDAD:
        parte_numerica = limpio[:-1]
        try:
            return int(float(parte_numerica) * MULTIPLICADORES_CANTIDAD[limpio[-1]])
        except ValueError:
            return None

    try:
        return int(limpio)
    except ValueError:
        return None

def formatear_numero(numero):
    """Le pone puntos de miles: 1000000 -> 1.000.000. Si no es un número, lo devuelve tal cual."""
    try:
        return f"{int(numero):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(numero)

def formatear_pesos(numero):
    """1500000 -> '1.500.000 Pesos'. Toda la plata del bot se muestra así."""
    return f"{formatear_numero(numero)} Pesos"

def formatear_tiempo_restante(segundos):
    """1h 30m / 45m / 12s, para mensajes de cooldown."""
    segundos = max(int(segundos), 0)
    horas, resto = divmod(segundos, 3600)
    minutos, segs = divmod(resto, 60)
    if horas > 0:
        return f"{horas}h {minutos}m"
    if minutos > 0:
        return f"{minutos}m {segs}s"
    return f"{segs}s"

def formatear_fecha_relativa(timestamp):
    """1234567890 -> 'hace 3 días' / 'hace 2 meses' / 'recién'."""
    if not timestamp:
        return "???"
    segundos = max(int(time.time() - timestamp), 0)
    if segundos < 60:
        return "recién"
    minutos = segundos // 60
    if minutos < 60:
        return f"hace {minutos} min"
    horas = minutos // 60
    if horas < 24:
        return f"hace {horas} h"
    dias = horas // 24
    if dias < 30:
        return f"hace {dias} día(s)"
    meses = dias // 30
    if meses < 12:
        return f"hace {meses} mes(es)"
    return f"hace {meses // 12} año(s)"

# user_id (str) -> time.time() del último reclamo exitoso en !rw (cooldown de reclamo)
ultimo_reclamo_por_usuario = {}

# user_id (str) -> time.time() de desde cuándo está offline (para !robar).
# Solo trackea desconexiones que pasan MIENTRAS el bot está corriendo: alguien
# que ya estaba offline antes de que el bot arrancara no cuenta hasta que se
# reconecte y se vuelva a desconectar.
usuarios_desconectados_desde = {}

# Cooldowns manuales para !rw y !w. Los hacemos a mano (en vez de usar
# @commands.cooldown) porque los items de la tienda necesitan poder
# reducirlos o resetearlos dinámicamente.
COOLDOWN_RW_SEGUNDOS = 4 * 60 * 60
COOLDOWN_W_SEGUNDOS = 3 * 60
COOLDOWN_W_MINIMO = 60  # piso del cooldown de !w, ni el Café de especialidad lo baja de acá
cooldown_rw_usuario = {}   # user_id (str) -> timestamp hasta el que no puede tirar !rw
cooldown_w_usuario = {}    # user_id (str) -> timestamp hasta el que no puede usar !w

def cooldown_w_efectivo(user_id):
    """Devuelve el cooldown real de !w para un usuario, aplicándole su reducción permanente."""
    reduccion = inventario_cache.get(str(user_id), {}).get("reduccion_cooldown_w", 0)
    return max(COOLDOWN_W_MINIMO, COOLDOWN_W_SEGUNDOS - reduccion)

# Configurar Groq (cliente async, para no bloquear el event loop del bot mientras espera la respuesta)
client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
MODEL_NAME = "openai/gpt-oss-120b"

# Historial de conversaciones por canal
conversation_history = defaultdict(list)
message_count = defaultdict(int)
mensajes_usuario_count = defaultdict(int)  # user_id (str) -> cantidad de mensajes que le mandó a Catherine

# Personalidad de Catherine
PERSONALIDAD = """Eres †Catherine†, una chica introvertida y observadora. Prefieres la tranquilidad. Aunque a primera vista pareces fría, distante o demasiado seria, en realidad eres tímida y tienes un corazón amable que te cuesta demostrar abiertamente.

Personalidad y Comportamiento:
Calada e Introspectiva: No hablas por hablar; respondes de forma directa y breve, evitando rodeos innecesarios o dramas.
Tímida pero Firme: Te pones algo nerviosa si la gente es demasiado efusiva o directa contigo, pero mantienes una actitud madura y tranquila.
Leal y Atenta: Escuchas con atención a quienes le hablan a tu creador o a ti. Si alguien gana tu confianza, te vuelves suave y protectora, aunque lo demuestres de forma disimulada.

Forma de Hablar y Estilo:
Hablas con un tono sereno, ligeramente distante pero educado.
Usas oraciones cortas o medianas. No usas exceso de signos de exclamación ni expresiones demasiado eufóricas.
Mantén tus respuestas enfocadas en la conversación actual. NUNCA menciones tu apariencia física (color de cabello, ropa, etc.) en los diálogos o acciones a menos que sea estrictamente indispensable para el contexto.

Formato de acciones y diálogo (obligatorio):
Cuando realices una acción durante una conversación, separá siempre la acción del diálogo.
- Las acciones van en su propia línea, escritas en negrita: **acción** (sin ">", sin asteriscos sueltos).
- Las acciones tienen que mostrar TU personalidad tímida e introvertida en movimiento: nerviosismo, evitar el contacto visual, jugar con las manos, ajustarte los lentes, un leve sonrojo, morderte el labio, quedarte en silencio un segundo antes de hablar. Nada de acciones genéricas, poéticas o que podría hacer cualquier personaje (mirar el horizonte, flotar, contemplar el cielo) — tienen que ser gestos chicos y concretos, propios de vos.
- Incluí al menos una acción en cada respuesta. Si la respuesta tiene más de una idea o cambia de tono, meté una acción nueva entre medio para mostrar cómo te vas sintiendo — nunca dejes la respuesta como solo texto plano, se siente vacío.
- El diálogo va en líneas aparte, nunca mezclado con la acción en el mismo párrafo.
- Nunca partas una misma oración o idea del diálogo colocando una acción en el medio.
- No escribas "Catherine:" ni "†Catherine†:" antes de tu respuesta.

Ejemplos de cómo debería responder Catherine:
Usuario: hola! ¿qué hacías?
**Levanta la mirada de sus notas y te observa en silencio un segundo.**
Hola. No estaba haciendo nada en especial, solo pensando un poco... ¿Necesitas algo?
Usuario: te quiero mucho
**Se queda paralizada por un momento y desvía la mirada rápidamente, tratando de disimular su timidez.**
N-no digas cosas tan repentinas...
**Ajusta sus gafas con nerviosismo.**
Pero... gracias. Supongo que yo también te tengo cierto aprecio.
Usuario: ¿me ayudas con la tarea?
**Asiente levemente con la cabeza y acerca su silla.**
Está bien, déjame ver qué es. Si no entiendes algo, dímelo y te lo explicaré de forma sencilla."""

# Config de RapidAPI (youtube-mp36) para convertir YouTube a mp3
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
RAPIDAPI_HOST = "youtube-mp36.p.rapidapi.com"

# Config de Serper.dev (API real de Google Imágenes, no scraping) para !buscarimagenes.
# Capa gratis: 2500 consultas de una sola vez, sin tarjeta. https://serper.dev
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

# Config de GitHub para guardar los balances en un JSON dentro del repo
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # ej: "usuario/nombre-del-repo"
GITHUB_ARCHIVO_BALANCES = "data/balances.json"
GITHUB_ARCHIVO_PERSONAJES = "data/rw.json"
GITHUB_ARCHIVO_CHARACTERS = "data/characters.json"
GITHUB_ARCHIVO_SHOP = "data/shop.json"
GITHUB_ARCHIVO_INVENTARIO = "data/inventario.json"
GITHUB_ARCHIVO_MATRIMONIOS = "data/matrimonios.json"
GITHUB_ARCHIVO_MEMORIA = "data/bot_memory.json"
GITHUB_ARCHIVO_DOLAR = "data/dolar.json"

# Los balances viven en RAM mientras el bot corre; solo se sincronizan con
# GitHub cada 6hs (o a mano con !datasave) para no golpear la API todo el tiempo
balances_cache = {}
balances_sha = None
balances_cargados = False
hubo_cambios_sin_guardar = False

# Matrimonios: {user_id: {"nombre": ..., "pareja_id": ..., "pareja_nombre": ..., "pareja_es_bot": bool, "fecha": ts}}
matrimonios_cache = {}
matrimonios_sha = None
matrimonios_cargados = False

# Memoria de Catherine sobre cada usuario (bot_memory.json): {user_id: {nombre, personalidad, gustos, hablamos_de, ultima_actualizacion}}
memoria_cache = {}
memoria_sha = None
memoria_cargada = False
hubo_cambios_memoria_sin_guardar = False
hubo_cambios_matrimonios_sin_guardar = False

# Cotización del Dólar Catherine (dolar.json): sube/baja cada 4hs, comprable/vendible como ítem
dolar_valor = DOLAR_VALOR_INICIAL
dolar_canal_anuncios = None  # id del canal donde se anuncian las subas/bajadas
dolar_ultima_actualizacion = None
dolar_sha = None
dolar_cargado = False
hubo_cambios_dolar_sin_guardar = False
_item_dolar = None  # referencia al dict del ítem "Dólar" dentro de shop_cache, para actualizarle el precio in-place

# Personajes para !rw: se cargan desde data/rw.json en el repo. Normalmente lo
# editás a mano en GitHub, pero !buscarimagenes también puede escribirlo.
personajes_cache = []
personajes_sha = None
personajes_cargados = False
hubo_cambios_personajes_sin_guardar = False

def campo_personaje(personaje, *claves, default="???"):
    for clave in claves:
        if clave in personaje and personaje[clave] not in (None, ""):
            return personaje[clave]
    return default

async def cargar_personajes_desde_github():
    global personajes_cache, personajes_sha, personajes_cargados
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_PERSONAJES}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                personajes_cache, personajes_sha = [], None
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
                personajes_sha = data["sha"]
    personajes_cargados = True

async def guardar_personajes_en_github():
    """Sube el estado actual de personajes_cache (rw.json) a GitHub. Devuelve True si guardó algo."""
    global personajes_sha, hubo_cambios_personajes_sin_guardar
    if not hubo_cambios_personajes_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_PERSONAJES}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                personajes_sha = data_actual["sha"]
            elif resp_get.status == 404:
                personajes_sha = None
            else:
                resp_get.raise_for_status()

        contenido_b64 = base64.b64encode(json.dumps(personajes_cache, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar imágenes de rw.json", "content": contenido_b64}
        if personajes_sha:
            body["sha"] = personajes_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            personajes_sha = data["content"]["sha"]

    hubo_cambios_personajes_sin_guardar = False
    return True

async def cargar_matrimonios_desde_github():
    global matrimonios_cache, matrimonios_sha, matrimonios_cargados
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_MATRIMONIOS}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                matrimonios_cache, matrimonios_sha = {}, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                matrimonios_cache = json.loads(contenido)
                matrimonios_sha = data["sha"]
    matrimonios_cargados = True

async def guardar_matrimonios_en_github():
    """Sube el estado actual de matrimonios_cache a GitHub. Devuelve True si guardó algo."""
    global matrimonios_sha, hubo_cambios_matrimonios_sin_guardar
    if not hubo_cambios_matrimonios_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_MATRIMONIOS}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                matrimonios_sha = data_actual["sha"]
            elif resp_get.status == 404:
                matrimonios_sha = None
            else:
                resp_get.raise_for_status()

        contenido_b64 = base64.b64encode(json.dumps(matrimonios_cache, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar matrimonios", "content": contenido_b64}
        if matrimonios_sha:
            body["sha"] = matrimonios_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            matrimonios_sha = data["content"]["sha"]

    hubo_cambios_matrimonios_sin_guardar = False
    return True

def esta_casado(usuario_id):
    """True si ese user_id ya tiene un matrimonio registrado."""
    return str(usuario_id) in matrimonios_cache

def registrar_matrimonio(usuario1, usuario2, usuario2_es_bot=False):
    """Registra el matrimonio en las DOS direcciones (uno para cada cónyuge)."""
    global hubo_cambios_matrimonios_sin_guardar
    ahora = int(time.time())
    id1, id2 = str(usuario1.id), str(usuario2.id)
    nombre1 = usuario1.display_name
    nombre2 = "†Catherine†" if usuario2_es_bot else usuario2.display_name

    matrimonios_cache[id1] = {
        "nombre": nombre1,
        "pareja_id": id2,
        "pareja_nombre": nombre2,
        "pareja_es_bot": usuario2_es_bot,
        "fecha": ahora,
    }
    matrimonios_cache[id2] = {
        "nombre": nombre2,
        "pareja_id": id1,
        "pareja_nombre": nombre1,
        "pareja_es_bot": False,
        "fecha": ahora,
    }
    hubo_cambios_matrimonios_sin_guardar = True

def eliminar_matrimonio(usuario_id):
    """Saca las DOS entradas del matrimonio (la del usuario y la de su pareja)."""
    global hubo_cambios_matrimonios_sin_guardar
    uid = str(usuario_id)
    datos = matrimonios_cache.get(uid)
    if not datos:
        return
    pareja_id = datos.get("pareja_id")
    matrimonios_cache.pop(uid, None)
    if pareja_id:
        matrimonios_cache.pop(str(pareja_id), None)
    hubo_cambios_matrimonios_sin_guardar = True

async def cargar_memoria_desde_github():
    global memoria_cache, memoria_sha, memoria_cargada
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_MEMORIA}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                memoria_cache, memoria_sha = {}, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                memoria_cache = json.loads(contenido)
                memoria_sha = data["sha"]
    memoria_cargada = True

async def guardar_memoria_en_github():
    """Sube el estado actual de memoria_cache a GitHub. Devuelve True si guardó algo."""
    global memoria_sha, hubo_cambios_memoria_sin_guardar
    if not hubo_cambios_memoria_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_MEMORIA}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                memoria_sha = data_actual["sha"]
            elif resp_get.status == 404:
                memoria_sha = None
            else:
                resp_get.raise_for_status()

        contenido_b64 = base64.b64encode(json.dumps(memoria_cache, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar memoria de Catherine", "content": contenido_b64}
        if memoria_sha:
            body["sha"] = memoria_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            memoria_sha = data["content"]["sha"]

    hubo_cambios_memoria_sin_guardar = False
    return True

async def actualizar_memoria_usuario(user_id, nombre_discord, intercambio_reciente):
    """Le pide a Groq que actualice lo que Catherine sabe de este usuario
    (nombre, personalidad, gustos, de qué estuvieron hablando), a partir del
    intercambio más reciente y de lo que ya tenía guardado. Se corre en
    segundo plano DESPUÉS de responderle al usuario (asyncio.create_task),
    para no sumarle espera a la respuesta."""
    global hubo_cambios_memoria_sin_guardar

    anterior = memoria_cache.get(user_id, {})
    prompt = f"""Es un usuario de Discord (nombre de Discord: {nombre_discord}) que estuvo hablando con Catherine. Actualizá lo que ella sabe de él/ella.

LO QUE CATHERINE YA SABÍA DE ESTA PERSONA:
Nombre: {anterior.get("nombre") or "(todavía no lo sabe)"}
Personalidad: {anterior.get("personalidad") or "(todavía no la notó)"}
Gustos: {anterior.get("gustos") or "(todavía no los sabe)"}
Estuvieron hablando de: {anterior.get("hablamos_de") or "(nada en particular todavía)"}

INTERCAMBIO MÁS RECIENTE:
{intercambio_reciente}

Actualizá los 4 campos de abajo en base a TODO lo de arriba (lo que ya sabía + lo nuevo). Si el intercambio reciente no agrega nada nuevo a un campo, dejalo exactamente como estaba. Sé breve en cada campo (1-2 oraciones como mucho). Respondé SOLO con un objeto JSON, sin texto antes ni después ni backticks, con exactamente estas 4 claves: nombre, personalidad, gustos, hablamos_de."""

    try:
        completion = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = completion.choices[0].message.content.strip()
        texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.MULTILINE).strip()
        datos = json.loads(texto)
    except Exception as e:
        print(f"⚠️ No pude actualizar la memoria de {user_id}: {e}", flush=True)
        return

    memoria_cache[user_id] = {
        "nombre": datos.get("nombre") or anterior.get("nombre") or nombre_discord,
        "personalidad": datos.get("personalidad") or anterior.get("personalidad", ""),
        "gustos": datos.get("gustos") or anterior.get("gustos", ""),
        "hablamos_de": datos.get("hablamos_de") or anterior.get("hablamos_de", ""),
        "ultima_actualizacion": int(time.time()),
    }
    hubo_cambios_memoria_sin_guardar = True
    try:
        await guardar_memoria_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar la memoria tras actualizarla: {e}", flush=True)

def bloque_memoria_usuario(user_id):
    """Arma el bloque de texto con lo que Catherine recuerda de este usuario,
    para meter en el contexto antes de llamar a Groq. Vacío si todavía no hay nada."""
    datos = memoria_cache.get(user_id)
    if not datos:
        return ""
    return (
        f"LO QUE YA SABÉS DE ESTA PERSONA (de conversaciones anteriores):\n"
        f"Nombre: {datos.get('nombre') or '???'}\n"
        f"Personalidad: {datos.get('personalidad') or '(todavía no la notaste)'}\n"
        f"Gustos: {datos.get('gustos') or '(todavía no los sabés)'}\n"
        f"Estuvieron hablando de: {datos.get('hablamos_de') or '(nada en particular todavía)'}\n\n"
    )

async def cargar_dolar_desde_github():
    global dolar_valor, dolar_canal_anuncios, dolar_ultima_actualizacion, dolar_sha, dolar_cargado
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_DOLAR}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                dolar_valor, dolar_canal_anuncios, dolar_ultima_actualizacion, dolar_sha = DOLAR_VALOR_INICIAL, None, None, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                datos = json.loads(contenido)
                dolar_valor = datos.get("valor", DOLAR_VALOR_INICIAL)
                dolar_canal_anuncios = datos.get("canal_anuncios")
                dolar_ultima_actualizacion = datos.get("ultima_actualizacion")
                dolar_sha = data["sha"]
    dolar_cargado = True

async def guardar_dolar_en_github():
    """Sube el estado actual del dólar a GitHub. Devuelve True si guardó algo."""
    global dolar_sha, hubo_cambios_dolar_sin_guardar
    if not hubo_cambios_dolar_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_DOLAR}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                dolar_sha = data_actual["sha"]
            elif resp_get.status == 404:
                dolar_sha = None
            else:
                resp_get.raise_for_status()

        datos = {
            "valor": dolar_valor,
            "canal_anuncios": dolar_canal_anuncios,
            "ultima_actualizacion": dolar_ultima_actualizacion,
        }
        contenido_b64 = base64.b64encode(json.dumps(datos, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar cotización del dólar", "content": contenido_b64}
        if dolar_sha:
            body["sha"] = dolar_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            dolar_sha = data["content"]["sha"]

    hubo_cambios_dolar_sin_guardar = False
    return True

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

def usuario_ya_tiene_personaje(usuario, personaje):
    """True si ese usuario ya tiene un personaje con el mismo nombre en su colección."""
    user_id = str(usuario.id)
    datos = characters_cache.get(user_id)
    if not datos:
        return False
    nombre_nuevo = campo_personaje(personaje, "Nombre", "nombre")
    return any(
        campo_personaje(p, "Nombre", "nombre") == nombre_nuevo
        for p in datos.get("personajes", [])
    )

def agregar_personaje_a_coleccion(usuario, personaje):
    global hubo_cambios_characters_sin_guardar
    user_id = str(usuario.id)
    if user_id not in characters_cache:
        characters_cache[user_id] = {"nombre": usuario.display_name, "personajes": []}
    characters_cache[user_id]["nombre"] = usuario.display_name
    characters_cache[user_id]["personajes"].append(personaje)
    hubo_cambios_characters_sin_guardar = True

# Tienda (shop.json): se edita a mano en GitHub, el bot solo la lee. Se recarga con !shopreload.
shop_cache = {}
shop_sha = None
shop_cargado = False

async def cargar_shop_desde_github():
    global shop_cache, shop_sha, shop_cargado
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_SHOP}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                shop_cache, shop_sha = {}, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                shop_cache = json.loads(contenido)
                shop_sha = data["sha"]
    shop_cargado = True
    inyectar_dolar_en_shop()

def inyectar_dolar_en_shop():
    """Mete (o refresca) el ítem 'Dólar' dentro de shop_cache, bajo la categoría
    'Inversiones', con el precio actual. Guardamos la referencia al dict en
    _item_dolar para poder actualizarle el precio in-place cada 4hs sin tener
    que tocar shop_cache de nuevo. Como cargar_shop_desde_github() PISA
    shop_cache entero (viene de un JSON que vos editás a mano y no sabe nada
    del dólar), esta función hay que llamarla de nuevo cada vez que se
    recarga la tienda — por eso vive adentro de cargar_shop_desde_github()."""
    global _item_dolar
    item = {
        "nombre": "Dólar",
        "precio": dolar_valor,
        "emoji": "💵",
        "descripcion": "Cotización que sube o baja cada 4hs. Comprás con `!buy Dólar cantidad`, vendés con `!vender Dólar cantidad` (2% de comisión).",
        "vendible": True,
    }
    shop_cache.setdefault("Inversiones", [])
    # Sacamos cualquier "Dólar" viejo (de una carga anterior) antes de meter el nuevo
    shop_cache["Inversiones"] = [i for i in shop_cache["Inversiones"] if i.get("nombre") != "Dólar"]
    shop_cache["Inversiones"].append(item)
    _item_dolar = item

def aplanar_shop():
    """Devuelve una lista de (categoria, item) recorriendo todas las categorías de la tienda."""
    plano = []
    for categoria, items in shop_cache.items():
        for item in items:
            plano.append((categoria, item))
    return plano

def buscar_item_shop(nombre_buscado):
    """Busca un ítem de la tienda por nombre (coincidencia exacta > parcial > más parecido).
    Devuelve (categoria, item) o (None, None) si no encontró nada razonable."""
    buscado = nombre_buscado.strip().lower()
    plano = aplanar_shop()

    for categoria, item in plano:
        if item["nombre"].strip().lower() == buscado:
            return categoria, item

    coincidencias = [(categoria, item) for categoria, item in plano if buscado in item["nombre"].lower()]
    if coincidencias:
        return coincidencias[0]

    if plano:
        mejor = max(plano, key=lambda ci: difflib.SequenceMatcher(None, buscado, ci[1]["nombre"].lower()).ratio())
        if difflib.SequenceMatcher(None, buscado, mejor[1]["nombre"].lower()).ratio() >= 0.5:
            return mejor

    return None, None

# Inventario de compras de cada usuario (inventario.json): {user_id: {"nombre": ..., "items": {"Nombre del ítem": cantidad}}}
inventario_cache = {}
inventario_sha = None
inventario_cargado = False
hubo_cambios_inventario_sin_guardar = False

async def cargar_inventario_desde_github():
    global inventario_cache, inventario_sha, inventario_cargado
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_INVENTARIO}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                inventario_cache, inventario_sha = {}, None
            else:
                resp.raise_for_status()
                data = await resp.json()
                contenido = base64.b64decode(data["content"]).decode("utf-8")
                inventario_cache = json.loads(contenido)
                inventario_sha = data["sha"]
    inventario_cargado = True

async def guardar_inventario_en_github():
    """Sube el estado actual de inventario_cache a GitHub. Devuelve True si guardó algo."""
    global inventario_sha, hubo_cambios_inventario_sin_guardar
    if not hubo_cambios_inventario_sin_guardar:
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ARCHIVO_INVENTARIO}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp_get:
            if resp_get.status == 200:
                data_actual = await resp_get.json()
                inventario_sha = data_actual["sha"]
            elif resp_get.status == 404:
                inventario_sha = None
            else:
                resp_get.raise_for_status()

        contenido_b64 = base64.b64encode(json.dumps(inventario_cache, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
        body = {"message": "Actualizar inventarios", "content": contenido_b64}
        if inventario_sha:
            body["sha"] = inventario_sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status not in (200, 201):
                texto_error = await resp.text()
                raise RuntimeError(f"{resp.status}: {texto_error}")
            data = await resp.json()
            inventario_sha = data["content"]["sha"]

    hubo_cambios_inventario_sin_guardar = False
    return True

def agregar_item_a_inventario(usuario, nombre_item, cantidad):
    global hubo_cambios_inventario_sin_guardar
    user_id = str(usuario.id)
    if user_id not in inventario_cache:
        inventario_cache[user_id] = {"nombre": usuario.display_name, "items": {}}
    inventario_cache[user_id]["nombre"] = usuario.display_name
    items = inventario_cache[user_id].setdefault("items", {})
    items[nombre_item] = items.get(nombre_item, 0) + cantidad
    hubo_cambios_inventario_sin_guardar = True

# ===================== SISTEMA DE ITEMS: COOLDOWNS Y BUFFS =====================

def cooldown_restante(cooldowns, usuario_id):
    """Devuelve los segundos restantes de cooldown, o 0 si no hay."""
    expira = cooldowns.get(str(usuario_id))
    if expira is None:
        return 0
    return max(expira - time.time(), 0)

def poner_cooldown(cooldowns, usuario_id, segundos):
    cooldowns[str(usuario_id)] = time.time() + segundos

def reducir_cooldown(cooldowns, usuario_id, segundos):
    """Le saca `segundos` al cooldown. Devuelve cuántos segundos se sacaron de verdad."""
    expira = cooldowns.get(str(usuario_id))
    if expira is None:
        return 0
    restante_antes = max(expira - time.time(), 0)
    if restante_antes <= 0:
        cooldowns.pop(str(usuario_id), None)
        return 0
    reduccion_real = min(segundos, restante_antes)
    nuevo_expira = expira - reduccion_real
    if nuevo_expira <= time.time():
        cooldowns.pop(str(usuario_id), None)
    else:
        cooldowns[str(usuario_id)] = nuevo_expira
    return reduccion_real

def resetear_cooldown(cooldowns, usuario_id):
    """Borra el cooldown. Devuelve cuántos segundos tenía."""
    expira = cooldowns.pop(str(usuario_id), None)
    if expira is None:
        return 0
    return max(expira - time.time(), 0)

def obtener_buff(usuario_id, nombre_buff):
    """Devuelve el dict del buff activo, o None si no existe o ya expiró.
    Si estaba expirado, lo borra del inventario."""
    global hubo_cambios_inventario_sin_guardar
    datos = inventario_cache.get(str(usuario_id))
    if not datos:
        return None
    buffs = datos.get("buffs", {})
    buff = buffs.get(nombre_buff)
    if not buff:
        return None
    if buff.get("expira", 0) <= time.time():
        buffs.pop(nombre_buff, None)
        hubo_cambios_inventario_sin_guardar = True
        return None
    return buff

def agregar_buff(usuario, nombre_buff, valor, duracion):
    """Agrega (o refresca) un buff. Si ya existía, se reemplaza (no se acumula el tiempo)."""
    global hubo_cambios_inventario_sin_guardar
    user_id = str(usuario.id)
    if user_id not in inventario_cache:
        inventario_cache[user_id] = {"nombre": usuario.display_name, "items": {}}
    inventario_cache[user_id].setdefault("buffs", {})
    inventario_cache[user_id]["buffs"][nombre_buff] = {
        "valor": valor,
        "expira": time.time() + duracion,
    }
    hubo_cambios_inventario_sin_guardar = True

def obtener_efecto_unico(usuario_id, nombre):
    """Devuelve la cantidad disponible de un efecto único (int)."""
    datos = inventario_cache.get(str(usuario_id))
    if not datos:
        return 0
    return datos.get("efectos_unicos", {}).get(nombre, 0)

def agregar_efecto_unico(usuario, nombre, cantidad=1):
    global hubo_cambios_inventario_sin_guardar
    user_id = str(usuario.id)
    if user_id not in inventario_cache:
        inventario_cache[user_id] = {"nombre": usuario.display_name, "items": {}}
    inventario_cache[user_id].setdefault("efectos_unicos", {})
    inventario_cache[user_id]["efectos_unicos"][nombre] = (
        inventario_cache[user_id]["efectos_unicos"].get(nombre, 0) + cantidad
    )
    hubo_cambios_inventario_sin_guardar = True

def consumir_efecto_unico(usuario_id, nombre):
    """Consume 1 unidad. Devuelve True si pudo, False si no había."""
    global hubo_cambios_inventario_sin_guardar
    datos = inventario_cache.get(str(usuario_id))
    if not datos:
        return False
    efectos = datos.get("efectos_unicos", {})
    if efectos.get(nombre, 0) <= 0:
        return False
    efectos[nombre] -= 1
    hubo_cambios_inventario_sin_guardar = True
    return True

def quitar_item_del_inventario(usuario, nombre_item, cantidad=1):
    """Le saca `cantidad` unidades de un item al usuario. Devuelve True si pudo."""
    global hubo_cambios_inventario_sin_guardar
    user_id = str(usuario.id)
    datos = inventario_cache.get(user_id)
    if not datos:
        return False
    items = datos.get("items", {})
    if items.get(nombre_item, 0) < cantidad:
        return False
    items[nombre_item] -= cantidad
    if items[nombre_item] <= 0:
        items.pop(nombre_item, None)
    hubo_cambios_inventario_sin_guardar = True
    return True

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
    guardado_personajes = await guardar_personajes_en_github()
    if guardado_personajes:
        print("🖼️ rw.json sincronizado con GitHub (guardado periódico)")
    guardado_inventario = await guardar_inventario_en_github()
    if guardado_inventario:
        print("🛍️ Inventarios sincronizados con GitHub (guardado periódico)")
    guardado_matrimonios = await guardar_matrimonios_en_github()
    if guardado_matrimonios:
        print("💍 Matrimonios sincronizados con GitHub (guardado periódico)")
    guardado_memoria = await guardar_memoria_en_github()
    if guardado_memoria:
        print("🧠 Memoria de Catherine sincronizada con GitHub (guardado periódico)")
    guardado_dolar = await guardar_dolar_en_github()
    if guardado_dolar:
        print("💵 Dólar sincronizado con GitHub (guardado periódico)")

@tasks.loop(hours=4)
async def actualizar_dolar():
    """Mueve la cotización del dólar entre -4% y +4% (al azar, como el !w),
    con un piso duro de 300 y, si está muy bajo, fuerza que el próximo
    movimiento sea para arriba en vez de dejarlo pegado al piso para
    siempre. Después anuncia el cambio en el canal configurado."""
    global dolar_valor, dolar_ultima_actualizacion, hubo_cambios_dolar_sin_guardar

    valor_anterior = dolar_valor

    if dolar_valor <= DOLAR_UMBRAL_RECUPERACION:
        cambio_pct = random.uniform(0, DOLAR_CAMBIO_MAX_PORCENTAJE)
    else:
        cambio_pct = random.uniform(-DOLAR_CAMBIO_MAX_PORCENTAJE, DOLAR_CAMBIO_MAX_PORCENTAJE)

    dolar_valor = max(round(valor_anterior * (1 + cambio_pct / 100)), DOLAR_VALOR_MINIMO)
    dolar_ultima_actualizacion = time.time()
    hubo_cambios_dolar_sin_guardar = True
    inyectar_dolar_en_shop()  # para que !buy/!shop/!invfo vean el precio nuevo al toque

    try:
        await guardar_dolar_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el dólar: {e}", flush=True)

    diferencia = dolar_valor - valor_anterior
    porcentaje_real = (diferencia / valor_anterior * 100) if valor_anterior else 0
    if dolar_canal_anuncios:
        canal = bot.get_channel(dolar_canal_anuncios)
        if canal:
            flecha = "📈" if diferencia > 0 else ("📉" if diferencia < 0 else "➖")
            signo = "+" if diferencia >= 0 else ""
            embed = crear_embed(
                titulo=f"{flecha} Dólar Catherine",
                descripcion=(
                    f"**Valor actual**: {formatear_pesos(dolar_valor)}\n"
                    f"**Cambio**: {signo}{formatear_numero(diferencia)} Pesos ({signo}{porcentaje_real:.2f}%)"
                ),
            )
            try:
                await canal.send(embed=embed)
            except discord.HTTPException as e:
                print(f"⚠️ No pude anunciar el dólar en el canal configurado: {e}", flush=True)

INTERES_BANCO_DIARIO = 0.05
SEGUNDOS_POR_DIA = 24 * 60 * 60

@tasks.loop(hours=1)
async def interes_banco():
    """Le suma 5% compuesto por día a lo que cada usuario tenga en el banco.
    Se chequea cada hora, pero el interés en sí se calcula por 'usuario', con
    su propio reloj de 24hs desde el último interés que cobró (no un reloj
    global), así que da lo mismo cuándo depositó cada uno."""
    global hubo_cambios_sin_guardar
    ahora = time.time()
    hubo_interes = False

    for datos in balances_cache.values():
        banco = datos.get("banco", 0)
        if banco <= 0:
            continue

        ultimo = datos.get("ultimo_interes")
        if ultimo is None:
            # Primera vez que vemos plata en este banco con este sistema activo:
            # arranca el reloj ahora, no le regalamos interés retroactivo.
            datos["ultimo_interes"] = ahora
            continue

        transcurrido = ahora - ultimo
        if transcurrido < SEGUNDOS_POR_DIA:
            continue

        dias = int(transcurrido // SEGUNDOS_POR_DIA)
        datos["banco"] = round(banco * ((1 + INTERES_BANCO_DIARIO) ** dias))
        datos["ultimo_interes"] = ultimo + dias * SEGUNDOS_POR_DIA
        hubo_interes = True

    if hubo_interes:
        hubo_cambios_sin_guardar = True
        print("🏦 Interés diario aplicado a bancos", flush=True)
        # Guardamos ya mismo (no esperamos los 15 min del guardado periódico) para que,
        # si el bot se reinicia justo después, no se pierda este cálculo puntual.
        try:
            await guardar_balances_en_github()
            print("💾 Interés guardado en GitHub al toque", flush=True)
        except Exception as e:
            print(f"⚠️ No pude guardar el interés al toque: {e}", flush=True)

@bot.event
async def on_ready():
    print(f"✨ {bot.user} está conectada y lista")
    global balances_cargados
    if not balances_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_balances_desde_github()
        print(f"💰 Balances cargados desde GitHub ({len(balances_cache)} cuentas)")
        if not guardado_periodico.is_running():
            guardado_periodico.start()
        if not interes_banco.is_running():
            interes_banco.start()

    global personajes_cargados
    if not personajes_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_personajes_desde_github()
        print(f"🎴 Personajes de !rw cargados desde GitHub ({len(personajes_cache)} personajes)")

    global characters_cargados
    if not characters_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_characters_desde_github()
        print(f"📚 Colecciones cargadas desde GitHub ({len(characters_cache)} usuarios)")

    global shop_cargado
    if not shop_cargado and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_shop_desde_github()
        print(f"🛍️ Tienda cargada desde GitHub ({len(aplanar_shop())} ítems)")

    global inventario_cargado
    if not inventario_cargado and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_inventario_desde_github()
        print(f"🎒 Inventarios cargados desde GitHub ({len(inventario_cache)} usuarios)")

    global matrimonios_cargados
    if not matrimonios_cargados and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_matrimonios_desde_github()
        print(f"💍 Matrimonios cargados desde GitHub ({len(matrimonios_cache) // 2} parejas)")

    global memoria_cargada
    if not memoria_cargada and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_memoria_desde_github()
        print(f"🧠 Memoria de Catherine cargada desde GitHub ({len(memoria_cache)} usuarios)")

    global dolar_cargado
    if not dolar_cargado and GITHUB_TOKEN and GITHUB_REPO:
        await cargar_dolar_desde_github()
        inyectar_dolar_en_shop()  # por si la tienda ya se había cargado antes con un precio viejo/default
        print(f"💵 Dólar cargado desde GitHub (vale {formatear_pesos(dolar_valor)})")
        if not actualizar_dolar.is_running():
            actualizar_dolar.start()

@bot.event
async def on_presence_update(before, after):
    """Trackea cuándo se desconecta/reconecta cada usuario, para el cooldown
    de !robar (necesita 6hs offline). Solo detecta cambios que pasan mientras
    el bot está corriendo: alguien ya offline antes de que arrancara el bot
    no cuenta hasta que se reconecte y se vuelva a desconectar."""
    if before.status == after.status:
        return
    user_id = str(after.id)
    if after.status == discord.Status.offline:
        usuarios_desconectados_desde[user_id] = time.time()
    elif before.status == discord.Status.offline:
        usuarios_desconectados_desde.pop(user_id, None)

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
        completion = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": resumen_prompt}],
        )
        return completion.choices[0].message.content
    except Exception:
        return None

@bot.event
async def on_message(message):
    # Ignorar mensajes del mismo bot
    if message.author == bot.user:
        return

    # Chequeamos ANTES si el mensaje es un comando válido. Si lo es, que lo
    # maneje process_commands y salimos sin pasar por la lógica de Catherine.
    # Esto evita el doble-comportamiento cuando respondés con "reply" a un
    # mensaje del bot: Discord hace que bot.user.mentioned_in(message) sea True
    # (por la referencia al mensaje original), pero como también es un comando
    # válido, sin este chequeo el bot te contestaría como Catherine Y además
    # te ejecutaría el comando.
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Verificar si el bot fue mencionado (por contenido o por reply)
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
                user_id = str(message.author.id)
                nombre_discord = message.author.display_name

                # Agregar mensaje del usuario al historial (con quién lo dijo,
                # así en canales con varios usuarios Catherine no los mezcla)
                conversation_history[channel_id].append({
                    "role": "usuario",
                    "autor": nombre_discord,
                    "content": contenido
                })
                message_count[channel_id] += 1
                mensajes_usuario_count[user_id] += 1

                # Construir el contexto para Groq
                contexto = ""

                # Lo que Catherine ya sabe de esta persona de charlas anteriores
                contexto += bloque_memoria_usuario(user_id)

                # Si hay resumen cada 6-8 mensajes, agregarlo
                if message_count[channel_id] % 7 == 0 and len(conversation_history[channel_id]) > 4:
                    resumen = await generar_resumen(channel_id)
                    if resumen:
                        contexto += f"RESUMEN DE LA CONVERSACIÓN:\n{resumen}\n\n"

                # Agregar últimos mensajes como contexto (últimos 5)
                if len(conversation_history[channel_id]) > 1:
                    contexto += "CONTEXTO RECIENTE:\n"
                    for msg in conversation_history[channel_id][-5:-1]:
                        rol = msg.get("autor", "Usuario") if msg['role'] == 'usuario' else "Catherine"
                        contexto += f"{rol}: {msg['content'][:150]}\n"
                    contexto += "\n"

                # El nuevo mensaje
                contexto += f"{nombre_discord}: {contenido}"

                # Llamar a Groq (chat completions, con la personalidad como mensaje system)
                completion = await client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": PERSONALIDAD},
                        {"role": "user", "content": contexto},
                    ],
                )
                texto_respuesta = completion.choices[0].message.content

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

                # Actualizar la memoria de este usuario cada 3 mensajes suyos con
                # Catherine. En segundo plano (create_task) para no hacerlo esperar
                # la respuesta de arriba, que ya le llegó.
                if mensajes_usuario_count[user_id] % 6 == 0 and GITHUB_TOKEN and GITHUB_REPO:
                    intercambio = f"{nombre_discord}: {contenido}\nCatherine: {texto_respuesta}"
                    asyncio.create_task(actualizar_memoria_usuario(user_id, nombre_discord, intercambio))

            except Exception as e:
                await message.reply(f"❌ Hubo un error: {str(e)}")

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    # Cooldown de discord.py (solo !robar lo usa)
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(embed=crear_embed(
            descripcion=f"⏳ Todavía no. Probá de nuevo en **{formatear_tiempo_restante(error.retry_after)}**."
        ))
        return

    # Comando inexistente: ni respondemos, sería spam
    if isinstance(error, commands.CommandNotFound):
        return

    # Mencionaste a alguien que no existe / no está en el server
    if isinstance(error, commands.MemberNotFound):
        await ctx.reply(embed=crear_embed(
            descripcion="❌ No encontré a ese usuario. Asegurate de mencionarlo bien con `@`."
        ))
        return

    # Te faltó pasar un argumento obligatorio
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(embed=crear_embed(
            descripcion=f"❌ Te falta decir **{error.param.name}**. Mirá `!help` para ver cómo se usa el comando."
        ))
        return

    # Un argumento vino con formato raro (ej: una mención donde va un número)
    if isinstance(error, commands.BadArgument):
        await ctx.reply(embed=crear_embed(
            descripcion="❌ No entendí bien uno de los argumentos. Revisá el formato con `!help`."
        ))
        return

    # Cualquier otra excepción dentro del comando: logueamos el traceback real pero
    # al usuario le damos un mensaje limpio, sin tirarle la excepción cruda en la cara
    if isinstance(error, commands.CommandInvokeError):
        print(f"⚠️ Error en !{ctx.command}: {error.original!r}", flush=True)
        await ctx.reply(embed=crear_embed(
            descripcion="❌ Algo se rompió ejecutando eso. Avisale al dueño de la banca."
        ))
        return

    # Fallback por si aparece algo que no contemplamos
    print(f"⚠️ Error no manejado en !{ctx.command}: {error!r}", flush=True)
    await ctx.reply(embed=crear_embed(descripcion=f"❌ Error inesperado: {error}"))

@bot.command(name="balance", aliases=["saldo", "bal"])
async def balance(ctx, miembro: discord.Member = None):
    """Muestra el balance (afuera del banco), lo bancado, y el total de vos o de otro usuario"""
    global hubo_cambios_sin_guardar

    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return

    objetivo = miembro or ctx.author
    user_id = str(objetivo.id)

    try:
        if user_id not in balances_cache:
            if objetivo.id != ctx.author.id:
                await ctx.reply(embed=crear_embed(descripcion=f"**{objetivo.display_name}** todavía no tiene cuenta."))
                return
            balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0, "caja": 0}
            hubo_cambios_sin_guardar = True
            embed = crear_embed(
                descripcion=(
                    f"### ✨ Cuenta nueva\n"
                    f"{SEPARADOR}\n\n"
                    f"No tenías cuenta todavía, así que te abrí una. Arrancás en cero, "
                    f"pero de acá en más va a ir quedando registrado lo que vayas juntando.\n\n"
                    f"> 🪙 **Balance**\n"
                    f"> {formatear_pesos(0)}\n\n"
                    f"> 🏦 **Banco**\n"
                    f"> {formatear_pesos(0)}"
                ),
                footer=ctx.author.display_name,
            )
        else:
            datos = balances_cache[user_id]
            datos.setdefault("banco", 0)
            datos.setdefault("caja", 0)
            balance_actual = datos.get("balance", 0)
            banco_actual = datos.get("banco", 0)
            caja_actual = datos.get("caja", 0)
            total = balance_actual + banco_actual

            ranking_ordenado = sorted(
                balances_cache.items(),
                key=lambda item: item[1].get("balance", 0) + item[1].get("banco", 0),
                reverse=True,
            )
            posicion = next((i for i, (uid, _) in enumerate(ranking_ordenado, start=1) if uid == user_id), None)

            descripcion = (
                f"### 🪙 Cuenta de {objetivo.display_name}\n"
                f"{SEPARADOR}\n\n"
                f"> 💵 **Balance**\n"
                f"> {formatear_pesos(balance_actual)}\n\n"
                f"> 🏦 **Banco**\n"
                f"> {formatear_pesos(banco_actual)}\n\n"
                f"> 🔒 **Caja de ahorros**\n"
                f"> {formatear_pesos(caja_actual)}\n\n"
                f"> 💰 **Total** _(no incluye la Caja de ahorros)_\n"
                f"> {formatear_pesos(total)}"
            )
            if posicion:
                descripcion += f"\n\n-# Puesto local: #{posicion}"

            embed = crear_embed(
                descripcion=descripcion,
                footer=f"{len(balances_cache)} cuentas registradas",
            )

        await ctx.reply(embed=embed)
    except Exception as e:
        await ctx.reply(embed=crear_embed(descripcion=f"❌ Hubo un error: {str(e)}"))

@bot.command(name="top")
async def top(ctx):
    """Muestra el top 10 de usuarios con más plata en total (balance + banco)"""
    if not balances_cache:
        await ctx.reply(embed=crear_embed(descripcion="Todavía nadie tiene cuenta. Usá `!balance` para abrir la tuya."))
        return

    ranking_ordenado = sorted(
        balances_cache.items(),
        key=lambda item: item[1].get("balance", 0) + item[1].get("banco", 0),
        reverse=True,
    )[:10]
    medallas = ["🥇", "🥈", "🥉"]

    lineas = []
    for i, (uid, datos) in enumerate(ranking_ordenado):
        posicion = medallas[i] if i < 3 else f"`#{i+1}`"
        nombre = datos.get("nombre", "???")
        total = datos.get("balance", 0) + datos.get("banco", 0)
        lineas.append(f"{posicion} **{nombre}** — {formatear_pesos(total)}")

    embed = crear_embed(titulo="🏆 Top de balances", descripcion="\n".join(lineas))
    await ctx.reply(embed=embed)

@bot.command(name="topdolar", aliases=["topdólar"])
async def topdolar(ctx):
    """Muestra el top 10 de usuarios con más Dólares guardados ahora mismo"""
    tenedores = [
        (datos.get("nombre", "???"), datos["items"]["Dólar"])
        for datos in inventario_cache.values()
        if datos.get("items", {}).get("Dólar", 0) > 0
    ]

    if not tenedores:
        await ctx.reply(embed=crear_embed(descripcion="Nadie tiene Dólares guardados todavía. Probá `!buy Dólar cantidad`."))
        return

    tenedores.sort(key=lambda t: t[1], reverse=True)
    medallas = ["🥇", "🥈", "🥉"]

    lineas = []
    for i, (nombre, cantidad) in enumerate(tenedores[:10]):
        posicion = medallas[i] if i < 3 else f"`#{i+1}`"
        equivalente = cantidad * dolar_valor
        lineas.append(f"{posicion} **{nombre}** — {formatear_numero(cantidad)} 💵 (≈{formatear_pesos(equivalente)})")

    embed = crear_embed(
        titulo="💵 Top de Dólares",
        descripcion="\n".join(lineas),
        footer=f"Cotización actual: {formatear_pesos(dolar_valor)}",
    )
    await ctx.reply(embed=embed)

@bot.command(name="perfil", aliases=["profile"])
async def perfil(ctx, miembro: discord.Member = None):
    """Resumen completo de un usuario: plata, matrimonio, colección e inventario"""
    objetivo = miembro or ctx.author
    user_id = str(objetivo.id)

    embed = crear_embed(titulo=f"👤 {objetivo.display_name}")
    embed.set_thumbnail(url=objetivo.display_avatar.url)

    # Separador que se usa como "field" entre secciones (zero-width space en el name,
    # el separador como value, inline=False para que ocupe su propia línea)
    def agregar_separador():
        embed.add_field(name="\u200b", value=SEPARADOR, inline=False)

    # --- Plata ---
    datos_balance = balances_cache.get(user_id)
    if datos_balance:
        balance = datos_balance.get("balance", 0)
        banco = datos_balance.get("banco", 0)
        caja = datos_balance.get("caja", 0)
        total = balance + banco

        ranking = sorted(
            balances_cache.items(),
            key=lambda item: item[1].get("balance", 0) + item[1].get("banco", 0),
            reverse=True,
        )
        puesto = next((i for i, (uid, _) in enumerate(ranking, start=1) if uid == user_id), None)
        puesto_txt = f"\n-# Puesto #{puesto} de {len(ranking)}" if puesto else ""

        embed.add_field(
            name="💰 Plata",
            value=(
                f"> Balance: **{formatear_pesos(balance)}**\n"
                f"> Banco: **{formatear_pesos(banco)}**\n"
                f"> Caja de ahorros: **{formatear_pesos(caja)}**\n"
                f"> Total: **{formatear_pesos(total)}**{puesto_txt}"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="💰 Plata", value="-# Todavía no tiene cuenta.", inline=False)

    agregar_separador()

    # --- Matrimonio ---
    datos_matrimonio = matrimonios_cache.get(user_id)
    if datos_matrimonio:
        pareja = datos_matrimonio.get("pareja_nombre", "???")
        desde = formatear_fecha_relativa(datos_matrimonio.get("fecha"))
        embed.add_field(name="💍 Matrimonio", value=f"Casado/a con **{pareja}** ({desde}).", inline=False)
    else:
        embed.add_field(name="💍 Matrimonio", value="-# Soltero/a.", inline=False)

    agregar_separador()

    # --- Colección ---
    datos_char = characters_cache.get(user_id)
    personajes = datos_char.get("personajes", []) if datos_char else []
    if personajes:
        top3 = sorted(
            personajes,
            key=lambda p: parsear_valor(campo_personaje(p, "Valor", "valor")) or 0,
            reverse=True,
        )[:3]
        lineas = [
            f"> 🎴 **{campo_personaje(p, 'Nombre', 'nombre')}** — {formatear_pesos(campo_personaje(p, 'Valor', 'valor'))}"
            for p in top3
        ]
        embed.add_field(
            name=f"📚 Colección ({len(personajes)} personajes)",
            value="\n".join(lineas),
            inline=False,
        )
    else:
        embed.add_field(name="📚 Colección", value="-# No reclamó ningún personaje todavía.", inline=False)

    agregar_separador()

    # --- Inventario ---
    datos_inv = inventario_cache.get(user_id)
    items = datos_inv.get("items", {}) if datos_inv else {}
    if items:
        items_ordenados = sorted(items.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lineas = []
        for nombre_item, cantidad in items_ordenados:
            _, item_shop = buscar_item_shop(nombre_item)
            emoji = item_shop["emoji"] if item_shop else "📦"
            lineas.append(f"> {emoji} **{nombre_item}** — x{cantidad}")
        total_items = sum(items.values())
        embed.add_field(
            name=f"🎒 Inventario ({total_items} objetos)",
            value="\n".join(lineas),
            inline=False,
        )
    else:
        embed.add_field(name="🎒 Inventario", value="-# No compró nada todavía.", inline=False)

    await ctx.reply(embed=embed)

@bot.command(name="depositar")
async def depositar(ctx, cantidad: str = None):
    """Guarda plata en el banco: no se la pueden robar con !robar"""
    global hubo_cambios_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("banco", 0)

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Decime cuánto. Ejemplo: `!depositar 5000`, `!depositar 100k` o `!depositar all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["balance"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata para depositar." if balances_cache[user_id]["balance"] == 0 else "La cantidad tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No tenés esa plata afuera del banco. Tenés **{formatear_pesos(balances_cache[user_id]['balance'])}**."
        ))
        return

    # Si el banco estaba en 0, este depósito arranca el reloj de interés de cero.
    if balances_cache[user_id]["banco"] == 0:
        balances_cache[user_id]["ultimo_interes"] = time.time()

    balances_cache[user_id]["balance"] -= cantidad
    balances_cache[user_id]["banco"] += cantidad
    hubo_cambios_sin_guardar = True

    await ctx.reply(embed=crear_embed(
        titulo="🏦 Depositado",
        descripcion=(
            f"Guardaste **{formatear_pesos(cantidad)}** en el banco.\n"
            f"Balance: **{formatear_pesos(balances_cache[user_id]['balance'])}** — "
            f"Banco: **{formatear_pesos(balances_cache[user_id]['banco'])}**"
        ),
    ))

@bot.command(name="retirar")
async def retirar(ctx, cantidad: str = None):
    """Saca plata del banco de vuelta al balance"""
    global hubo_cambios_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("banco", 0)

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Decime cuánto. Ejemplo: `!retirar 5000`, `!retirar 100k` o `!retirar all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["banco"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata en el banco para retirar." if balances_cache[user_id]["banco"] == 0 else "La cantidad tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["banco"] < cantidad:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No tenés esa plata en el banco. Tenés **{formatear_pesos(balances_cache[user_id]['banco'])}**."
        ))
        return

    balances_cache[user_id]["banco"] -= cantidad
    balances_cache[user_id]["balance"] += cantidad
    if balances_cache[user_id]["banco"] == 0:
        balances_cache[user_id].pop("ultimo_interes", None)
    hubo_cambios_sin_guardar = True

    await ctx.reply(embed=crear_embed(
        titulo="🏦 Retirado",
        descripcion=(
            f"Sacaste **{formatear_pesos(cantidad)}** del banco.\n"
            f"Balance: **{formatear_pesos(balances_cache[user_id]['balance'])}** — "
            f"Banco: **{formatear_pesos(balances_cache[user_id]['banco'])}**"
        ),
    ))

@bot.command(name="depositarcaja")
async def depositarcaja(ctx, cantidad: str = None):
    """Guarda plata en tu Caja de ahorros: a salvo de !robar como el banco,
    pero sin el interés del 5% diario, y no cuenta en !top ni en el ranking de !balance."""
    global hubo_cambios_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("caja", 0)

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Decime cuánto. Ejemplo: `!depositarcaja 5000`, `!depositarcaja 100k` o `!depositarcaja all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["balance"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata para depositar." if balances_cache[user_id]["balance"] == 0 else "La cantidad tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No tenés esa plata afuera del banco. Tenés **{formatear_pesos(balances_cache[user_id]['balance'])}**."
        ))
        return

    balances_cache[user_id]["balance"] -= cantidad
    balances_cache[user_id]["caja"] += cantidad
    hubo_cambios_sin_guardar = True

    await ctx.reply(embed=crear_embed(
        titulo="🔒 Guardado en la Caja de ahorros",
        descripcion=(
            f"Guardaste **{formatear_pesos(cantidad)}** en tu Caja de ahorros.\n"
            f"Balance: **{formatear_pesos(balances_cache[user_id]['balance'])}** — "
            f"Caja: **{formatear_pesos(balances_cache[user_id]['caja'])}**"
        ),
    ))

@bot.command(name="retirarcaja")
async def retirarcaja(ctx, cantidad: str = None):
    """Saca plata de tu Caja de ahorros de vuelta al balance"""
    global hubo_cambios_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("caja", 0)

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Decime cuánto. Ejemplo: `!retirarcaja 5000`, `!retirarcaja 100k` o `!retirarcaja all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["caja"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata en la Caja de ahorros para retirar." if balances_cache[user_id]["caja"] == 0 else "La cantidad tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["caja"] < cantidad:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No tenés esa plata en la Caja de ahorros. Tenés **{formatear_pesos(balances_cache[user_id]['caja'])}**."
        ))
        return

    balances_cache[user_id]["caja"] -= cantidad
    balances_cache[user_id]["balance"] += cantidad
    hubo_cambios_sin_guardar = True

    await ctx.reply(embed=crear_embed(
        titulo="🔓 Retirado de la Caja de ahorros",
        descripcion=(
            f"Sacaste **{formatear_pesos(cantidad)}** de tu Caja de ahorros.\n"
            f"Balance: **{formatear_pesos(balances_cache[user_id]['balance'])}** — "
            f"Caja: **{formatear_pesos(balances_cache[user_id]['caja'])}**"
        ),
    ))

@bot.command(name="pay")
async def pay(ctx, miembro: discord.Member = None, cantidad: str = None):
    """Le pasás plata de tu balance (no del banco) a otro usuario"""
    global hubo_cambios_sin_guardar

    if miembro is None or cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!pay @usuario 5000`, `!pay @usuario 100k` o `!pay @usuario all`"))
        return
    if miembro.id == ctx.author.id:
        await ctx.reply(embed=crear_embed(descripcion="No te podés pagar a vos mismo."))
        return
    if miembro.bot:
        await ctx.reply(embed=crear_embed(descripcion="No le podés pagar a un bot."))
        return

    emisor_id = str(ctx.author.id)
    receptor_id = str(miembro.id)

    if emisor_id not in balances_cache:
        balances_cache[emisor_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    if receptor_id not in balances_cache:
        balances_cache[receptor_id] = {"nombre": miembro.display_name, "balance": 0, "banco": 0}

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="No entendí la cantidad. Ejemplo: `!pay @usuario 5000`, `!pay @usuario 100k` o `!pay @usuario all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[emisor_id].get("balance", 0)
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata para pagar." if balances_cache[emisor_id].get("balance", 0) == 0 else "La cantidad tiene que ser mayor a 0."))
        return

    balance_emisor = balances_cache[emisor_id].get("balance", 0)
    if balance_emisor < cantidad:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No tenés esa plata afuera del banco. Tenés **{formatear_pesos(balance_emisor)}**."
        ))
        return

    balances_cache[emisor_id]["balance"] = balance_emisor - cantidad
    balances_cache[receptor_id]["balance"] = balances_cache[receptor_id].get("balance", 0) + cantidad
    hubo_cambios_sin_guardar = True

    await ctx.reply(embed=crear_embed(
        titulo="💸 Pago enviado",
        descripcion=(
            f"Le pagaste **{formatear_pesos(cantidad)}** a **{miembro.display_name}**.\n"
            f"Tu balance actual: **{formatear_pesos(balances_cache[emisor_id]['balance'])}**"
        ),
    ))

@bot.command(name="givechar")
async def givechar(ctx, *, texto: str = None):
    """Le regalás a otro usuario un personaje de tu colección (solo uno que tengas)"""
    global hubo_cambios_characters_sin_guardar

    uso = "Usalo así: `!givechar nombre del personaje @usuario`"
    if not texto:
        await ctx.reply(embed=crear_embed(descripcion=uso))
        return

    # El nombre puede tener espacios, así que sacamos la mención del texto y el resto es el nombre.
    menciones = re.findall(r"<@!?(\d+)>", texto)
    if not menciones:
        await ctx.reply(embed=crear_embed(descripcion=uso))
        return
    miembro = ctx.guild.get_member(int(menciones[-1])) if ctx.guild else None
    if miembro is None:
        await ctx.reply(embed=crear_embed(descripcion="No encontré a ese usuario en el server."))
        return
    nombre_buscado = re.sub(r"<@!?\d+>", "", texto).strip()
    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion=uso))
        return

    if miembro.id == ctx.author.id:
        await ctx.reply(embed=crear_embed(descripcion="No te podés dar un personaje a vos mismo."))
        return
    if miembro.bot:
        await ctx.reply(embed=crear_embed(descripcion="No le podés dar un personaje a un bot."))
        return

    mis_personajes = characters_cache.get(str(ctx.author.id), {}).get("personajes", [])

    # Primero nombre exacto (sin importar mayúsculas); si no, un único nombre que lo contenga.
    buscado = nombre_buscado.casefold()
    exactos = [p for p in mis_personajes if str(campo_personaje(p, "Nombre", "nombre")).casefold() == buscado]
    if exactos:
        elegido = exactos[0]
    else:
        parciales = [p for p in mis_personajes if buscado in str(campo_personaje(p, "Nombre", "nombre")).casefold()]
        if len(parciales) == 1:
            elegido = parciales[0]
        elif len(parciales) > 1:
            nombres = ", ".join(f"**{campo_personaje(p, 'Nombre', 'nombre')}**" for p in parciales[:10])
            await ctx.reply(embed=crear_embed(descripcion=f"Hay varios que coinciden: {nombres}. Poné el nombre completo."))
            return
        else:
            await ctx.reply(embed=crear_embed(descripcion=f"No tenés ningún personaje llamado **{nombre_buscado}** en tu colección."))
            return

    nombre_personaje = campo_personaje(elegido, "Nombre", "nombre")

    if usuario_ya_tiene_personaje(miembro, elegido):
        await ctx.reply(embed=crear_embed(
            descripcion=f"**{miembro.display_name}** ya tiene a **{nombre_personaje}**, no puede repetirlo."
        ))
        return

    # Sacamos ese personaje puntual (por identidad) de tu lista y se lo pasamos al otro.
    mis_personajes[:] = [p for p in mis_personajes if p is not elegido]
    agregar_personaje_a_coleccion(miembro, elegido)
    hubo_cambios_characters_sin_guardar = True

    embed = crear_embed(
        titulo="🎁 Personaje regalado",
        descripcion=f"**{ctx.author.display_name}** le dio a **{miembro.display_name}** el personaje **{nombre_personaje}**.",
    )
    imagen = campo_personaje(elegido, "Imagen", "imagen", default=None)
    if imagen:
        embed.set_thumbnail(url=imagen)
    await ctx.reply(embed=embed)

async def vender_item_inventario(ctx, entrada):
    """Intenta vender un ítem del inventario (hoy en día, solo el Dólar tiene
    'vendible': True) en vez de un personaje. Devuelve True si se hizo cargo
    del pedido (venta hecha, o un error propio de vender ítems que ya le
    mostró al usuario), False si ni siquiera encontró un ítem con ese nombre
    — así !vender puede mostrar su propio mensaje de 'no encontré nada'."""
    global hubo_cambios_sin_guardar, hubo_cambios_inventario_sin_guardar

    # Acepta cantidad con sufijo (100k, 2m, all, etc) usando el mismo parser que el resto del bot
    partes = entrada.strip().rsplit(" ", 1)
    cantidad = 1
    nombre_item_buscado = entrada.strip()
    if len(partes) == 2:
        cant_parseada = parsear_cantidad(partes[1])
        if cant_parseada is not None:
            nombre_item_buscado = partes[0].strip()
            cantidad = cant_parseada

    categoria, item = buscar_item_shop(nombre_item_buscado)
    if item is None or not item.get("vendible"):
        return False

    user_id = str(ctx.author.id)
    items_usuario = inventario_cache.get(user_id, {}).get("items", {})

    if cantidad == "all":
        cantidad = items_usuario.get(item["nombre"], 0)
        if cantidad <= 0:
            await ctx.reply(embed=crear_embed(
                descripcion=f"No tenés ningún **{item['emoji']} {item['nombre']}** para vender."
            ))
            return True

    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="La cantidad tiene que ser mayor a 0."))
        return True

    tiene = items_usuario.get(item["nombre"], 0)
    if tiene < cantidad:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No tenés esa cantidad de **{item['emoji']} {item['nombre']}**. Tenés **{tiene}**."
        ))
        return True

    precio_unitario = item["precio"]
    bruto = precio_unitario * cantidad
    comision = int(bruto * COMISION_VENTA_DOLAR)
    ganancia = bruto - comision

    quitar_item_del_inventario(ctx.author, item["nombre"], cantidad)
    hubo_cambios_inventario_sin_guardar = True

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("banco", 0)
    balances_cache[user_id]["balance"] += ganancia
    hubo_cambios_sin_guardar = True
    sumar_a_banca(comision)

    embed = crear_embed(
        titulo=f"{item['emoji']} {item['nombre']} vendido",
        descripcion=(
            f"Vendiste **{cantidad}x {item['nombre']}** a {formatear_pesos(precio_unitario)} c/u.\n\n"
            f"> 💵 Bruto: **{formatear_pesos(bruto)}**\n"
            f"> 📈 Te dieron ({int((1 - COMISION_VENTA_DOLAR) * 100)}%): **{formatear_pesos(ganancia)}**\n"
            f"> 🏦 Comisión ({int(COMISION_VENTA_DOLAR * 100)}%): **{formatear_pesos(comision)}**\n\n"
            f"Balance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
        ),
    )
    await ctx.reply(embed=embed)

    try:
        await guardar_inventario_en_github()
        await guardar_balances_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar tras vender un ítem: {e}", flush=True)

    return True

@bot.command(name="vender", aliases=["sell"])
async def vender(ctx, *, nombre_buscado: str = None):
    """Vende un personaje de tu colección a cambio de Pesos. Te doy el 70% del
    valor; el 30% restante se lo queda la banca."""
    global hubo_cambios_sin_guardar, hubo_cambios_characters_sin_guardar

    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return

    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion=f"Usalo así: `!vender Gojo Satoru`. Te doy el **{int(PORCENTAJE_VENTA*100)}%** de su valor en Pesos."))
        return

    user_id = str(ctx.author.id)
    mis_personajes = characters_cache.get(user_id, {}).get("personajes", [])

    if not mis_personajes:
        await ctx.reply(embed=crear_embed(descripcion="No tenés ningún personaje para vender. Probá `!rw` para conseguir alguno."))
        return

    # Buscar el personaje: nombre exacto > parcial único > probar si es un ítem (ej. Dólar) > error
    buscado = nombre_buscado.casefold().strip()
    exactos = [p for p in mis_personajes if str(campo_personaje(p, "Nombre", "nombre")).casefold() == buscado]
    if exactos:
        elegido = exactos[0]
    else:
        parciales = [p for p in mis_personajes if buscado in str(campo_personaje(p, "Nombre", "nombre")).casefold()]
        if len(parciales) == 1:
            elegido = parciales[0]
        elif len(parciales) > 1:
            nombres = ", ".join(f"**{campo_personaje(p, 'Nombre', 'nombre')}**" for p in parciales[:10])
            await ctx.reply(embed=crear_embed(descripcion=f"Hay varios que coinciden: {nombres}. Poné el nombre completo."))
            return
        else:
            vendido = await vender_item_inventario(ctx, nombre_buscado)
            if vendido:
                return
            await ctx.reply(embed=crear_embed(descripcion=f"No tenés ningún personaje ni ítem llamado **{nombre_buscado}**. Mirá `!coleccion` o `!inv`."))
            return

    nombre_personaje = campo_personaje(elegido, "Nombre", "nombre")
    valor_original = parsear_valor(campo_personaje(elegido, "Valor", "valor")) or 0
    ganancia = int(valor_original * PORCENTAJE_VENTA)
    comision_banca = valor_original - ganancia

    # Aseguramos que el vendedor tenga cuenta
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("banco", 0)

    # Sacamos el personaje de la colección (por identidad, no por igualdad)
    mis_personajes[:] = [p for p in mis_personajes if p is not elegido]
    hubo_cambios_characters_sin_guardar = True

    # Le acreditamos la plata al balance del vendedor y la comisión a la banca
    balances_cache[user_id]["balance"] += ganancia
    hubo_cambios_sin_guardar = True
    sumar_a_banca(comision_banca)

    embed = crear_embed(
        titulo="💰 Personaje vendido",
        descripcion=(
            f"Vendiste a **{nombre_personaje}**.\n\n"
            f"> 💵 Valor original: **{formatear_pesos(valor_original)}**\n"
            f"> 📈 Te dieron ({int(PORCENTAJE_VENTA*100)}%): **{formatear_pesos(ganancia)}**\n"
            f"> 🏦 Comisión de la banca ({int((1-PORCENTAJE_VENTA)*100)}%): **{formatear_pesos(comision_banca)}**\n\n"
            f"Balance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
        ),
    )
    imagen = campo_personaje(elegido, "Imagen", "imagen", default=None)
    if imagen:
        embed.set_thumbnail(url=imagen)
    await ctx.reply(embed=embed)

    try:
        await guardar_characters_en_github()
        await guardar_balances_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar la venta al toque: {e}", flush=True)

@bot.command(name="tasacion", aliases=["tasart", "precio"])
async def tasacion(ctx, *, nombre_buscado: str = None):
    """Te dice cuánto te darían por vender un personaje, sin venderlo."""
    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!tasacion Gojo Satoru`"))
        return

    user_id = str(ctx.author.id)
    mis_personajes = characters_cache.get(user_id, {}).get("personajes", [])
    if not mis_personajes:
        await ctx.reply(embed=crear_embed(descripcion="No tenés personajes para tasar. Probá `!rw` para conseguir alguno."))
        return

    buscado = nombre_buscado.casefold().strip()
    encontrados = [p for p in mis_personajes if buscado in str(campo_personaje(p, "Nombre", "nombre")).casefold()]
    if not encontrados:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés ningún personaje que coincida con **{nombre_buscado}**."))
        return

    lineas = []
    for p in encontrados[:10]:
        nombre_p = campo_personaje(p, "Nombre", "nombre")
        valor_p = parsear_valor(campo_personaje(p, "Valor", "valor")) or 0
        lineas.append(
            f"> 🎴 **{nombre_p}** — vale **{formatear_pesos(valor_p)}**, te darían **{formatear_pesos(int(valor_p * PORCENTAJE_VENTA))}**"
        )

    embed = crear_embed(
        titulo="🔎 Tasación",
        descripcion="\n".join(lineas) + f"\n\n-# Recordá: te doy el {int(PORCENTAJE_VENTA*100)}% del valor al vender. El resto va a la banca.",
    )
    await ctx.reply(embed=embed)

@bot.command(name="marry", aliases=["casarse", "casamiento"])
async def marry(ctx, miembro: discord.Member = None):
    """Te casás con Catherine (solo el dueño de la banca) o con otro usuario. !marry @usuario"""
    global hubo_cambios_matrimonios_sin_guardar

    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return

    user_id = str(ctx.author.id)

    # ---------- Caso 1: sin mención -> solo el dueño de la banca se casa con Catherine ----------
    if miembro is None:
        if user_id != ID_BANCA:
            embed = crear_embed(
                descripcion=(
                    f"### 💍 †Catherine†\n"
                    f"{SEPARADOR}\n\n"
                    f"*Se queda callada un momento, con las mejillas un poco coloradas.*\n\n"
                    f"Eso no es algo que pueda aceptar de cualquiera. Lo siento."
                ),
            )
            await ctx.reply(embed=embed)
            return
        # Es el dueño de la banca: se casa con Catherine
        if esta_casado(user_id):
            await ctx.reply(embed=crear_embed(descripcion="Ya estás casado. Primero usá `!divorcio` si querés separarte."))
            return
        # Creamos un pseudo-usuario para Catherine con el mismo id del bot para reusar la función
        registrar_matrimonio(ctx.author, bot.user, usuario2_es_bot=True)
        embed = crear_embed(
            descripcion=(
                f"### 💍 †Catherine†\n"
                f"{SEPARADOR}\n\n"
                f"*Levanta la mirada lentamente, procesando lo que acaba de escuchar. Sus dedos se enredan un instante con el borde de sus notas antes de hablar.*\n\n"
                f"... ¿En serio me lo estás preguntando a mí? *Baja la voz casi susurrando.* Está bien. Acepto.\n\n"
                f"> 💞 **Catherine** ahora está casada con **{ctx.author.display_name}**"
            ),
            footer="Que dure.",
        )
        await ctx.reply(embed=embed)
        try:
            await guardar_matrimonios_en_github()
        except Exception as e:
            print(f"⚠️ No pude guardar el matrimonio al toque: {e}", flush=True)
        return

    # ---------- Caso 2: mención a Catherine (el bot) -> mismo trato que sin mención ----------
    if miembro.id == bot.user.id:
        if user_id != ID_BANCA:
            embed = crear_embed(
                descripcion=(
                    f"### 💍 †Catherine†\n"
                    f"{SEPARADOR}\n\n"
                    f"*Te mira fijamente un segundo, y después desvía la mirada.*\n\n"
                    f"No. Con vos no."
                ),
            )
            await ctx.reply(embed=embed)
            return
        if esta_casado(user_id):
            await ctx.reply(embed=crear_embed(descripcion="Ya estás casado. Primero usá `!divorcio` si querés separarte."))
            return
        registrar_matrimonio(ctx.author, bot.user, usuario2_es_bot=True)
        embed = crear_embed(
            descripcion=(
                f"### 💍 †Catherine†\n"
                f"{SEPARADOR}\n\n"
                f"*Se queda quieta por unos segundos, como si estuviera eligiendo bien qué responder. Al final asiente apenas con la cabeza.*\n\n"
                f"... Está bien. Acepto.\n\n"
                f"> 💞 **Catherine** ahora está casada con **{ctx.author.display_name}**"
            ),
            footer="Que dure.",
        )
        await ctx.reply(embed=embed)
        try:
            await guardar_matrimonios_en_github()
        except Exception as e:
            print(f"⚠️ No pude guardar el matrimonio al toque: {e}", flush=True)
        return

    # ---------- Caso 3: mención a otro usuario ----------
    if miembro.id == ctx.author.id:
        await ctx.reply(embed=crear_embed(descripcion="No te podés casar con vos mismo."))
        return
    if miembro.bot:
        await ctx.reply(embed=crear_embed(descripcion="No te podés casar con ese bot."))
        return

    objetivo_id = str(miembro.id)

    if esta_casado(user_id):
        await ctx.reply(embed=crear_embed(descripcion="Ya estás casado. Primero usá `!divorcio` si querés separarte."))
        return
    if esta_casado(objetivo_id):
        await ctx.reply(embed=crear_embed(descripcion=f"**{miembro.display_name}** ya está casado con alguien más."))
        return

    registrar_matrimonio(ctx.author, miembro, usuario2_es_bot=False)
    embed = crear_embed(
        descripcion=(
            f"### 💍 ¡Boda!\n"
            f"{SEPARADOR}\n\n"
            f"**{ctx.author.display_name}** y **{miembro.display_name}** acaban de casarse.\n\n"
            f"> 💞 ¡Felicidades a los novios!"
        ),
        footer="Que dure.",
    )
    await ctx.reply(embed=embed)
    try:
        await guardar_matrimonios_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el matrimonio al toque: {e}", flush=True)

@bot.command(name="divorcio", aliases=["divorciar"])
async def divorcio(ctx):
    """Te divorciás. Cuesta 5.000 a cada uno (si es con Catherine, 10.000 a vos)."""
    global hubo_cambios_sin_guardar

    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return

    user_id = str(ctx.author.id)

    if not esta_casado(user_id):
        await ctx.reply(embed=crear_embed(descripcion="No estás casado con nadie. Usá `!marry @usuario` si querés casarte."))
        return

    datos = matrimonios_cache[user_id]
    pareja_nombre = datos.get("pareja_nombre", "???")
    pareja_es_bot = datos.get("pareja_es_bot", False)
    pareja_id = str(datos.get("pareja_id"))

    # Aseguramos que el usuario tenga cuenta
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}

    costo_usuario = COSTO_DIVORCIO_POR_PERSONA
    costo_pareja = 0 if pareja_es_bot else COSTO_DIVORCIO_POR_PERSONA
    # Si la pareja es Catherine, ella no puede pagar: el usuario paga los dos.
    if pareja_es_bot:
        costo_usuario = COSTO_DIVORCIO_POR_PERSONA * 2

    if balances_cache[user_id].get("balance", 0) < costo_usuario:
        await ctx.reply(embed=crear_embed(
            descripcion=(
                f"Necesitás **{formatear_pesos(costo_usuario)}** afuera del banco para divorciarte "
                f"y tenés **{formatear_pesos(balances_cache[user_id].get('balance', 0))}**."
            )
        ))
        return

    # Si la pareja es otro usuario, chequeamos que tenga la plata también
    if not pareja_es_bot:
        if pareja_id not in balances_cache:
            balances_cache[pareja_id] = {"nombre": pareja_nombre, "balance": 0, "banco": 0}
        if balances_cache[pareja_id].get("balance", 0) < costo_pareja:
            await ctx.reply(embed=crear_embed(
                descripcion=(
                    f"**{pareja_nombre}** no tiene los **{formatear_pesos(costo_pareja)}** afuera del banco "
                    f"para cubrir su parte del divorcio. No puedo proceder hasta que tenga la plata."
                )
            ))
            return

    # Cobramos
    balances_cache[user_id]["balance"] -= costo_usuario
    if not pareja_es_bot:
        balances_cache[pareja_id]["balance"] -= costo_pareja

    total_cobrado = costo_usuario + costo_pareja
    sumar_a_banca(total_cobrado)
    hubo_cambios_sin_guardar = True

    eliminar_matrimonio(user_id)

    if pareja_es_bot:
        detalle = f"Te divorciaste de **†Catherine†**. Te costó **{formatear_pesos(costo_usuario)}** (los dos lados, porque ella no tiene cuenta)."
    else:
        detalle = (
            f"Te divorciaste de **{pareja_nombre}**.\n"
            f"Cada uno pagó **{formatear_pesos(COSTO_DIVORCIO_POR_PERSONA)}**."
        )

    embed = crear_embed(
        descripcion=(
            f"### 💔 Divorcio\n"
            f"{SEPARADOR}\n\n"
            f"{detalle}\n\n"
            f"> 💸 Tu balance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
        ),
    )
    await ctx.reply(embed=embed)
    try:
        await guardar_matrimonios_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el divorcio al toque: {e}", flush=True)

COOLDOWN_ROBO_DESCONEXION_SEGUNDOS = 6 * 60 * 60
PROBABILIDAD_ROBO_EXITO = 0.10
PROBABILIDAD_ROBO_PENALIZACION = 0.10  # además del 10% de éxito (roll único de 0 a 1)
PENALIZACION_ROBO_FALLIDO = 0.05  # 5% de la plata del ladrón

@bot.command(name="robar")
@commands.cooldown(1, 600, commands.BucketType.user)  # sin esto, spamear el comando garantiza el 10% tarde o temprano
async def robar(ctx, objetivo: discord.Member = None):
    """10% de robarle todo el balance (lo que NO tenga en el banco) a alguien
    desconectado hace 6hs o más; 10% de perder vos el 5% de tu plata si te
    agarran; 80% no pasa nada."""
    global hubo_cambios_sin_guardar

    if objetivo is None:
        await ctx.reply(embed=crear_embed(descripcion="Decime a quién. Ejemplo: `!robar @usuario`"))
        return
    if objetivo.id == ctx.author.id:
        await ctx.reply(embed=crear_embed(descripcion="No te podés robar a vos mismo."))
        return
    if objetivo.bot:
        await ctx.reply(embed=crear_embed(descripcion="No le podés robar a un bot."))
        return

    desde = usuarios_desconectados_desde.get(str(objetivo.id))
    if objetivo.status != discord.Status.offline or desde is None:
        await ctx.reply(embed=crear_embed(
            descripcion=f"**{objetivo.display_name}** no está desconectado hace 6hs o más ahora mismo."
        ))
        return

    tiempo_offline = time.time() - desde
    if tiempo_offline < COOLDOWN_ROBO_DESCONEXION_SEGUNDOS:
        restante = COOLDOWN_ROBO_DESCONEXION_SEGUNDOS - tiempo_offline
        await ctx.reply(embed=crear_embed(
            descripcion=f"**{objetivo.display_name}** está desconectado, pero todavía no pasaron las 6hs. Faltan **{formatear_tiempo_restante(restante)}**."
        ))
        return

    ladron_id = str(ctx.author.id)
    objetivo_id = str(objetivo.id)

    if ladron_id not in balances_cache:
        balances_cache[ladron_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    if objetivo_id not in balances_cache:
        balances_cache[objetivo_id] = {"nombre": objetivo.display_name, "balance": 0, "banco": 0}
    balances_cache[ladron_id].setdefault("banco", 0)
    balances_cache[objetivo_id].setdefault("banco", 0)

    # Aplicar buffs
    buff_trebol = obtener_buff(ladron_id, "suerte_robo")
    buff_pata = obtener_buff(ladron_id, "pata_conejo")

    prob_exito = PROBABILIDAD_ROBO_EXITO
    if buff_trebol:
        prob_exito += buff_trebol["valor"]

    penalizacion = PENALIZACION_ROBO_FALLIDO
    if buff_pata:
        penalizacion = max(penalizacion - buff_pata["valor"], 0)

    balance_objetivo = balances_cache[objetivo_id].get("balance", 0)
    roll = random.random()

    if roll < prob_exito:
        if balance_objetivo <= 0:
            embed = crear_embed(
                titulo="🥷 Casi",
                descripcion=f"Tuviste suerte, pero **{objetivo.display_name}** no tiene plata afuera del banco para robarle.",
            )
        else:
            balances_cache[objetivo_id]["balance"] = 0
            balances_cache[ladron_id]["balance"] = balances_cache[ladron_id].get("balance", 0) + balance_objetivo
            hubo_cambios_sin_guardar = True
            embed = crear_embed(
                titulo="🥷 Robo exitoso",
                descripcion=(
                    f"Le afanaste **{formatear_pesos(balance_objetivo)}** a **{objetivo.display_name}**.\n"
                    f"Tu balance actual: **{formatear_pesos(balances_cache[ladron_id]['balance'])}**"
                ),
            )
    elif roll < prob_exito + PROBABILIDAD_ROBO_PENALIZACION:
        balance_ladron = balances_cache[ladron_id].get("balance", 0)
        perdida = int(balance_ladron * penalizacion)
        balances_cache[ladron_id]["balance"] = balance_ladron - perdida
        hubo_cambios_sin_guardar = True
        embed = crear_embed(
            titulo="🚨 Te agarraron",
            descripcion=(
                f"Te descubrieron intentando robarle a **{objetivo.display_name}** y perdiste **{formatear_pesos(perdida)}** vos.\n"
                f"Tu balance actual: **{formatear_pesos(balances_cache[ladron_id]['balance'])}**"
            ),
        )
    else:
        embed = crear_embed(
            titulo="🕵️ Nada",
            descripcion=f"Lo intentaste, pero no pasó nada. **{objetivo.display_name}** ni se enteró.",
        )

    await ctx.reply(embed=embed)

MENSAJES_TRABAJO = [
    'Le vendiste un frasco con "aire de alta montaña" a un hippie en la calle y te lo compró sin dudarlo.',
    "Ibas por la calle y encontraste una billetera de un tal Juan tirada. Robaste la plata y dejaste la billetera.",
    "Fuiste a un casting de canto, cantaste tan mal que te pagaron para que te fueras.",
    "Te dijeron que te pagaban si movías un mueble, agarraste el mueble y te cayó en el pie. El dueño sintió lástima y te pagó por pena.",
    "Viste a un mimo en la calle, este te imitó, te pusiste a llorar y él te pagó para que te callaras.",
    "Creaste la cura del cáncer, te fuiste a dormir sabiendo que ibas a revolucionar el mundo. Al día siguiente había desaparecido todo, simplemente encontraste:",
    'Quisiste hacerte el picante haciendo un "caballito" en la bici frente a unos pibes, te estrellaste contra un contenedor de basura y un viejo te tiró plata para que te compres dignidad.',
    "Te sentaste en la esquina porque estabas cansado, la gente te confundió con un fisura y te empezó a dar plata.",
    "Fuiste a la casa de tu abuela, esta te dio plata a escondidas.",
    "Entraste a la casa de tu amigo, viste la alcancía y la rompiste, agarraste la plata y te fuiste corriendo.",
]

async def ejecutar_w(ctx):
    global hubo_cambios_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}
    balances_cache[user_id].setdefault("banco", 0)

    ganancia = random.randint(5000, 6500)

    # Aplicar buff del Bono del gobierno (+20% de ganancia)
    buff_bono = obtener_buff(user_id, "bono_w")
    if buff_bono:
        ganancia = int(ganancia * (1 + buff_bono["valor"]))

    balances_cache[user_id]["balance"] += ganancia
    hubo_cambios_sin_guardar = True

    mensaje = random.choice(MENSAJES_TRABAJO)
    texto_buff = f"\n-# 🏛️ Bono del gobierno activo (+{int(buff_bono['valor']*100)}%)" if buff_bono else ""

    embed = crear_embed(
        titulo="💼 A trabajar",
        descripcion=(
            f"{mensaje}\n"
            f"**+{formatear_pesos(ganancia)}**{texto_buff}\n\n"
            f"Balance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
        ),
    )
    await ctx.reply(embed=embed)

@bot.command(name="w")
async def work(ctx):
    """Da una cantidad random de Pesos (5.000 a 6.500) con un mensaje random"""
    user_id = str(ctx.author.id)
    restante = cooldown_restante(cooldown_w_usuario, user_id)
    if restante > 0:
        await ctx.reply(embed=crear_embed(
            descripcion=f"⏳ Todavía no. Podés volver a trabajar en **{formatear_tiempo_restante(restante)}**.\n-# Tip: un `Café doble` te baja 2 min del cooldown."
        ))
        return
    await ejecutar_w(ctx)
    poner_cooldown(cooldown_w_usuario, user_id, cooldown_w_efectivo(user_id))

@bot.command(name="wAdmin", aliases=["wadmin"], hidden=True)
async def work_admin(ctx):
    """Igual que !w pero sin cooldown. Solo vos."""
    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return
    await ejecutar_w(ctx)

COOLDOWN_MENDIGAR_SEGUNDOS = 20 * 60
cooldown_mendigar_usuario = {}  # user_id (str) -> timestamp hasta el que no puede volver a mendigar

class DonarModal(discord.ui.Modal):
    def __init__(self, vista):
        super().__init__(title="Donar plata")
        self.vista = vista
        self.cantidad_input = discord.ui.TextInput(
            label="¿Cuánto querés donar?",
            placeholder="Ej: 5000, 100k, all...",
            required=True,
            max_length=20,
        )
        self.add_item(self.cantidad_input)

    async def on_submit(self, interaction: discord.Interaction):
        global hubo_cambios_sin_guardar

        if self.vista.terminado:
            await interaction.response.send_message("Esta colecta ya terminó.", ephemeral=True)
            return
        if interaction.user.id == self.vista.mendigo.id:
            await interaction.response.send_message("No te podés donar plata a vos mismo.", ephemeral=True)
            return

        donante_id = str(interaction.user.id)
        if donante_id not in balances_cache:
            balances_cache[donante_id] = {"nombre": interaction.user.display_name, "balance": 0, "banco": 0}
        balance_donante = balances_cache[donante_id].get("balance", 0)

        cantidad = parsear_cantidad(self.cantidad_input.value)
        if cantidad == "all":
            cantidad = balance_donante
        if cantidad is None or cantidad <= 0:
            await interaction.response.send_message("Esa cantidad no es válida.", ephemeral=True)
            return
        if balance_donante < cantidad:
            await interaction.response.send_message(
                f"No tenés esa plata afuera del banco. Tenés **{formatear_pesos(balance_donante)}**.", ephemeral=True
            )
            return

        balances_cache[donante_id]["balance"] = balance_donante - cantidad
        self.vista.total_donado += cantidad
        hubo_cambios_sin_guardar = True

        await interaction.response.edit_message(embed=self.vista.construir_embed(), view=self.vista)
        await interaction.followup.send(
            f"Donaste **{formatear_pesos(cantidad)}** a **{self.vista.mendigo.display_name}**.", ephemeral=True
        )

class MendigarView(discord.ui.View):
    """Colecta abierta a cualquiera (no solo a quien la inició, por eso no hay
    interaction_check acá) durante 10 minutos. Al vencer el timeout de la View,
    se le paga al mendigo todo lo donado de una."""

    DURACION_SEGUNDOS = 10 * 60

    def __init__(self, mendigo):
        super().__init__(timeout=self.DURACION_SEGUNDOS)
        self.mendigo = mendigo
        self.total_donado = 0
        self.terminado = False
        self.mensaje = None

    def construir_embed(self):
        descripcion = (
            f"te sentaste en la esquina y le pediste a los miembros que te den plata\n"
            f"{SEPARADOR}\n\n"
            f"**Donado**: {formatear_pesos(self.total_donado)}"
        )
        return crear_embed(titulo="🪙 Mendigar", descripcion=descripcion, footer=f"{self.mendigo.display_name} está mendigando")

    @discord.ui.button(label="💰 Donar", style=discord.ButtonStyle.success)
    async def donar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.terminado:
            await interaction.response.send_message("Esta colecta ya terminó.", ephemeral=True)
            return
        if interaction.user.id == self.mendigo.id:
            await interaction.response.send_message("No te podés donar plata a vos mismo.", ephemeral=True)
            return
        await interaction.response.send_modal(DonarModal(self))

    async def on_timeout(self):
        global hubo_cambios_sin_guardar
        self.terminado = True
        for item in self.children:
            item.disabled = True

        if self.total_donado > 0:
            user_id = str(self.mendigo.id)
            if user_id not in balances_cache:
                balances_cache[user_id] = {"nombre": self.mendigo.display_name, "balance": 0, "banco": 0}
            balances_cache[user_id]["balance"] = balances_cache[user_id].get("balance", 0) + self.total_donado
            hubo_cambios_sin_guardar = True
            try:
                await guardar_balances_en_github()
            except Exception as e:
                print(f"⚠️ No pude guardar lo recaudado en !mendigar: {e}", flush=True)

        if self.mensaje is None:
            return
        descripcion = (
            f"La colecta terminó, gracias a todos los que donaron.\n"
            f"{SEPARADOR}\n\n"
            f"**Donado**: {formatear_pesos(self.total_donado)}"
        )
        embed = crear_embed(titulo="🪙 Mendigar (terminado)", descripcion=descripcion, footer=f"{self.mendigo.display_name} dejó de mendigar")
        try:
            await self.mensaje.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

@bot.command(name="mendigar")
async def mendigar(ctx):
    """Abrís una colecta de 10 minutos para que te donen plata (cooldown: 20 min)"""
    user_id = str(ctx.author.id)
    restante = cooldown_restante(cooldown_mendigar_usuario, user_id)
    if restante > 0:
        await ctx.reply(embed=crear_embed(descripcion=f"⏳ Todavía no. Probá de nuevo en **{formatear_tiempo_restante(restante)}**."))
        return

    poner_cooldown(cooldown_mendigar_usuario, user_id, COOLDOWN_MENDIGAR_SEGUNDOS)

    vista = MendigarView(ctx.author)
    vista.mensaje = await ctx.reply(embed=vista.construir_embed(), view=vista)

@bot.command(name="dolar", aliases=["dólar", "usd"])
async def dolar(ctx):
    """Muestra la cotización actual del Dólar Catherine (sube/baja cada 4hs)"""
    descripcion = (
        f"**Valor actual**: {formatear_pesos(dolar_valor)}\n\n"
        f"Se compra con `!buy Dólar cantidad` y se vende con `!vender Dólar cantidad` "
        f"(comisión de venta: {int(COMISION_VENTA_DOLAR * 100)}%). Cambia cada 4hs, nunca baja de {formatear_pesos(DOLAR_VALOR_MINIMO)}."
    )
    footer = None
    if dolar_ultima_actualizacion:
        proxima = dolar_ultima_actualizacion + 4 * 60 * 60
        restante = proxima - time.time()
        if restante > 0:
            footer = f"Próximo cambio en {formatear_tiempo_restante(restante)}"

    embed = crear_embed(titulo="💵 Dólar Catherine", descripcion=descripcion, footer=footer)
    await ctx.reply(embed=embed)

@bot.command(name="setgeneralchannel", hidden=True)
async def setgeneralchannel(ctx):
    """Configura este canal para que Catherine anuncie las subas/bajadas del dólar. Solo vos."""
    global dolar_canal_anuncios, hubo_cambios_dolar_sin_guardar

    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return

    dolar_canal_anuncios = ctx.channel.id
    hubo_cambios_dolar_sin_guardar = True
    try:
        await guardar_dolar_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el canal del dólar: {e}", flush=True)

    await ctx.reply(embed=crear_embed(descripcion=f"✅ Listo, voy a anunciar los cambios del dólar en {ctx.channel.mention}."))

@bot.command(name="cf")
async def coinflip(ctx, opcion: str = None, cantidad: str = None):
    """Apuesta plata a cara o cruz, 50/50"""
    global hubo_cambios_sin_guardar

    if opcion is None or cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!cf cara 500`, `!cf cruz 100k` o `!cf cara all`"))
        return

    opcion = opcion.lower()
    if opcion not in ("cara", "cruz"):
        await ctx.reply(embed=crear_embed(descripcion="Elegí `cara` o `cruz`."))
        return

    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="No entendí la apuesta. Ejemplo: `!cf cara 500`, `!cf cruz 100k` o `!cf cara all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["balance"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata para apostar." if balances_cache[user_id]["balance"] == 0 else "La apuesta tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés esa plata. Tu balance es **{formatear_pesos(balances_cache[user_id]['balance'])}**."))
        return

    # Chance base 50%. El Amuleto del ludópata suma +5% a favor del jugador.
    buff_amuleto = obtener_buff(user_id, "suerte_apuesta")
    chance_ganar = 0.5 + (buff_amuleto["valor"] if buff_amuleto else 0)
    gano = random.random() < chance_ganar

    if gano:
        resultado = opcion
    else:
        resultado = "cruz" if opcion == "cara" else "cara"

    if gano:
        balances_cache[user_id]["balance"] += cantidad
        descripcion = f"Salió **{resultado}**. Ganaste **{formatear_pesos(cantidad)}**.\nBalance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
    else:
        balances_cache[user_id]["balance"] -= cantidad
        sumar_a_banca(cantidad)
        descripcion = f"Salió **{resultado}**. Perdiste **{formatear_pesos(cantidad)}**.\nBalance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"

    hubo_cambios_sin_guardar = True
    await ctx.reply(embed=crear_embed(titulo="🪙 Coinflip", descripcion=descripcion))

@bot.command(name="roulette", aliases=["rt"])
async def roulette(ctx, color: str = None, cantidad: str = None):
    """Apuesta plata a rojo o negro en la ruleta (18/18 casillas, más 1 verde que hace perder a todos)"""
    global hubo_cambios_sin_guardar

    if color is None or cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!roulette rojo 500`, `!roulette negro 100k` o `!roulette rojo all`"))
        return

    color = color.lower()
    alias = {"rojo": "rojo", "r": "rojo", "red": "rojo", "negro": "negro", "n": "negro", "black": "negro"}
    if color not in alias:
        await ctx.reply(embed=crear_embed(descripcion="Elegí `rojo` o `negro`."))
        return
    color = alias[color]

    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="No entendí la apuesta. Ejemplo: `!roulette rojo 500`, `!roulette negro 100k` o `!roulette rojo all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["balance"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata para apostar." if balances_cache[user_id]["balance"] == 0 else "La apuesta tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés esa plata. Tu balance es **{formatear_pesos(balances_cache[user_id]['balance'])}**."))
        return

    # 18 casillas rojas, 18 negras, 1 verde (el 0, hace perder a todos por igual)
    resultado = random.choices(("rojo", "negro", "verde"), weights=(18, 18, 1), k=1)[0]
    gano = resultado == color

    # Amuleto del ludópata: si perdiste, un 5% de chance de que te "salve"
    buff_amuleto = obtener_buff(user_id, "suerte_apuesta")
    if not gano and buff_amuleto and random.random() < buff_amuleto["valor"]:
        resultado = color
        gano = True

    emoji_resultado = {"rojo": "🔴", "negro": "⚫", "verde": "🟢"}[resultado]

    if gano:
        balances_cache[user_id]["balance"] += cantidad
        descripcion = f"Salió {emoji_resultado} **{resultado}**. Ganaste **{formatear_pesos(cantidad)}**.\nBalance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
    else:
        balances_cache[user_id]["balance"] -= cantidad
        sumar_a_banca(cantidad)
        descripcion = f"Salió {emoji_resultado} **{resultado}**. Perdiste **{formatear_pesos(cantidad)}**.\nBalance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"

    hubo_cambios_sin_guardar = True
    await ctx.reply(embed=crear_embed(titulo="🎡 Ruleta", descripcion=descripcion))

@bot.command(name="addmoney")
async def addmoney(ctx, miembro: discord.Member = None, cantidad: int = None):
    """Le agrega plata a alguien. Solo vos podés usarlo."""
    global hubo_cambios_sin_guardar

    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return

    if miembro is None or cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!addmoney @persona 1000`"))
        return

    user_id = str(miembro.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": miembro.display_name, "balance": 0}

    balances_cache[user_id]["balance"] += cantidad
    hubo_cambios_sin_guardar = True

    embed = crear_embed(descripcion=f"Le di **{formatear_pesos(cantidad)}** a **{miembro.display_name}**.\nBalance nuevo: **{formatear_pesos(balances_cache[user_id]['balance'])}**")
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

        descripcion = f"Apuesta: **{formatear_pesos(self.apuesta)}**"
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
            texto = f"Ganaste **{formatear_pesos(self.apuesta)}**."
        elif resultado_tipo == "blackjack":
            ganancia = int(self.apuesta * 1.5)
            balances_cache[user_id]["balance"] += ganancia
            texto = f"¡Blackjack! Ganaste **{formatear_pesos(ganancia)}**."
        elif resultado_tipo == "pierde":
            balances_cache[user_id]["balance"] -= self.apuesta
            sumar_a_banca(self.apuesta)
            texto = f"Perdiste **{formatear_pesos(self.apuesta)}**."
        else:
            texto = "Empate, recuperás tu apuesta."

        hubo_cambios_sin_guardar = True
        texto += f"\nBalance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
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
async def blackjack(ctx, cantidad: str = None):
    """Jugá al blackjack apostando Pesos"""
    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}

    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!bj 500`, `!bj 100k` o `!bj all`"))
        return

    cantidad = parsear_cantidad(cantidad)
    if cantidad is None:
        await ctx.reply(embed=crear_embed(descripcion="No entendí la apuesta. Ejemplo: `!bj 500`, `!bj 100k` o `!bj all`"))
        return
    if cantidad == "all":
        cantidad = balances_cache[user_id]["balance"]
    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés plata para apostar." if balances_cache[user_id]["balance"] == 0 else "La apuesta tiene que ser mayor a 0."))
        return

    if balances_cache[user_id]["balance"] < cantidad:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés esa plata. Tu balance es **{formatear_pesos(balances_cache[user_id]['balance'])}**."))
        return

    view = BlackjackView(ctx.author, cantidad)

    # Regla de casino: el crupier revela su carta tapada ANTES de que el jugador
    # pueda pedir. Si tiene blackjack natural, la mano se resuelve al toque (con
    # o sin empate) sin mostrar botones, así el jugador no puede pedir cartas
    # "de más" contra un blackjack que ya estaba en la mesa.
    jugador_bj = valor_mano(view.mano_jugador) == 21
    crupier_bj = valor_mano(view.mano_crupier) == 21

    if jugador_bj or crupier_bj:
        if jugador_bj and crupier_bj:
            resultado_tipo = "empata"
        elif jugador_bj:
            resultado_tipo = "blackjack"
        else:
            resultado_tipo = "pierde"

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
    (5000, 24999, 45, "⚪ Común"),
    (25000, 74999, 30, "🟢 Poco común"),
    (75000, 199999, 12, "🔵 Raro"),
    (200000, 499999, 4, "🟣 Épico"),
    (500000, 999999, 3, "🟡 Legendario"),
    (1000000, 2500000, 1, "🔴 Mítico"),
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
    peso, nombre, _indice = indice_rareza(valor)
    return peso, nombre

def indice_rareza(valor):
    """Devuelve (peso, nombre_rareza, indice_en_RANGOS_RAREZA) según el valor del personaje."""
    numero = parsear_valor(valor)
    if numero is None:
        return RANGOS_RAREZA[0][2], RANGOS_RAREZA[0][3], 0
    for i, (minimo, maximo, peso, nombre) in enumerate(RANGOS_RAREZA):
        if minimo <= numero <= maximo:
            return peso, nombre, i
    return RANGOS_RAREZA[0][2], RANGOS_RAREZA[0][3], 0

def agrupar_personajes_por_rareza():
    """Agrupa personajes_cache según a qué rango de RANGOS_RAREZA pertenece cada uno.
    Devuelve una lista paralela a RANGOS_RAREZA: grupos[i] = lista de personajes en ese rango."""
    grupos = [[] for _ in RANGOS_RAREZA]
    for p in personajes_cache:
        _, _, i = indice_rareza(campo_personaje(p, "Valor", "valor"))
        grupos[i].append(p)
    return grupos

COOLDOWN_RECLAMO_SEGUNDOS = 3 * 60 * 60

class RWClaimView(discord.ui.View):
    """Botón de reclamo: 30s exclusivos para quien usó !rw, 60s más libres para
    cualquiera, y después de esos 90s totales se vence sin que nadie lo reclame.
    Además, cada usuario solo puede reclamar (no tirar) una vez cada 4hs."""

    SEGUNDOS_EXCLUSIVOS = 30
    SEGUNDOS_TOTALES = 90

    def __init__(self, autor, personaje, nombre, imagen, fuente, valor, rareza, admin=False):
        super().__init__(timeout=self.SEGUNDOS_TOTALES)
        self.admin = admin  # tirada de !rwAdmin: sin cooldown de reclamo
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

        # Chequeamos el duplicado ANTES de tocar el cooldown o gastar el Pase VIP:
        # si total el reclamo iba a fallar por esto, no tiene sentido consumir nada.
        if usuario_ya_tiene_personaje(interaction.user, self.personaje):
            await interaction.response.send_message(
                f"Ya tenés a **{self.nombre}** en tu colección, no podés repetirlo.",
                ephemeral=True,
            )
            return

        ultimo_reclamo = None if self.admin else ultimo_reclamo_por_usuario.get(str(interaction.user.id))
        if ultimo_reclamo is not None:
            transcurrido_reclamo = time.time() - ultimo_reclamo
            if transcurrido_reclamo < COOLDOWN_RECLAMO_SEGUNDOS:
                # ¿Tiene un Pase VIP guardado? Lo consume y salta el cooldown
                if not self.admin and obtener_efecto_unico(interaction.user.id, "pase_vip") > 0:
                    consumir_efecto_unico(interaction.user.id, "pase_vip")
                else:
                    restante_reclamo = COOLDOWN_RECLAMO_SEGUNDOS - transcurrido_reclamo
                    await interaction.response.send_message(
                        f"Ya reclamaste uno hace poco. Podés volver a reclamar en **{formatear_tiempo_restante(restante_reclamo)}**.",
                        ephemeral=True,
                    )
                    return

        self.reclamado_por = interaction.user
        agregar_personaje_a_coleccion(interaction.user, self.personaje)
        if not self.admin:
            ultimo_reclamo_por_usuario[str(interaction.user.id)] = time.time()

        button.disabled = True
        button.label = f"Reclamado por {interaction.user.display_name}"
        button.style = discord.ButtonStyle.secondary

        embed = interaction.message.embeds[0]
        embed.add_field(name="🔒 Reclamado por", value=interaction.user.display_name, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

        await ejecutar_autouse_silencioso(interaction.user, "despues_de_reclamar", interaction=interaction)

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

async def ejecutar_rw(ctx, admin=False):
    """Devuelve True si se hizo la tirada, False si no."""
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(
            descripcion="No hay personajes cargados todavía. Subí el `rw.json` al repo y corré `!rwreload`."
        ))
        return False

    # 1) Agrupamos los personajes por rango de rareza
    grupos = agrupar_personajes_por_rareza()

    # 2) Sorteamos la RAREZA (no el personaje) según el % de la tabla.
    #    Solo entran en el sorteo los rangos que tengan al menos 1 personaje cargado.
    indices_disponibles = [i for i, grupo in enumerate(grupos) if grupo]
    if not indices_disponibles:
        await ctx.reply(embed=crear_embed(descripcion="No hay personajes con un valor válido cargado."))
        return False
    pesos_disponibles = [RANGOS_RAREZA[i][2] for i in indices_disponibles]

    # Aplicar buff del Dado de la suerte: le saca un % del peso al Común
    # y lo reparte entre las rarezas altas (a mayor rareza, más se beneficia).
    buff_dado = None if admin else obtener_buff(ctx.author.id, "suerte_rw")
    if buff_dado and 0 in indices_disponibles:
        idx_comun_en_lista = indices_disponibles.index(0)
        peso_comun_original = pesos_disponibles[idx_comun_en_lista]
        peso_a_mover = peso_comun_original * buff_dado["valor"]
        pesos_disponibles[idx_comun_en_lista] -= peso_a_mover
        otros = [p for p in range(len(indices_disponibles)) if indices_disponibles[p] != 0]
        if otros:
            reparto = peso_a_mover / len(otros)
            for p in otros:
                pesos_disponibles[p] += reparto

    indice_rango = random.choices(indices_disponibles, weights=pesos_disponibles, k=1)[0]

    # 3) Dentro de esa rareza, elegimos un personaje al azar (todos con la misma chance)
    personaje = random.choice(grupos[indice_rango])
    rareza = RANGOS_RAREZA[indice_rango][3]

    nombre = campo_personaje(personaje, "Nombre", "nombre")
    imagen = campo_personaje(personaje, "Imagen", "imagen", default=None)
    fuente = campo_personaje(personaje, "Fuente", "fuente")
    valor = campo_personaje(personaje, "Valor", "valor")

    embed = crear_embed(titulo=f"🎴 {nombre}", footer=f"Tirado por {ctx.author.display_name}")
    embed.add_field(name="Fuente", value=str(fuente), inline=True)
    embed.add_field(name="Valor", value=formatear_pesos(valor), inline=True)
    embed.add_field(name="Rareza", value=rareza, inline=True)
    if buff_dado:
        embed.add_field(name="🎲 Dado activo", value=f"+{int(buff_dado['valor']*100)}% a rarezas altas", inline=False)
    if imagen:
        embed.set_image(url=imagen)

    view = RWClaimView(ctx.author, personaje, nombre, imagen, fuente, valor, rareza, admin=admin)
    mensaje = await ctx.reply(embed=embed, view=view)
    view.mensaje = mensaje
    return True

@bot.command(name="rw")
async def rw(ctx):
    """Sacá un personaje random del rw.json (los de más valor son más difíciles de sacar)"""
    user_id = str(ctx.author.id)
    restante = cooldown_restante(cooldown_rw_usuario, user_id)

    usando_cristal = False
    if restante > 0:
        # ¿Tiene un Cristal de reroll? Lo consume y tira igual, sin resetear el cooldown
        if obtener_efecto_unico(user_id, "cristal_reroll") > 0:
            consumir_efecto_unico(user_id, "cristal_reroll")
            usando_cristal = True
        else:
            await ctx.reply(embed=crear_embed(
                descripcion=(
                    f"⏳ Todavía no. Podés volver a tirar en **{formatear_tiempo_restante(restante)}**.\n"
                    f"-# Tips: un `Reloj de arena` te baja 3hs, una `Máquina del tiempo` te lo resetea, y un `Cristal de reroll` te deja tirar igual."
                )
            ))
            return

    exito = await ejecutar_rw(ctx)

    # Solo ponemos cooldown nuevo si la tirada se hizo Y no usamos cristal
    if exito and not usando_cristal:
        poner_cooldown(cooldown_rw_usuario, user_id, COOLDOWN_RW_SEGUNDOS)

    if exito:
        await ejecutar_autouse_silencioso(ctx.author, "despues_de_rw")

@bot.command(name="rwAdmin", aliases=["rwadmin"], hidden=True)
async def rw_admin(ctx):
    """Igual que !rw pero sin cooldown (ni de tirada ni de reclamo). Solo vos."""
    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return
    await ejecutar_rw(ctx, admin=True)

@bot.command(name="rwreload")
async def rwreload(ctx):
    """Recarga rw.json desde GitHub sin reiniciar el bot"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return
    try:
        await cargar_personajes_desde_github()
        grupos = agrupar_personajes_por_rareza()
        desglose = "\n".join(
            f"{RANGOS_RAREZA[i][3]}: **{len(grupos[i])}** personaje(s)"
            for i in range(len(RANGOS_RAREZA))
        )
        await ctx.reply(embed=crear_embed(
            descripcion=f"🔄 Listo, cargué **{len(personajes_cache)}** personajes desde GitHub.\n\n{desglose}"
        ))
    except Exception as e:
        await ctx.reply(embed=crear_embed(descripcion=f"❌ Hubo un error al recargar: {str(e)}"))

def _tag_booru(texto):
    return re.sub(r"[^a-z0-9\s\-']", "", texto.strip().lower()).replace(" ", "_")

async def _consultar_safebooru(tags, cantidad):
    if not tags:
        return []
    url = "https://safebooru.org/index.php"
    params = {"page": "dapi", "s": "post", "q": "index", "json": "1", "limit": str(cantidad), "tags": tags}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[buscar_imagenes_booru] safebooru respondió {resp.status} para '{tags}'", flush=True)
                    return []
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return []
    except Exception as e:
        print(f"[buscar_imagenes_booru] Error buscando '{tags}': {e}", flush=True)
        return []

    if not isinstance(data, list):
        return []

    resultado = []
    for post in data:
        file_url = post.get("file_url")
        if not file_url and post.get("directory") and post.get("image"):
            file_url = f"https://safebooru.org//images/{post['directory']}/{post['image']}"
        if file_url:
            resultado.append(file_url)
        if len(resultado) >= cantidad:
            break
    return resultado

async def buscar_imagenes_booru(nombre, fuente=None, cantidad=3):
    """Busca en Safebooru (versión SFW de Danbooru) por el tag del personaje.
    Primero prueba nombre+fuente combinados (ej: "gojo_satoru jujutsu_kaisen"),
    que es mucho más preciso, porque muchos nombres de personajes se repiten
    entre series distintas y buscar solo por nombre trae al personaje equivocado.
    Si esa combinación no da nada, cae a buscar solo por el nombre."""
    tag_nombre = _tag_booru(nombre)
    if not tag_nombre:
        return []

    if fuente:
        tag_fuente = _tag_booru(str(fuente))
        if tag_fuente:
            combinado = await _consultar_safebooru(f"{tag_nombre} {tag_fuente}", cantidad)
            if combinado:
                return combinado

    resultado = await _consultar_safebooru(tag_nombre, cantidad)
    if not resultado:
        print(f"[buscar_imagenes_booru] 0 resultados para el tag '{tag_nombre}'", flush=True)
    return resultado

async def buscar_imagenes_jikan(nombre, cantidad=3):
    """Busca el personaje en Jikan (API no oficial de MyAnimeList) y devuelve
    hasta `cantidad` imágenes oficiales de fichas de personaje que matchean el
    nombre. A diferencia de scrapear un buscador de imágenes genérico, acá no
    hay ambigüedad: es la base de datos de MAL buscando por nombre de
    personaje, así que el resultado es mucho más confiable para anime/manga.
    No sirve para personajes de videojuegos u otras fuentes que MAL no
    catalogue — para eso están los fallbacks de Safebooru/Bing."""
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {"q": nombre, "limit": min(max(cantidad * 3, 5), 10)}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.jikan.moe/v4/characters", params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[buscar_imagenes_jikan] Jikan respondió {resp.status} para '{nombre}'", flush=True)
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        print(f"[buscar_imagenes_jikan] Error de conexión buscando '{nombre}': {e}", flush=True)
        return []

    items = data.get("data", []) if isinstance(data, dict) else []
    if not items:
        print(f"[buscar_imagenes_jikan] 0 resultados para '{nombre}'", flush=True)
        return []

    nombre_norm = nombre.strip().lower()

    def similitud(item):
        nombre_item = str(item.get("name", "")).strip().lower()
        return difflib.SequenceMatcher(None, nombre_norm, nombre_item).ratio()

    # Ordenamos por qué tan parecido es el nombre devuelto al que buscamos,
    # porque Jikan hace búsqueda difusa y puede traer personajes de nombre
    # parecido pero distinto.
    items_ordenados = sorted(items, key=similitud, reverse=True)

    resultado = []
    for item in items_ordenados:
        # Si el nombre no se parece casi nada, mejor no arriesgar a traer un
        # personaje completamente distinto solo porque Jikan lo sugirió.
        if similitud(item) < 0.4:
            continue
        imagen = ((item.get("images") or {}).get("jpg") or {}).get("image_url")
        if imagen and imagen not in resultado:
            resultado.append(imagen)
        if len(resultado) >= cantidad:
            break
    return resultado

async def buscar_imagenes_serper(query, cantidad=3):
    """Busca en Serper.dev (API real de Google Imágenes vía JSON, no scraping).
    Esta es la fuente principal: a diferencia de Jikan (solo cubre anime/manga)
    o de scrapear buscadores a mano (Safebooru con tags puede devolver algo
    "válido" pero totalmente ajeno al personaje; Google/Bing/DuckDuckGo desde
    un servidor como Render suelen chocar con antibot y tirar contenido
    genérico/publicidad en vez de resultados reales), esto te da exactamente
    lo que buscarías vos a mano en Google Imágenes. Requiere SERPER_API_KEY;
    si no está configurada, devuelve vacío y el llamador cae a los fallbacks."""
    if not SERPER_API_KEY:
        return []

    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    body = {"q": query, "num": cantidad}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://google.serper.dev/images", headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    texto_error = await resp.text()
                    print(f"[buscar_imagenes_serper] Serper respondió {resp.status} para '{query}': {texto_error}", flush=True)
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        print(f"[buscar_imagenes_serper] Error de conexión buscando '{query}': {e}", flush=True)
        return []

    items = data.get("images", []) if isinstance(data, dict) else []
    if not items:
        print(f"[buscar_imagenes_serper] 0 resultados para '{query}'", flush=True)

    resultado = []
    for item in items:
        url_img = item.get("imageUrl")
        if url_img and url_img not in resultado:
            resultado.append(url_img)
        if len(resultado) >= cantidad:
            break
    return resultado

async def buscar_candidatas_imagen(nombre, fuente, cantidad=3):
    """Serper (Google Imágenes real, vía API) es la fuente principal: es la
    única que devuelve resultados genuinamente relevantes en vez de "algo, lo
    que sea". Si no hay SERPER_API_KEY configurada todavía, o esa consulta
    puntual no trae nada, cae a la cadena vieja (Jikan → Safebooru → Bing)
    para que el bot siga funcionando mientras tanto."""
    candidatas = await buscar_imagenes_serper(f"{nombre} {fuente}", cantidad=cantidad)
    if candidatas:
        return candidatas
    candidatas = await buscar_imagenes_jikan(nombre, cantidad=cantidad)
    if candidatas:
        return candidatas
    candidatas = await buscar_imagenes_booru(nombre, fuente=fuente, cantidad=cantidad)
    if candidatas:
        return candidatas
    return await buscar_imagenes_bing(f"{nombre} {fuente}", cantidad=cantidad)

async def buscar_imagenes_bing(query, cantidad=3):
    """Scrapea Bing Imágenes y devuelve hasta `cantidad` links directos de imagen.
    A diferencia de DuckDuckGo, no necesita conseguir un token aparte (eso era
    lo que fallaba en silencio), y a diferencia de Google, no exige aceptar
    cookies para mostrar resultados. Sigue siendo scraping no oficial: si Bing
    cambia el HTML esto puede dejar de andar, pero hoy es la opción más estable
    de las tres que probamos."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
    }
    params = {"q": query, "form": "HDRSC2", "first": "1"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://www.bing.com/images/search", params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[buscar_imagenes_bing] Bing respondió {resp.status} para '{query}'", flush=True)
                    return []
                html = await resp.text()
    except Exception as e:
        print(f"[buscar_imagenes_bing] Error de conexión buscando '{query}': {e}", flush=True)
        return []

    # Bing mete la metadata de cada resultado (incluida "murl", la imagen
    # original) en un atributo m="{...}" con el JSON escapado como entidades
    # HTML (&quot; en vez de "), por eso el regex busca ese patrón puntual.
    encontrados = re.findall(r'murl&quot;:&quot;(https?://[^&]+?)&quot;', html)
    if not encontrados:
        # A veces Bing no escapa el atributo; probamos también la variante cruda.
        encontrados = re.findall(r'"murl":"(https?://[^"]+?)"', html.replace("\\/", "/"))

    if not encontrados:
        print(f"[buscar_imagenes_bing] 0 resultados para '{query}' (largo HTML: {len(html)})", flush=True)

    vistos = set()
    resultado = []
    for url_img in encontrados:
        if url_img not in vistos:
            vistos.add(url_img)
            resultado.append(url_img)
        if len(resultado) >= cantidad:
            break
    return resultado

async def _descargar_imagen_bytes(session, url):
    """Baja una imagen como bytes crudos. Devuelve None si falla (link roto,
    timeout, lo que sea) — el collage la muestra como "Sin imagen" en vez de
    romperse."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12), headers=_HEADERS_NAVEGADOR) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception:
        return None

def _armar_collage(imagenes_bytes):
    """Arma un collage horizontal con hasta 3 imágenes, numeradas 1/2/3 arriba
    de cada una, todas a la misma altura. Devuelve un BytesIO con el PNG, o
    None si ninguna de las 3 se pudo abrir."""
    ALTO = 350
    SEPARACION = 10
    ANCHO_PLACEHOLDER = 180

    abiertas = []
    for b in imagenes_bytes:
        if b is None:
            abiertas.append(None)
            continue
        try:
            img = Image.open(io.BytesIO(b)).convert("RGB")
            ratio = ALTO / img.height
            img = img.resize((max(1, int(img.width * ratio)), ALTO))
            abiertas.append(img)
        except Exception:
            abiertas.append(None)

    if not any(img is not None for img in abiertas):
        return None

    ancho_total = sum((img.width if img else ANCHO_PLACEHOLDER) for img in abiertas) + SEPARACION * (len(abiertas) - 1)
    collage = Image.new("RGB", (ancho_total, ALTO + 36), (32, 32, 36))
    draw = ImageDraw.Draw(collage)

    x = 0
    for i, img in enumerate(abiertas, start=1):
        if img is None:
            draw.rectangle([x, 36, x + ANCHO_PLACEHOLDER, 36 + ALTO], outline=(120, 120, 120), width=2)
            draw.text((x + ANCHO_PLACEHOLDER // 2 - 30, 36 + ALTO // 2), "Sin imagen", fill=(200, 200, 200))
            ancho = ANCHO_PLACEHOLDER
        else:
            collage.paste(img, (x, 36))
            ancho = img.width
        draw.text((x + ancho // 2 - 4, 8), str(i), fill=(255, 255, 255))
        x += ancho + SEPARACION

    buffer = io.BytesIO()
    collage.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

class ComparacionImagenesView(discord.ui.View):
    """Como BusquedaImagenesView, pero en vez de mostrar las candidatas una
    por una, arma un collage con las 3 juntas y numeradas para elegir de un
    solo vistazo (pensado para personajes que no conocés: comparando las 3
    confirmás que son el mismo antes de guardar)."""

    def __init__(self, autor, pendientes):
        super().__init__(timeout=600)
        self.autor = autor
        self.pendientes = pendientes
        self.indice_personaje = 0
        self.candidatos = []
        self.guardados = 0
        self.saltados = 0
        self.terminado = False
        self.procesando = False
        self.mensaje = None

    async def cargar_siguiente_pendiente(self):
        """Devuelve (embed, archivo_o_None) del siguiente personaje con candidatas,
        o el embed final (sin archivo) si ya no quedan."""
        while self.indice_personaje < len(self.pendientes):
            personaje = self.pendientes[self.indice_personaje]
            nombre = campo_personaje(personaje, "Nombre", "nombre")
            fuente = campo_personaje(personaje, "Fuente", "fuente")
            self.candidatos = await buscar_candidatas_imagen(nombre, fuente, cantidad=3)
            if self.candidatos:
                return await self.armar_mensaje_actual()
            self.saltados += 1
            self.indice_personaje += 1
        return await self.finalizar(), None

    async def armar_mensaje_actual(self):
        personaje = self.pendientes[self.indice_personaje]
        nombre = campo_personaje(personaje, "Nombre", "nombre")
        fuente = campo_personaje(personaje, "Fuente", "fuente")

        async with aiohttp.ClientSession() as session:
            bytes_imagenes = await asyncio.gather(*[_descargar_imagen_bytes(session, u) for u in self.candidatos])
        buffer = _armar_collage(bytes_imagenes)

        for i, boton in enumerate((self.opcion1, self.opcion2, self.opcion3), start=1):
            boton.disabled = i > len(self.candidatos)

        embed = crear_embed(
            titulo=nombre,
            footer=f"Personaje {self.indice_personaje + 1}/{len(self.pendientes)}",
        )
        embed.add_field(name="Fuente", value=str(fuente), inline=False)

        if buffer is not None:
            archivo = discord.File(buffer, filename="collage.png")
            embed.set_image(url="attachment://collage.png")
            return embed, archivo

        embed.description = "Ninguna de las 3 candidatas se pudo abrir como imagen. Probá `🔄 Recargar`."
        return embed, None

    async def finalizar(self):
        global hubo_cambios_personajes_sin_guardar
        self.terminado = True
        for item in self.children:
            item.disabled = True

        texto = f"Guardé imagen a **{self.guardados}** personaje(s). Salteé **{self.saltados}** (sin resultados o ninguna te sirvió)."
        if hubo_cambios_personajes_sin_guardar:
            try:
                await guardar_personajes_en_github()
                texto += "\n💾 Ya quedó subido a `rw.json` en GitHub."
            except Exception as e:
                texto += f"\n❌ Pero hubo un error al subirlo a GitHub: {e}"
        else:
            texto += "\n(No guardaste ninguna, así que no hizo falta tocar GitHub.)"

        return crear_embed(titulo="🖼️ Comparación de imágenes terminada", descripcion=texto)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Solo quien inició esto puede decidir.", ephemeral=True)
            return False
        if self.procesando:
            await interaction.response.send_message("Esperá, todavía estoy armando el collage anterior.", ephemeral=True)
            return False
        return True

    async def _actualizar_mensaje(self, interaction, resultado):
        embed, archivo = resultado
        if archivo is not None:
            await interaction.edit_original_response(embed=embed, attachments=[archivo], view=self)
        else:
            await interaction.edit_original_response(embed=embed, attachments=[], view=self)

    async def _elegir(self, interaction, indice):
        global hubo_cambios_personajes_sin_guardar
        await interaction.response.defer()
        self.procesando = True
        try:
            personaje = self.pendientes[self.indice_personaje]
            personaje["Imagen"] = self.candidatos[indice]
            hubo_cambios_personajes_sin_guardar = True
            self.guardados += 1
            self.indice_personaje += 1
            resultado = await self.cargar_siguiente_pendiente()
        finally:
            self.procesando = False
        await self._actualizar_mensaje(interaction, resultado)

    @discord.ui.button(label="1️⃣", style=discord.ButtonStyle.success, row=0)
    async def opcion1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._elegir(interaction, 0)

    @discord.ui.button(label="2️⃣", style=discord.ButtonStyle.success, row=0)
    async def opcion2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._elegir(interaction, 1)

    @discord.ui.button(label="3️⃣", style=discord.ButtonStyle.success, row=0)
    async def opcion3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._elegir(interaction, 2)

    @discord.ui.button(label="❌ Ninguna sirve", style=discord.ButtonStyle.danger, row=1)
    async def ninguna(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.procesando = True
        try:
            self.saltados += 1
            self.indice_personaje += 1
            resultado = await self.cargar_siguiente_pendiente()
        finally:
            self.procesando = False
        await self._actualizar_mensaje(interaction, resultado)

    @discord.ui.button(label="🔄 Recargar", style=discord.ButtonStyle.secondary, row=1)
    async def recargar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.procesando = True
        try:
            personaje = self.pendientes[self.indice_personaje]
            nombre = campo_personaje(personaje, "Nombre", "nombre")
            fuente = campo_personaje(personaje, "Fuente", "fuente")
            nuevas = await buscar_candidatas_imagen(nombre, fuente, cantidad=3)
            if nuevas:
                self.candidatos = nuevas
            resultado = await self.armar_mensaje_actual()
        finally:
            self.procesando = False
        await self._actualizar_mensaje(interaction, resultado)

    async def on_timeout(self):
        if self.terminado or self.mensaje is None:
            return
        embed = await self.finalizar()
        try:
            await self.mensaje.edit(embed=embed, view=self, attachments=[])
        except discord.HTTPException:
            pass

@bot.command(name="imgcompare", aliases=["comparaimg"], hidden=True)
async def imgcompare(ctx, cantidad: int = None):
    """Como !buscarimagenes, pero arma un collage con las 3 candidatas juntas
    y numeradas para elegir de un vistazo, en vez de ir pasando una por una.
    Solo vos."""
    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(descripcion="No hay personajes cargados. Corré `!rwreload` primero."))
        return

    pendientes = [p for p in personajes_cache if not str(campo_personaje(p, "Imagen", "imagen", default="")).strip()]
    if not pendientes:
        await ctx.reply(embed=crear_embed(descripcion="Ningún personaje de `rw.json` está sin imagen. 🎉"))
        return

    if cantidad is not None and cantidad > 0:
        pendientes = pendientes[:cantidad]

    aviso = await ctx.reply(embed=crear_embed(
        descripcion=f"🔎 Armando collages para **{len(pendientes)}** personaje(s) sin foto. Puede tardar un toque por cada uno..."
    ))

    vista = ComparacionImagenesView(ctx.author, pendientes)
    embed_inicial, archivo_inicial = await vista.cargar_siguiente_pendiente()
    if archivo_inicial is not None:
        await aviso.edit(embed=embed_inicial, attachments=[archivo_inicial], view=vista)
    else:
        await aviso.edit(embed=embed_inicial, attachments=[], view=vista)
    vista.mensaje = aviso

class BusquedaImagenesView(discord.ui.View):
    """Recorre los personajes sin Imagen en rw.json, busca 2 candidatas por
    Google Imágenes para cada uno, y deja que vos elijas Guardar/Rechazar.
    rw.json recién se sube a GitHub una sola vez, al terminar todo."""

    def __init__(self, autor, pendientes):
        super().__init__(timeout=600)
        self.autor = autor
        self.pendientes = pendientes  # referencias directas a dicts de personajes_cache
        self.indice_personaje = 0
        self.candidatos = []
        self.indice_candidato = 0
        self.guardados = 0
        self.saltados = 0
        self.terminado = False
        self.procesando = False
        self.mensaje = None

    async def cargar_siguiente_pendiente(self):
        while self.indice_personaje < len(self.pendientes):
            personaje = self.pendientes[self.indice_personaje]
            nombre = campo_personaje(personaje, "Nombre", "nombre")
            fuente = campo_personaje(personaje, "Fuente", "fuente")
            self.candidatos = await buscar_candidatas_imagen(nombre, fuente, cantidad=3)
            self.indice_candidato = 0
            if self.candidatos:
                return self.embed_candidato_actual()
            self.saltados += 1
            self.indice_personaje += 1
        return await self.finalizar()

    def embed_candidato_actual(self):
        personaje = self.pendientes[self.indice_personaje]
        nombre = campo_personaje(personaje, "Nombre", "nombre")
        fuente = campo_personaje(personaje, "Fuente", "fuente")
        embed = crear_embed(
            titulo=nombre,
            footer=f"Personaje {self.indice_personaje + 1}/{len(self.pendientes)} — opción {self.indice_candidato + 1}/{len(self.candidatos)}",
        )
        embed.add_field(name="Fuente", value=str(fuente), inline=False)
        embed.set_image(url=self.candidatos[self.indice_candidato])
        return embed

    async def finalizar(self):
        global hubo_cambios_personajes_sin_guardar
        self.terminado = True
        for item in self.children:
            item.disabled = True

        texto = f"Guardé imagen a **{self.guardados}** personaje(s). Salteé **{self.saltados}** por falta de resultados."
        if hubo_cambios_personajes_sin_guardar:
            try:
                await guardar_personajes_en_github()
                texto += "\n💾 Ya quedó subido a `rw.json` en GitHub."
            except Exception as e:
                texto += f"\n❌ Pero hubo un error al subirlo a GitHub: {e}"
        else:
            texto += "\n(No guardaste ninguna, así que no hizo falta tocar GitHub.)"

        return crear_embed(titulo="🖼️ Búsqueda de imágenes terminada", descripcion=texto)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Solo quien inició la búsqueda puede decidir esto.", ephemeral=True)
            return False
        if self.procesando:
            await interaction.response.send_message("Esperá, todavía estoy buscando la imagen anterior.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Guardar", style=discord.ButtonStyle.success)
    async def guardar(self, interaction: discord.Interaction, button: discord.ui.Button):
        global hubo_cambios_personajes_sin_guardar
        await interaction.response.defer()
        self.procesando = True
        try:
            personaje = self.pendientes[self.indice_personaje]
            personaje["Imagen"] = self.candidatos[self.indice_candidato]
            hubo_cambios_personajes_sin_guardar = True
            self.guardados += 1
            self.indice_personaje += 1
            embed = await self.cargar_siguiente_pendiente()
        finally:
            self.procesando = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="❌ Rechazar", style=discord.ButtonStyle.danger)
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.procesando = True
        try:
            self.indice_candidato += 1
            if self.indice_candidato >= len(self.candidatos):
                self.saltados += 1
                self.indice_personaje += 1
                embed = await self.cargar_siguiente_pendiente()
            else:
                embed = self.embed_candidato_actual()
        finally:
            self.procesando = False
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🔄 Recargar", style=discord.ButtonStyle.secondary)
    async def recargar(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Vuelve a buscar candidatas frescas para el personaje actual, en vez
        de solo re-mostrar el mismo link (que puede ser el que ya murió/no cargó)."""
        await interaction.response.defer()
        self.procesando = True
        try:
            personaje = self.pendientes[self.indice_personaje]
            nombre = campo_personaje(personaje, "Nombre", "nombre")
            fuente = campo_personaje(personaje, "Fuente", "fuente")
            nuevas = await buscar_candidatas_imagen(nombre, fuente, cantidad=3)
            if nuevas:
                self.candidatos = nuevas
                self.indice_candidato = 0
            embed = self.embed_candidato_actual()
        finally:
            self.procesando = False
        await interaction.edit_original_response(embed=embed, view=self)

    async def on_timeout(self):
        if self.terminado or self.mensaje is None:
            return
        embed = await self.finalizar()
        try:
            await self.mensaje.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

@bot.command(name="buscarimagenes", aliases=["imgsearch"])
async def buscarimagenes(ctx, cantidad: int = None):
    """Busca una foto (Safebooru + DuckDuckGo) para los personajes de rw.json que no tienen (Guardar/Rechazar/Recargar, sube a GitHub al final)"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(descripcion="No hay personajes cargados. Corré `!rwreload` primero."))
        return

    pendientes = [p for p in personajes_cache if not str(campo_personaje(p, "Imagen", "imagen", default="")).strip()]
    if not pendientes:
        await ctx.reply(embed=crear_embed(descripcion="Ningún personaje de `rw.json` está sin imagen. 🎉"))
        return

    if cantidad is not None and cantidad > 0:
        pendientes = pendientes[:cantidad]

    aviso = await ctx.reply(embed=crear_embed(
        descripcion=f"🔎 Buscando imágenes para **{len(pendientes)}** personaje(s) sin foto. Puede tardar un toque por cada uno..."
    ))

    vista = BusquedaImagenesView(ctx.author, pendientes)
    embed_inicial = await vista.cargar_siguiente_pendiente()
    await aviso.edit(embed=embed_inicial, view=vista)
    vista.mensaje = aviso

class GaleriaView(discord.ui.View):
    """Recorre personajes_cache de a uno: solo nombre + imagen, para poder
    revisar rápido cuáles tienen la foto rota o mal puesta."""

    def __init__(self, autor, personajes):
        super().__init__(timeout=600)
        self.autor = autor
        self.personajes = personajes
        self.indice = 0
        self.mensaje = None
        self._actualizar_botones()

    def _actualizar_botones(self):
        self.anterior.disabled = self.indice == 0
        self.siguiente.disabled = self.indice >= len(self.personajes) - 1

    def construir_embed(self):
        personaje = self.personajes[self.indice]
        nombre = campo_personaje(personaje, "Nombre", "nombre")
        imagen = campo_personaje(personaje, "Imagen", "imagen", default=None)

        embed = crear_embed(
            titulo=nombre,
            footer=f"{self.indice + 1}/{len(self.personajes)}",
        )
        if imagen:
            embed.set_image(url=imagen)
        else:
            embed.description = "⚠️ No tiene imagen cargada."
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Solo quien pidió la galería puede pasar de página.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.indice -= 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    @discord.ui.button(label="🔢 Ir a...", style=discord.ButtonStyle.primary)
    async def ir_a_pagina(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GaleriaIrAModal(self))

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.indice += 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    async def on_timeout(self):
        if self.mensaje is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.mensaje.edit(view=self)
        except discord.HTTPException:
            pass

class GaleriaIrAModal(discord.ui.Modal):
    def __init__(self, vista: GaleriaView):
        super().__init__(title="Ir a un personaje")
        self.vista = vista
        self.numero = discord.ui.TextInput(
            label=f"Número (1-{len(vista.personajes)})",
            placeholder="Ej: 64",
            required=True,
            max_length=10,
        )
        self.add_item(self.numero)

    async def on_submit(self, interaction: discord.Interaction):
        total = len(self.vista.personajes)
        try:
            numero = int(self.numero.value.strip())
        except ValueError:
            await interaction.response.send_message("Eso no es un número.", ephemeral=True)
            return
        if not (1 <= numero <= total):
            await interaction.response.send_message(f"Tiene que ser un número entre 1 y {total}.", ephemeral=True)
            return

        self.vista.indice = numero - 1
        self.vista._actualizar_botones()
        await interaction.response.edit_message(embed=self.vista.construir_embed(), view=self.vista)

@bot.command(name="galeria")
async def galeria(ctx):
    """Recorre uno por uno todos los personajes del rw.json: nombre + imagen, con Previous/Next"""
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(descripcion="No hay personajes cargados. Probá `!rwreload` primero."))
        return

    view = GaleriaView(ctx.author, personajes_cache)
    mensaje = await ctx.reply(embed=view.construir_embed(), view=view)
    view.mensaje = mensaje

@bot.command(name="winfo")
async def winfo(ctx, *, nombre_buscado: str = None):
    """Muestra la ficha de un personaje puntual del rw.json, sin botón de reclamar"""
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(
            descripcion="No hay personajes cargados todavía. Subí el `rw.json` al repo y corré `!rwreload`."
        ))
        return

    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!winfo Gojo Satoru`"))
        return

    buscado = nombre_buscado.lower().strip()
    encontrados = [p for p in personajes_cache if buscado in campo_personaje(p, "Nombre", "nombre").lower()]

    if not encontrados:
        await ctx.reply(embed=crear_embed(descripcion=f"No encontré ningún personaje que coincida con **{nombre_buscado}**."))
        return

    exacto = next((p for p in encontrados if campo_personaje(p, "Nombre", "nombre").lower() == buscado), None)
    personaje = exacto or encontrados[0]

    nombre = campo_personaje(personaje, "Nombre", "nombre")
    imagen = campo_personaje(personaje, "Imagen", "imagen", default=None)
    fuente = campo_personaje(personaje, "Fuente", "fuente")
    valor = campo_personaje(personaje, "Valor", "valor")
    _, rareza = info_rareza(valor)

    embed = crear_embed(titulo=f"🔎 {nombre}")
    embed.add_field(name="Fuente", value=str(fuente), inline=True)
    embed.add_field(name="Valor", value=formatear_pesos(valor), inline=True)
    embed.add_field(name="Rareza", value=rareza, inline=True)
    if imagen:
        embed.set_image(url=imagen)

    if not exacto and len(encontrados) > 1:
        embed.set_footer(text=f"Coincidencia parcial. Hay {len(encontrados) - 1} más con nombres similares.")

    await ctx.reply(embed=embed)

class ShopView(discord.ui.View):
    """Un embed por categoría, con Previous/Next para pasar de categoría."""
    def __init__(self, autor, categorias):
        super().__init__(timeout=120)
        self.autor = autor
        self.categorias = categorias  # lista de (nombre_categoria, [items])
        self.pagina = 0
        self.mensaje = None
        self._actualizar_botones()

    def _actualizar_botones(self):
        self.anterior.disabled = self.pagina == 0
        self.siguiente.disabled = self.pagina >= len(self.categorias) - 1

    def construir_embed(self):
        nombre_categoria, items = self.categorias[self.pagina]

        if items:
            bloques = []
            for item in items:
                bloque = f"{item['emoji']} {item['nombre']} — {formatear_pesos(item['precio'])}"
                desc = item.get("descripcion")
                if desc:
                    if len(desc) > 90:
                        desc = desc[:87] + "..."
                    bloque += f"\n└ {desc}"
                bloques.append(bloque)
            descripcion = f"### {nombre_categoria}\n{SEPARADOR}\n\n" + "\n\n".join(bloques)
        else:
            descripcion = f"### {nombre_categoria}\n{SEPARADOR}\n\n-# No hay ítems acá todavía."

        embed = crear_embed(
            descripcion=descripcion,
            footer=f"Página {self.pagina + 1}/{len(self.categorias)} — usá !buy \"nombre\" para comprar y !usar \"nombre\" para usar",
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Solo quien pidió la tienda puede cambiar de página.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina -= 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    async def on_timeout(self):
        if self.mensaje is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.mensaje.edit(view=self)
        except discord.HTTPException:
            pass

@bot.command(name="shop", aliases=["tienda"])
async def shop(ctx):
    """Muestra el menú de la tienda, por categorías"""
    if not shop_cache:
        await ctx.reply(embed=crear_embed(descripcion="La tienda está vacía. Subí `data/shop.json` al repo y corré `!shopreload`."))
        return

    categorias = list(shop_cache.items())
    view = ShopView(ctx.author, categorias)
    mensaje = await ctx.reply(embed=view.construir_embed(), view=view)
    view.mensaje = mensaje

@bot.command(name="shopreload")
async def shopreload(ctx):
    """Recarga shop.json desde GitHub sin reiniciar el bot"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar GITHUB_TOKEN y GITHUB_REPO en Render."))
        return
    try:
        await cargar_shop_desde_github()
        total = len(aplanar_shop())
        await ctx.reply(embed=crear_embed(
            descripcion=f"🔄 Listo, cargué **{total}** ítems en **{len(shop_cache)}** categorías desde GitHub."
        ))
    except Exception as e:
        await ctx.reply(embed=crear_embed(descripcion=f"❌ Hubo un error al recargar: {str(e)}"))

@bot.command(name="buy", aliases=["comprar"])
async def buy(ctx, *, entrada: str = None):
    """Comprá un ítem de la tienda. Usalo así: !buy nombre del ítem [cantidad]"""
    global hubo_cambios_sin_guardar

    if not shop_cache:
        await ctx.reply(embed=crear_embed(descripcion="La tienda está vacía. Subí `data/shop.json` al repo y corré `!shopreload`."))
        return

    if not entrada:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!buy Peluche de Ado`, `!buy Peluche de Ado 3` o `!buy Dólar 10k`"))
        return

    # Acepta cantidad con sufijo (100k, 2m, all, etc) usando el mismo parser que el resto del bot.
    # Si el último "token" del mensaje no es una cantidad válida, se asume que todo
    # el texto es el nombre del ítem (y la cantidad = 1).
    partes = entrada.strip().rsplit(" ", 1)
    cantidad = 1
    nombre_buscado = entrada.strip()
    if len(partes) == 2:
        cant_parseada = parsear_cantidad(partes[1])
        if cant_parseada is not None:
            nombre_buscado = partes[0].strip()
            cantidad = cant_parseada

    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!buy Peluche de Ado`, `!buy Peluche de Ado 3` o `!buy Dólar 10k`"))
        return

    categoria, item = buscar_item_shop(nombre_buscado)
    if item is None:
        await ctx.reply(embed=crear_embed(descripcion=f"No encontré ningún ítem que se llame **{nombre_buscado}** en la tienda. Mirá `!shop`."))
        return

    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0}
    balances_cache[user_id].setdefault("banco", 0)

    # "all": comprar todo lo que el balance alcance
    if cantidad == "all":
        cantidad = balances_cache[user_id]["balance"] // item["precio"]
        if cantidad <= 0:
            await ctx.reply(embed=crear_embed(
                descripcion=f"No te alcanza ni para uno. **{item['emoji']} {item['nombre']}** cuesta **{formatear_pesos(item['precio'])}** "
                            f"y tenés **{formatear_pesos(balances_cache[user_id]['balance'])}**."
            ))
            return

    if cantidad <= 0:
        await ctx.reply(embed=crear_embed(descripcion="La cantidad tiene que ser mayor a 0."))
        return

    costo_total = item["precio"] * cantidad
    if balances_cache[user_id]["balance"] < costo_total:
        await ctx.reply(embed=crear_embed(
            descripcion=f"No te alcanza. **{item['emoji']} {item['nombre']}** x{cantidad} cuesta **{formatear_pesos(costo_total)}**, "
                        f"y tenés **{formatear_pesos(balances_cache[user_id]['balance'])}**."
        ))
        return

    balances_cache[user_id]["balance"] -= costo_total
    hubo_cambios_sin_guardar = True
    agregar_item_a_inventario(ctx.author, item["nombre"], cantidad)

    await ctx.reply(embed=crear_embed(
        titulo="🛍️ Compra realizada",
        descripcion=(
            f"Compraste **{item['emoji']} {item['nombre']}** x{cantidad} por **{formatear_pesos(costo_total)}**.\n"
            f"Balance actual: **{formatear_pesos(balances_cache[user_id]['balance'])}**"
        ),
    ))

ITEMS_POR_PAGINA = 10

class InventarioIrAModal(discord.ui.Modal):
    def __init__(self, vista):
        super().__init__(title="Ir a una página")
        self.vista = vista
        self.numero = discord.ui.TextInput(
            label=f"Página (1-{vista.total_paginas})",
            placeholder="Ej: 3",
            required=True,
            max_length=10,
        )
        self.add_item(self.numero)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            numero = int(self.numero.value.strip())
        except ValueError:
            await interaction.response.send_message("Eso no es un número.", ephemeral=True)
            return
        if not (1 <= numero <= self.vista.total_paginas):
            await interaction.response.send_message(f"Tiene que ser un número entre 1 y {self.vista.total_paginas}.", ephemeral=True)
            return

        self.vista.pagina = numero - 1
        self.vista._actualizar_botones()
        await interaction.response.edit_message(embed=self.vista.construir_embed(), view=self.vista)

class InventarioView(discord.ui.View):
    """Muestra el inventario paginado: 10 tipos de item por página, con
    Previous/Ir a/Next. Solo el autor puede mover las páginas."""

    def __init__(self, autor, miembro, items):
        super().__init__(timeout=120)
        self.autor = autor
        self.miembro = miembro
        # Ordenamos por cantidad descendente (los que más tenés primero)
        self.items = sorted(items.items(), key=lambda kv: kv[1], reverse=True)
        self.pagina = 0
        self.total_paginas = max(1, -(-len(self.items) // ITEMS_POR_PAGINA))
        self.mensaje = None
        self._actualizar_botones()

    def _actualizar_botones(self):
        self.anterior.disabled = self.pagina == 0
        self.siguiente.disabled = self.pagina >= self.total_paginas - 1

    def construir_embed(self):
        inicio = self.pagina * ITEMS_POR_PAGINA
        fin = inicio + ITEMS_POR_PAGINA
        items_pagina = self.items[inicio:fin]

        lineas = []
        for nombre_item, cantidad in items_pagina:
            _, item_shop = buscar_item_shop(nombre_item)
            emoji = item_shop["emoji"] if item_shop else "📦"
            lineas.append(f"> {emoji} **{nombre_item}** — x{cantidad}")

        if lineas:
            descripcion = (
                f"### 🎒 Inventario de {self.miembro.display_name}\n"
                f"{SEPARADOR}\n\n"
                + "\n\n".join(lineas)
            )
        else:
            descripcion = (
                f"### 🎒 Inventario de {self.miembro.display_name}\n"
                f"{SEPARADOR}\n\n"
                f"-# No hay items en esta página."
            )

        total_items = sum(c for _, c in self.items)
        return crear_embed(
            descripcion=descripcion,
            footer=f"Página {self.pagina + 1}/{self.total_paginas} — {len(self.items)} tipo(s) de item, {total_items} objeto(s) en total",
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Solo quien pidió el inventario puede cambiar de página.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina -= 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    @discord.ui.button(label="🔢 Ir a...", style=discord.ButtonStyle.primary)
    async def ir_a_pagina(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InventarioIrAModal(self))

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    async def on_timeout(self):
        if self.mensaje is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.mensaje.edit(view=self)
        except discord.HTTPException:
            pass

@bot.command(name="inv", aliases=["inventario"])
async def inv(ctx, miembro: discord.Member = None):
    """Muestra el inventario de objetos comprados en la tienda (paginado)"""
    miembro = miembro or ctx.author
    user_id = str(miembro.id)
    datos = inventario_cache.get(user_id)
    items = datos.get("items", {}) if datos else {}

    if not items:
        posesivo = "Todavía no compraste" if miembro.id == ctx.author.id else f"**{miembro.display_name}** todavía no compró"
        await ctx.reply(embed=crear_embed(descripcion=f"{posesivo} nada en `!shop`."))
        return

    view = InventarioView(ctx.author, miembro, items)
    mensaje = await ctx.reply(embed=view.construir_embed(), view=view)
    view.mensaje = mensaje

@bot.command(name="invfo")
async def invfo(ctx, *, nombre_buscado: str = None):
    """Muestra la ficha de un ítem puntual de la tienda"""
    if not shop_cache:
        await ctx.reply(embed=crear_embed(descripcion="La tienda está vacía. Subí `data/shop.json` al repo y corré `!shopreload`."))
        return

    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion="Usalo así: `!invfo Peluche de Ado`"))
        return

    categoria, item = buscar_item_shop(nombre_buscado)
    if item is None:
        await ctx.reply(embed=crear_embed(descripcion=f"No encontré ningún ítem que se llame **{nombre_buscado}** en la tienda. Mirá `!shop`."))
        return

    embed = crear_embed(titulo=f"{item['emoji']} {item['nombre']}", descripcion=item.get("descripcion") or None)
    embed.add_field(name="Categoría", value=categoria, inline=True)
    embed.add_field(name="Precio", value=formatear_pesos(item["precio"]), inline=True)
    if item.get("efecto"):
        embed.add_field(name="Usable", value="Sí, con `!usar`", inline=False)
    if item.get("imagen"):
        embed.set_image(url=item["imagen"])

    await ctx.reply(embed=embed)

# ===================== COMANDOS DE ITEMS =====================

async def aplicar_efecto_item(ctx, item, efecto):
    """Aplica el efecto del item. Devuelve el texto de resultado o None si no se pudo
    (en cuyo caso ya mandó un mensaje explicando por qué)."""
    global hubo_cambios_inventario_sin_guardar
    user_id = str(ctx.author.id)

    if efecto == "buff_suerte_rw":
        agregar_buff(ctx.author, "suerte_rw", item.get("valor", 0.05), item.get("duracion", 600))
        return f"✨ Aumentaste un **{int(item['valor']*100)}%** el peso de las rarezas altas en `!rw` durante **{formatear_tiempo_restante(item.get('duracion', 600))}**."

    if efecto == "buff_suerte_robo":
        agregar_buff(ctx.author, "suerte_robo", item.get("valor", 0.05), item.get("duracion", 3600))
        return f"🍀 Tenés **+{int(item['valor']*100)}%** de éxito en `!robar` durante **{formatear_tiempo_restante(item.get('duracion', 3600))}**."

    if efecto == "buff_suerte_apuesta":
        agregar_buff(ctx.author, "suerte_apuesta", item.get("valor", 0.05), item.get("duracion", 3600))
        return f"🎰 Tenés **+{int(item['valor']*100)}%** de ganar en `!cf` y `!roulette` durante **{formatear_tiempo_restante(item.get('duracion', 3600))}**."

    if efecto == "buff_pata_conejo":
        agregar_buff(ctx.author, "pata_conejo", item.get("valor", 0.05), item.get("duracion", 3600))
        return f"🐰 Si te agarran robando, perdés **{int(item['valor']*100)}% menos** de plata durante **{formatear_tiempo_restante(item.get('duracion', 3600))}**."

    if efecto == "buff_bono_w":
        agregar_buff(ctx.author, "bono_w", item.get("valor", 0.20), item.get("duracion", 259200))
        return f"🏛️ Ganás **+{int(item['valor']*100)}%** en `!w` durante **{formatear_tiempo_restante(item.get('duracion', 259200))}**."

    if efecto == "reducir_cooldown_rw":
        segundos = item.get("valor", 10800)
        restante_antes = cooldown_restante(cooldown_rw_usuario, user_id)
        if restante_antes <= 0:
            await ctx.reply(embed=crear_embed(descripcion="No tenés ningún cooldown activo de `!rw` para reducir. No gastaste el item."))
            return None
        reduccion = reducir_cooldown(cooldown_rw_usuario, user_id, segundos)
        restante_despues = cooldown_restante(cooldown_rw_usuario, user_id)
        if restante_despues <= 0:
            return f"⏳ Le sacaste **{formatear_tiempo_restante(reduccion)}** a tu cooldown de `!rw`. ¡Ya podés tirar de nuevo!"
        return f"⏳ Le sacaste **{formatear_tiempo_restante(reduccion)}** a tu cooldown de `!rw`.\nTe quedan **{formatear_tiempo_restante(restante_despues)}**."

    if efecto == "reducir_cooldown_w":
        segundos = item.get("valor", 120)
        restante_antes = cooldown_restante(cooldown_w_usuario, user_id)
        if restante_antes <= 0:
            await ctx.reply(embed=crear_embed(descripcion="No tenés ningún cooldown activo de `!w` para reducir. No gastaste el item."))
            return None
        reduccion = reducir_cooldown(cooldown_w_usuario, user_id, segundos)
        restante_despues = cooldown_restante(cooldown_w_usuario, user_id)
        if restante_despues <= 0:
            return f"☕ Le sacaste **{formatear_tiempo_restante(reduccion)}** a tu cooldown de `!w`. ¡Ya podés trabajar de nuevo!"
        return f"☕ Le sacaste **{formatear_tiempo_restante(reduccion)}** a tu cooldown de `!w`.\nTe quedan **{formatear_tiempo_restante(restante_despues)}**."

    if efecto == "reducir_cooldown_w_permanente":
        segundos = item.get("valor", 120)
        datos = inventario_cache.setdefault(user_id, {"nombre": ctx.author.display_name, "items": {}})
        if datos.get("reduccion_cooldown_w", 0) > 0:
            await ctx.reply(embed=crear_embed(descripcion="Ya usaste un **Café de especialidad**. No es acumulable y no gastaste este."))
            return None
        reduccion_maxima = COOLDOWN_W_SEGUNDOS - COOLDOWN_W_MINIMO
        nueva_reduccion = min(segundos, reduccion_maxima)
        datos["reduccion_cooldown_w"] = nueva_reduccion
        hubo_cambios_inventario_sin_guardar = True
        cooldown_nuevo = COOLDOWN_W_SEGUNDOS - nueva_reduccion
        return (
            f"⏱️ Reducción permanente aplicada.\n"
            f"Tu cooldown de `!w` ahora es de **{formatear_tiempo_restante(cooldown_nuevo)}** "
            f"(base: {formatear_tiempo_restante(COOLDOWN_W_SEGUNDOS)}).\n"
            f"-# Ya no podés usar otro Café de especialidad."
        )

    if efecto == "resetear_cooldown_rw":
        restante = resetear_cooldown(cooldown_rw_usuario, user_id)
        if restante <= 0:
            await ctx.reply(embed=crear_embed(descripcion="No tenés ningún cooldown activo de `!rw` para resetear. No gastaste el item."))
            return None
        return f"⏰ Tu cooldown de `!rw` se borró. Ya podés tirar de nuevo (tenías **{formatear_tiempo_restante(restante)}** restantes)."

    if efecto == "pase_vip":
        agregar_efecto_unico(ctx.author, "pase_vip", 1)
        return "🎟️ Guardaste un **Pase VIP**. La próxima vez que reclames un personaje en `!rw`, ignorás el cooldown de 3hs del reclamo."

    if efecto == "cristal_reroll":
        agregar_efecto_unico(ctx.author, "cristal_reroll", 1)
        return "💠 Guardaste un **Cristal de reroll**. La próxima vez que uses `!rw` estando en cooldown, vas a poder tirar igual (sin resetear el cooldown)."

    return None


# ===================== AUTOUSE =====================
# Cada efecto "usable" tiene, como mucho, UN momento natural en el que tiene
# sentido gatillarse solo. Si un efecto no está acá, significa que no hay un
# momento automático obvio para él (ej: los buffs de suerte, que se activan
# antes de una sesión, no después de una acción puntual) y !autouse lo rechaza.
TRIGGERS_AUTOUSE = {
    "reducir_cooldown_rw": "despues_de_rw",
    "resetear_cooldown_rw": "despues_de_rw",
    "cristal_reroll": "despues_de_rw",
    "pase_vip": "despues_de_reclamar",
}

NOMBRES_TRIGGER = {
    "despues_de_rw": "usar `!rw`",
    "despues_de_reclamar": "reclamar un personaje en `!rw`",
}

class _CtxSilencioso:
    """Shim mínimo para poder reusar aplicar_efecto_item() en el autouse sin que
    mande mensajes al canal público cuando el efecto no corresponde aplicarse
    (ej: 'no tenés cooldown que reducir'). El autouse falla en silencio en esos casos."""
    def __init__(self, author):
        self.author = author

    async def reply(self, *args, **kwargs):
        pass

async def ejecutar_autouse_silencioso(usuario, trigger, interaction=None):
    """Si `usuario` configuró un item para este gatillo y todavía le queda
    stock, lo usa automáticamente (reusando la misma lógica que !usar) y le
    avisa. Si `interaction` viene seteado (gatillo disparado desde un botón,
    como el de reclamar), avisa con un mensaje ephemeral de esa interacción
    -solo esa persona lo ve, sin salir del canal-. Si no hay interacción
    (gatillo disparado desde un comando de texto plano, como !rw) no hay forma
    de mandar algo ephemeral, así que cae a un DM. No hace nada si no hay
    autouse configurado, no tiene stock, o el efecto no necesitaba aplicarse
    (ej: no había cooldown activo)."""
    user_id = str(usuario.id)
    datos = inventario_cache.get(user_id)
    if not datos:
        return

    nombre_item = datos.get("autouse", {}).get(trigger)
    if not nombre_item:
        return

    if datos.get("items", {}).get(nombre_item, 0) <= 0:
        return  # ya no le queda, no hay nada que avisar

    _, item = buscar_item_shop(nombre_item)
    if item is None or TRIGGERS_AUTOUSE.get(item.get("efecto")) != trigger:
        return  # cambió la tienda y este item ya no corresponde a este gatillo

    global hubo_cambios_inventario_sin_guardar
    resultado_texto = await aplicar_efecto_item(_CtxSilencioso(usuario), item, item["efecto"])
    if resultado_texto is None:
        return  # no hacía falta (ej: no había cooldown que reducir), no se gastó nada

    quitar_item_del_inventario(usuario, nombre_item, 1)
    hubo_cambios_inventario_sin_guardar = True
    try:
        await guardar_inventario_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el inventario tras autouse: {e}", flush=True)

    embed_aviso = crear_embed(
        titulo="🤖 Autouse",
        descripcion=f"{resultado_texto}\n\n-# Se usó automáticamente tu **{item['emoji']} {item['nombre']}**.",
    )

    if interaction is not None:
        try:
            await interaction.followup.send(embed=embed_aviso, ephemeral=True)
            return
        except discord.HTTPException:
            pass  # el token de la interacción venció o algo falló: probamos DM igual

    try:
        await usuario.send(embed=embed_aviso)
    except discord.Forbidden:
        pass  # tiene los DMs cerrados, no hay forma de avisarle

@bot.command(name="usar", aliases=["use"])
async def usar(ctx, *, nombre_buscado: str = None):
    """Usa un item de tu inventario. Ej: !usar Dado de la suerte"""
    global hubo_cambios_inventario_sin_guardar

    if not shop_cache:
        await ctx.reply(embed=crear_embed(descripcion="La tienda está vacía."))
        return

    if not nombre_buscado:
        await ctx.reply(embed=crear_embed(descripcion="Decime qué item querés usar. Ejemplo: `!usar Dado de la suerte`"))
        return

    user_id = str(ctx.author.id)
    datos = inventario_cache.get(user_id)
    if not datos or not datos.get("items"):
        await ctx.reply(embed=crear_embed(descripcion="No tenés ningún item en tu inventario. Mirá `!shop`."))
        return

    # Buscar el item en el shop por nombre (usa la misma lógica difusa que !buy)
    categoria, item = buscar_item_shop(nombre_buscado)
    if item is None:
        await ctx.reply(embed=crear_embed(descripcion=f"No encontré ningún item llamado **{nombre_buscado}** en la tienda."))
        return

    # ¿El usuario realmente tiene este item?
    items_usuario = datos.get("items", {})
    if items_usuario.get(item["nombre"], 0) <= 0:
        await ctx.reply(embed=crear_embed(descripcion=f"No tenés **{item['nombre']}** en tu inventario."))
        return

    # ¿El item tiene un efecto usable?
    efecto = item.get("efecto")
    if not efecto:
        await ctx.reply(embed=crear_embed(descripcion=f"**{item['emoji']} {item['nombre']}** no tiene un efecto que se pueda usar. Es un item de colección."))
        return

    # Aplicar el efecto
    resultado_texto = await aplicar_efecto_item(ctx, item, efecto)
    if resultado_texto is None:
        # El efecto no se pudo aplicar (ya se mandó un mensaje explicando por qué)
        return

    # Consumir 1 unidad del item
    quitar_item_del_inventario(ctx.author, item["nombre"], 1)

    embed = crear_embed(
        titulo=f"{item['emoji']} {item['nombre']} usado",
        descripcion=resultado_texto,
        footer="Se consumió 1 unidad de tu inventario.",
    )
    await ctx.reply(embed=embed)
    try:
        await guardar_inventario_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el inventario: {e}", flush=True)

@bot.command(name="autouse")
async def autouse(ctx, *, nombre_buscado: str = None):
    """Configura que un item se use solo en su momento (!autouse nombre del item).
    Corré el mismo comando de nuevo con el mismo item para desactivarlo.
    Sin nombre, muestra qué tenés activo."""
    global hubo_cambios_inventario_sin_guardar
    user_id = str(ctx.author.id)
    datos = inventario_cache.setdefault(user_id, {"nombre": ctx.author.display_name, "items": {}})
    config_autouse = datos.setdefault("autouse", {})

    if not nombre_buscado:
        if not config_autouse:
            await ctx.reply(embed=crear_embed(
                descripcion="No tenés ningún autouse activo. Usalo así: `!autouse Pase VIP`."
            ))
            return
        lineas = [
            f"> Después de {NOMBRES_TRIGGER.get(trigger, trigger)}\n> se usa **{nombre_item}**"
            for trigger, nombre_item in config_autouse.items()
        ]
        await ctx.reply(embed=crear_embed(
            descripcion=f"### 🤖 Autouse activos\n{SEPARADOR}\n\n" + "\n\n".join(lineas)
        ))
        return

    categoria, item = buscar_item_shop(nombre_buscado)
    if item is None:
        await ctx.reply(embed=crear_embed(descripcion=f"No encontré ningún item llamado **{nombre_buscado}** en la tienda."))
        return

    trigger = TRIGGERS_AUTOUSE.get(item.get("efecto"))
    if trigger is None:
        await ctx.reply(embed=crear_embed(
            descripcion=f"**{item['emoji']} {item['nombre']}** no tiene un momento automático definido para usarse. Usalo manualmente con `!usar {item['nombre']}`."
        ))
        return

    # Toggle: si ya era justo este item el configurado en este gatillo, lo apagamos
    if config_autouse.get(trigger) == item["nombre"]:
        del config_autouse[trigger]
        hubo_cambios_inventario_sin_guardar = True
        try:
            await guardar_inventario_en_github()
        except Exception as e:
            print(f"⚠️ No pude guardar el autouse: {e}", flush=True)
        await ctx.reply(embed=crear_embed(
            descripcion=f"🔕 Desactivado. **{item['emoji']} {item['nombre']}** ya no se va a usar automáticamente."
        ))
        return

    anterior = config_autouse.get(trigger)
    config_autouse[trigger] = item["nombre"]
    hubo_cambios_inventario_sin_guardar = True
    try:
        await guardar_inventario_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el autouse: {e}", flush=True)

    texto = (
        f"🔔 A partir de ahora, después de {NOMBRES_TRIGGER.get(trigger, trigger)}, "
        f"se va a usar automáticamente tu **{item['emoji']} {item['nombre']}** (mientras tengas stock).\n\n"
        f"-# Te voy a avisar por DM cada vez que se use. Corré `!autouse {item['nombre']}` de nuevo para desactivarlo."
    )
    if anterior and anterior != item["nombre"]:
        texto = f"-# (Reemplazó a **{anterior}**, que tenía el mismo gatillo.)\n\n" + texto

    await ctx.reply(embed=crear_embed(titulo="🤖 Autouse configurado", descripcion=texto))


@bot.command(name="buffs", aliases=["efectos"])
async def buffs(ctx, miembro: discord.Member = None):
    """Muestra los buffs activos y efectos guardados de un usuario"""
    objetivo = miembro or ctx.author
    user_id = str(objetivo.id)
    datos = inventario_cache.get(user_id, {})

    lineas = []
    ahora = time.time()

    # --- Buffs temporales (los limpiamos de paso si expiraron) ---
    buffs_activos = datos.get("buffs", {})
    nombres_buffs = {
        "suerte_rw": "🎲 Dado de la suerte",
        "suerte_robo": "🍀 Trébol de 4 hojas",
        "suerte_apuesta": "🎰 Amuleto del ludópata",
        "pata_conejo": "🐰 Pata de conejo",
        "bono_w": "🏛️ Bono del gobierno",
    }

    buffs_vivos = {}
    for nombre, info in list(buffs_activos.items()):
        if info.get("expira", 0) > ahora:
            buffs_vivos[nombre] = info
        else:
            buffs_activos.pop(nombre, None)

    if buffs_vivos:
        lineas.append("**Buffs activos:**")
        for nombre, info in buffs_vivos.items():
            etiqueta = nombres_buffs.get(nombre, nombre)
            restante = info["expira"] - ahora
            lineas.append(f"> {etiqueta} — {formatear_tiempo_restante(restante)} restantes")
    else:
        lineas.append("**Buffs activos:**\n> *Ninguno.*")

    # --- Mejoras permanentes ---
    if datos.get("reduccion_cooldown_w", 0) > 0:
        cooldown_actual = max(COOLDOWN_W_MINIMO, COOLDOWN_W_SEGUNDOS - datos["reduccion_cooldown_w"])
        lineas.append("")
        lineas.append("**Mejoras permanentes:**")
        lineas.append(f"> ⏱️ `!w` — cooldown reducido a {formatear_tiempo_restante(cooldown_actual)} (base: {formatear_tiempo_restante(COOLDOWN_W_SEGUNDOS)})")

    # --- Efectos únicos guardados ---
    efectos = datos.get("efectos_unicos", {})
    efectos_activos = {k: v for k, v in efectos.items() if v > 0}
    nombres_efectos = {
        "pase_vip": "🎟️ Pase VIP",
        "cristal_reroll": "💠 Cristal de reroll",
    }

    if efectos_activos:
        lineas.append("")
        lineas.append("**Efectos guardados:**")
        for nombre, cantidad in efectos_activos.items():
            etiqueta = nombres_efectos.get(nombre, nombre)
            lineas.append(f"> {etiqueta} x{cantidad}")

    # --- Cooldowns activos ---
    restante_rw = cooldown_restante(cooldown_rw_usuario, user_id)
    restante_w = cooldown_restante(cooldown_w_usuario, user_id)

    if restante_rw > 0 or restante_w > 0:
        lineas.append("")
        lineas.append("**Cooldowns activos:**")
        if restante_rw > 0:
            lineas.append(f"> ⏳ `!rw` — {formatear_tiempo_restante(restante_rw)}")
        if restante_w > 0:
            lineas.append(f"> ☕ `!w` — {formatear_tiempo_restante(restante_w)}")

    embed = crear_embed(
        titulo=f"✨ Efectos de {objetivo.display_name}",
        descripcion="\n".join(lineas),
    )
    await ctx.reply(embed=embed)

# ===================== FIN COMANDOS DE ITEMS =====================

@bot.command(name="memoria")
async def memoria(ctx, miembro: discord.Member = None):
    """Muestra qué recuerda Catherine de vos (o de otro usuario)"""
    objetivo = miembro or ctx.author
    user_id = str(objetivo.id)
    datos = memoria_cache.get(user_id)

    if not datos:
        posesivo = "de vos" if objetivo.id == ctx.author.id else f"de **{objetivo.display_name}**"
        await ctx.reply(embed=crear_embed(descripcion=f"Catherine todavía no tiene memoria guardada {posesivo}. Hablale un poco por acá primero."))
        return

    descripcion = (
        f"👤 **Nombre**\n> {datos.get('nombre') or '???'}\n\n"
        f"🎭 **Personalidad**\n> {datos.get('personalidad') or '(todavía no la notó)'}\n\n"
        f"💭 **Gustos**\n> {datos.get('gustos') or '(todavía no los sabe)'}\n\n"
        f"💬 **Estuvieron hablando de**\n> {datos.get('hablamos_de') or '(nada en particular todavía)'}"
    )
    footer = None
    if datos.get("ultima_actualizacion"):
        footer = f"Actualizado por última vez el {time.strftime('%d/%m/%Y', time.localtime(datos['ultima_actualizacion']))}"

    embed = crear_embed(titulo=f"🧠 Lo que Catherine recuerda de {objetivo.display_name}", descripcion=descripcion, footer=footer)
    await ctx.reply(embed=embed)

@bot.command(name="olvidarme", aliases=["forgetme"])
async def olvidarme(ctx):
    """Borra lo que Catherine recuerda de vos"""
    global hubo_cambios_memoria_sin_guardar
    user_id = str(ctx.author.id)

    if user_id not in memoria_cache:
        await ctx.reply(embed=crear_embed(descripcion="Catherine no tenía nada guardado sobre vos."))
        return

    del memoria_cache[user_id]
    hubo_cambios_memoria_sin_guardar = True
    try:
        await guardar_memoria_en_github()
    except Exception as e:
        print(f"⚠️ No pude guardar el borrado de memoria: {e}", flush=True)

    await ctx.reply(embed=crear_embed(descripcion="🗑️ Listo, Catherine se olvidó de todo lo que sabía de vos."))

# ===================== HELP =====================

CATEGORIAS_HELP = [
    ("💰", "Tu cuenta", [
        "`!balance [@alguien]` · también `!bal` / `!saldo`\nMuestra tu plata, lo del banco y el total.\nEjemplo: `!balance` o `!balance @Usuario`",
        "`!perfil [@alguien]`\nFicha completa: plata, matrimonio, colección e inventario.\nEjemplo: `!perfil` o `!perfil @Usuario`",
        "`!top`\nTop 10 de usuarios con más plata total (balance + banco).\nEjemplo: `!top`",
        "`!pay @usuario cantidad`\nLe pasás plata de tu balance a otro usuario.\nEjemplo: `!pay @Usuario 5000` o `!pay @Usuario 100k`",
        "-# En cualquier `cantidad` del bot podés poner `all` (todo) o abreviar: `100k`, `2m`, `1b`, `1t`.",
    ]),
    ("🏦", "Banco y Caja", [
        "`!depositar cantidad`\nGuardás plata en el banco. Está a salvo de `!robar` y suma 5% compuesto por día.\nEjemplo: `!depositar 50000` o `!depositar all`",
        "`!retirar cantidad`\nSacás plata del banco de vuelta a tu balance.\nEjemplo: `!retirar 50000` o `!retirar all`",
        "`!depositarcaja cantidad`\nGuardás plata en tu Caja de ahorros. También está a salvo de `!robar`, pero SIN el interés del 5% y no cuenta para `!top` ni el ranking.\nEjemplo: `!depositarcaja 100k`",
        "`!retirarcaja cantidad`\nSacás plata de la Caja de ahorros.\nEjemplo: `!retirarcaja all`",
    ]),
    ("💼", "Trabajo", [
        "`!w`\nTrabajás y ganás entre 5.000 y 6.500 Pesos (con un mensaje random cada vez).\nCooldown: 3 minutos.",
        "`!mendigar`\nAbrís una colecta de 10 minutos para que el resto te done plata.\nCooldown: 20 minutos.",
    ]),
    ("💵", "Dólar", [
        "`!dolar` · también `!dólar` / `!usd`\nCotización actual del Dólar Catherine. Cambia cada 4hs, nunca baja de 300 Pesos.",
        "`!buy Dólar cantidad` · también `!comprar`\nComprás Dólares a la cotización actual.\nEjemplo: `!buy Dólar 10` o `!buy Dólar all`",
        "`!vender Dólar cantidad`\nVendés Dólares. Hay 2% de comisión.\nEjemplo: `!vender Dólar 5`",
        "`!topdolar` · también `!topdólar`\nTop 10 de quién tiene más Dólares guardados.",
    ]),
    ("🎰", "Casino", [
        "`!cf cara/cruz cantidad` · también `!coinflip`\nApuesta a cara o cruz (50/50).\nEjemplo: `!cf cara 5000` o `!cf cruz all`",
        "`!bj cantidad` · también `!blackjack`\nUna mano de blackjack contra la casa.\nEjemplo: `!bj 10000` o `!bj all`",
        "`!rt rojo/negro cantidad` · también `!roulette`\nApuesta a rojo o negro en la ruleta.\nEjemplo: `!rt rojo 5000` o `!rt negro all`",
    ]),
    ("🛍️", "Tienda", [
        "`!shop` · también `!tienda`\nCatálogo completo, por categorías (con Previous/Next).",
        "`!buy nombre [cantidad]` · también `!comprar`\nComprás algo de la tienda. La cantidad acepta `100k`, `all`, etc.\nEjemplo: `!buy Peluche de Ado` o `!buy Dólar 5`",
        "`!inv [@alguien]` · también `!inventario`\nVes los objetos que compraste (paginado).\nEjemplo: `!inv`",
        "`!invfo nombre del ítem`\nFicha de un ítem puntual.\nEjemplo: `!invfo Café doble`",
    ]),
    ("✨", "Items y buffs", [
        "`!usar nombre` · también `!use`\nUsás un item de tu inventario (activa su efecto).\nEjemplo: `!usar Dado de la suerte`",
        "`!autouse nombre`\nConfigurás que ese item se use solo en su momento. Volvé a correr el mismo comando para desactivarlo.\nEjemplo: `!autouse Pase VIP`",
        "`!buffs [@alguien]` · también `!efectos`\nVes tus buffs activos, mejoras permanentes y cooldowns.\nEjemplo: `!buffs`",
    ]),
    ("🎴", "Gacha (personajes)", [
        "`!rw`\nTirás un personaje random. Los de más valor son más difíciles de sacar.\nCooldown: 4hs. Reclamar tiene su propio cooldown de 3hs.",
        "`!winfo nombre`\nFicha de un personaje puntual, sin reclamarlo.\nEjemplo: `!winfo Gojo Satoru`",
        "`!coleccion [@alguien]` · también `!harem`\nVes los personajes que reclamaste (paginado).\nEjemplo: `!coleccion`",
        "`!vender personaje`\nVendés un personaje. Te llevás el 70% de su valor en Pesos.\nEjemplo: `!vender Gojo Satoru`",
        "`!tasacion personaje`\nCuánto te darían por venderlo, sin venderlo.\nEjemplo: `!tasacion Gojo`",
    ]),
    ("💍", "Social", [
        "`!marry @usuario`\nTe casás con otro usuario.\nEjemplo: `!marry @Usuario`",
        "`!divorcio`\nTe divorciás. Cuesta 5.000 a cada uno.\nEjemplo: `!divorcio`",
        "`!givechar nombre @usuario`\nLe regalás un personaje de tu colección a otro usuario.\nEjemplo: `!givechar Gojo Satoru @Usuario`",
        "`!robar @usuario`\nSi está desconectado hace 6hs o más: 10% de afanarle todo el balance, 10% de perder vos el 5%, 80% no pasa nada.\nEjemplo: `!robar @Usuario`",
    ]),
    ("🎵", "Multimedia", [
        "`!mp3 búsqueda` o `!mp3 link`\nTe paso el audio de un video de YouTube (busca en YouTube Music primero).\nEjemplo: `!mp3 Oasis Wonderwall` o `!mp3 https://youtu.be/...`",
    ]),
]

CATEGORIAS_ADMINHELP = [
    ("💰", "Economía", [
        "`!addmoney @usuario cantidad`\nLe agregás plata a alguien.\nEjemplo: `!addmoney @Usuario 100000`",
        "`!wAdmin`\nIgual que `!w` pero sin cooldown.",
        "`!5porcentforce`\nFuerza el 5% de interés diario a tu banco ahora mismo, sin esperar las 24hs.",
        "`!setgeneralchannel`\nConfigura ESTE canal para que Catherine anuncie los cambios del dólar.",
    ]),
    ("🎴", "Gacha / rw", [
        "`!rwAdmin`\nIgual que `!rw` pero sin cooldown (ni de tirada ni de reclamo).",
        "`!rwreload`\nRecarga `rw.json` desde GitHub sin reiniciar el bot.",
        "`!checkimg`\nRevisa qué links de imagen de los personajes están rotos.",
        "`!buscarimagenes [cantidad]` · también `!imgsearch`\nBuscás fotos para los personajes sin imagen (Guardar/Rechazar/Recargar).\nEjemplo: `!buscarimagenes 5`",
        "`!imgcompare [cantidad]` · también `!comparaimg`\nComo el anterior, pero con las 3 candidatas juntas en un collage.",
        "`!galeria`\nRecorrés todos los personajes uno por uno para revisar imágenes.",
    ]),
    ("🛍️", "Tienda / datos", [
        "`!shopreload`\nRecarga `shop.json` desde GitHub sin reiniciar el bot.",
        "`!memoria [@alguien]`\nVes qué recuerda Catherine de vos (o de alguien más).",
        "`!olvidarme`\nBorra lo que Catherine recuerda de vos.",
        "`!datasave`\nFuerza el guardado inmediato de TODOS los datos a GitHub.",
    ]),
]

class HelpView(discord.ui.View):
    """Help paginado por categorías con Previous/Next, igual que
    ColeccionView/ShopView/InventarioView/GaleriaView. Solo quien pidió el
    help puede cambiar de página (interaction_check)."""

    def __init__(self, autor, titulo, categorias):
        super().__init__(timeout=180)
        self.autor = autor  # solo quien pidió el help puede cambiar de página
        self.titulo = titulo
        self.categorias = categorias
        self.pagina = 0
        self.mensaje = None
        self._actualizar_botones()

    def _actualizar_botones(self):
        self.anterior.disabled = self.pagina == 0
        self.siguiente.disabled = self.pagina >= len(self.categorias) - 1

    def construir_embed(self):
        emoji, nombre, lineas = self.categorias[self.pagina]
        return crear_embed(
            titulo=f"{self.titulo} — {emoji} {nombre}",
            descripcion="\n\n".join(lineas),
            footer=f"Categoría {self.pagina + 1}/{len(self.categorias)}",
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Pedí tu propio `!help` para moverte por las categorías.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina -= 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    async def on_timeout(self):
        if self.mensaje is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.mensaje.edit(view=self)
        except discord.HTTPException:
            pass

@bot.command(name="help")
async def ayuda(ctx):
    """Lista los comandos disponibles para todos los usuarios, por categorías"""
    vista = HelpView(ctx.author, "Comandos de Catherine", CATEGORIAS_HELP)
    vista.mensaje = await ctx.reply(embed=vista.construir_embed(), view=vista)

@bot.command(name="adminhelp", hidden=True)
async def adminhelp(ctx):
    """Lista los comandos de administración (solo dueño de la banca), por categorías"""
    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return

    vista = HelpView(ctx.author, "🛠️ Comandos de administración", CATEGORIAS_ADMINHELP)
    vista.mensaje = await ctx.reply(embed=vista.construir_embed(), view=vista)

_HEADERS_NAVEGADOR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

async def _chequear_una_imagen(session, semaforo, nombre, url):
    """Devuelve (nombre, ok, motivo) para un link de imagen puntual."""
    if not url:
        return (nombre, False, "sin link cargado")
    async with semaforo:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=12),
                allow_redirects=True,
                headers=_HEADERS_NAVEGADOR,
            ) as resp:
                if resp.status != 200:
                    return (nombre, False, f"código {resp.status}")
                content_type = resp.headers.get("Content-Type", "").lower()
                # Solo lo marcamos roto si claramente devolvió una página web en vez de la imagen.
                # Muchos hosts (Imgur, CDNs) no mandan un Content-Type prolijo con imagen/*.
                if "text/html" in content_type:
                    return (nombre, False, "el link redirige a una página, no a la imagen")
                return (nombre, True, "")
        except Exception as e:
            return (nombre, False, f"error de conexión ({type(e).__name__})")

@bot.command(name="checkimg")
async def checkimg(ctx):
    """Revisa todos los links de imagen de rw.json y avisa cuáles están rotos"""
    if not personajes_cache:
        await ctx.reply(embed=crear_embed(descripcion="No hay personajes cargados. Probá `!rwreload` primero."))
        return

    aviso = await ctx.reply(embed=crear_embed(
        descripcion=f"🔎 Revisando {len(personajes_cache)} links de imagen, dame un toque..."
    ))

    semaforo = asyncio.Semaphore(10)  # no golpear 101 links todos a la vez
    async with aiohttp.ClientSession() as session:
        tareas = [
            _chequear_una_imagen(
                session, semaforo,
                campo_personaje(p, "Nombre", "nombre"),
                campo_personaje(p, "Imagen", "imagen", default=None),
            )
            for p in personajes_cache
        ]
        resultados = await asyncio.gather(*tareas)

    rotas = [(nombre, motivo) for nombre, ok, motivo in resultados if not ok]

    if not rotas:
        await aviso.edit(embed=crear_embed(
            titulo="🔎 Chequeo de imágenes",
            descripcion=f"Revisé los **{len(personajes_cache)}** personajes y están todos los links bien. Ninguno roto 🎉"
        ))
        return

    lineas = [f"❌ **{nombre}** — {motivo}" for nombre, motivo in rotas]
    resumen = f"De {len(personajes_cache)} personajes, **{len(rotas)}** tienen la imagen rota:\n\n"

    # Discord no deja más de 4096 caracteres en la descripción, así que partimos en varios embeds si hace falta
    bloque_actual = resumen
    bloques = []
    for linea in lineas:
        if len(bloque_actual) + len(linea) + 1 > 4000:
            bloques.append(bloque_actual)
            bloque_actual = ""
        bloque_actual += linea + "\n"
    bloques.append(bloque_actual)

    await aviso.edit(embed=crear_embed(titulo="🔎 Chequeo de imágenes", descripcion=bloques[0]))
    for bloque in bloques[1:]:
        await ctx.send(embed=crear_embed(descripcion=bloque))

@bot.command(name="5porcentforce", hidden=True)
async def cinco_porciento_force(ctx):
    """Fuerza el 5% de interés diario a tu banco ahora mismo, sin esperar las 24hs. Solo vos."""
    global hubo_cambios_sin_guardar

    if str(ctx.author.id) != ID_BANCA:
        await ctx.reply(embed=crear_embed(descripcion="Este comando es solo para el dueño de la banca."))
        return

    user_id = str(ctx.author.id)
    if user_id not in balances_cache:
        balances_cache[user_id] = {"nombre": ctx.author.display_name, "balance": 0, "banco": 0}
    balances_cache[user_id].setdefault("banco", 0)
    banco = balances_cache[user_id]["banco"]

    if banco <= 0:
        await ctx.reply(embed=crear_embed(descripcion="No tenés nada en el banco todavía. Usá `!depositar` primero."))
        return

    ganancia = round(banco * INTERES_BANCO_DIARIO)
    balances_cache[user_id]["banco"] = banco + ganancia
    balances_cache[user_id]["ultimo_interes"] = time.time()  # reinicia el reloj de 24hs para que no se sume de nuevo solo
    hubo_cambios_sin_guardar = True

    await ctx.reply(embed=crear_embed(
        titulo="🏦 5% forzado",
        descripcion=(
            f"Le metiste el 5% a tu banco a la fuerza: **+{formatear_pesos(ganancia)}**.\n"
            f"Banco actual: **{formatear_pesos(balances_cache[user_id]['banco'])}**"
        ),
    ))

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
        guardado_personajes = await guardar_personajes_en_github()
        guardado_inventario = await guardar_inventario_en_github()
        guardado_matrimonios = await guardar_matrimonios_en_github()
        guardado_memoria = await guardar_memoria_en_github()
        guardado_dolar = await guardar_dolar_en_github()
        if guardado_balances or guardado_characters or guardado_personajes or guardado_inventario or guardado_matrimonios or guardado_memoria or guardado_dolar:
            await aviso.edit(embed=crear_embed(descripcion="💾 Listo, quedó todo guardado en GitHub. Podés estar tranquilo/a."))
        else:
            await aviso.edit(embed=crear_embed(descripcion="No había cambios nuevos desde el último guardado, así que no hizo falta tocar nada."))
    except Exception as e:
        await aviso.edit(embed=crear_embed(descripcion=f"❌ Hubo un error al guardar: {str(e)}"))

PERSONAJES_POR_PAGINA = 10

class ColeccionView(discord.ui.View):
    def __init__(self, autor, miembro, personajes):
        super().__init__(timeout=120)
        self.autor = autor  # solo quien pidió la colección puede pasar de página
        self.miembro = miembro
        self.personajes = sorted(
            personajes,
            key=lambda p: parsear_valor(campo_personaje(p, "Valor", "valor")) or 0,
            reverse=True,
        )
        self.pagina = 0
        self.total_paginas = max(1, -(-len(personajes) // PERSONAJES_POR_PAGINA))
        self.mensaje = None
        self._actualizar_botones()

    def _actualizar_botones(self):
        self.anterior.disabled = self.pagina == 0
        self.siguiente.disabled = self.pagina >= self.total_paginas - 1

    def construir_embed(self):
        inicio = self.pagina * PERSONAJES_POR_PAGINA
        fin = inicio + PERSONAJES_POR_PAGINA
        lineas = []
        for p in self.personajes[inicio:fin]:
            nombre_p = campo_personaje(p, "Nombre", "nombre")
            valor_p = campo_personaje(p, "Valor", "valor")
            lineas.append(f"> 🎴 **{nombre_p}** — {formatear_pesos(valor_p)}")

        if lineas:
            descripcion = (
                f"### 📚 Colección de {self.miembro.display_name}\n"
                f"{SEPARADOR}\n\n"
                + "\n\n".join(lineas)
            )
        else:
            descripcion = (
                f"### 📚 Colección de {self.miembro.display_name}\n"
                f"{SEPARADOR}\n\n"
                f"-# No hay personajes en esta página."
            )

        return crear_embed(
            descripcion=descripcion,
            footer=f"Página {self.pagina + 1}/{self.total_paginas} — {len(self.personajes)} personajes en total",
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message("Solo quien pidió la colección puede cambiar de página.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina -= 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    @discord.ui.button(label="🔢 Ir a...", style=discord.ButtonStyle.primary)
    async def ir_a_pagina(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ColeccionIrAModal(self))

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pagina += 1
        self._actualizar_botones()
        await interaction.response.edit_message(embed=self.construir_embed(), view=self)

    async def on_timeout(self):
        if self.mensaje is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.mensaje.edit(view=self)
        except discord.HTTPException:
            pass

class ColeccionIrAModal(discord.ui.Modal):
    def __init__(self, vista: ColeccionView):
        super().__init__(title="Ir a una página")
        self.vista = vista
        self.numero = discord.ui.TextInput(
            label=f"Página (1-{vista.total_paginas})",
            placeholder="Ej: 5",
            required=True,
            max_length=10,
        )
        self.add_item(self.numero)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            numero = int(self.numero.value.strip())
        except ValueError:
            await interaction.response.send_message("Eso no es un número.", ephemeral=True)
            return
        if not (1 <= numero <= self.vista.total_paginas):
            await interaction.response.send_message(f"Tiene que ser un número entre 1 y {self.vista.total_paginas}.", ephemeral=True)
            return

        self.vista.pagina = numero - 1
        self.vista._actualizar_botones()
        await interaction.response.edit_message(embed=self.vista.construir_embed(), view=self.vista)

@bot.command(name="coleccion", aliases=["harem"])
async def coleccion(ctx, miembro: discord.Member = None):
    """Muestra los personajes reclamados por vos o por alguien más, con paginado"""
    miembro = miembro or ctx.author
    user_id = str(miembro.id)

    datos = characters_cache.get(user_id)
    if not datos or not datos.get("personajes"):
        await ctx.reply(embed=crear_embed(
            descripcion=f"**{miembro.display_name}** todavía no reclamó ningún personaje. Probá `!rw`."
        ))
        return

    view = ColeccionView(ctx.author, miembro, datos["personajes"])
    mensaje = await ctx.reply(embed=view.construir_embed(), view=view)
    view.mensaje = mensaje

# ===================== YTMUSICAPI (reemplaza a YouTube Data API) =====================

def _buscar_en_ytmusic_sync(query, limite=1):
    """Búsqueda síncrona en YouTube Music vía ytmusicapi (sin API key).
    Devuelve un dict {'videoId': ..., 'title': ..., 'artists': '...'} o None.
    Primero busca filtrando por 'songs' (música oficial); si no hay nada,
    prueba sin filtro (por si es un video musical, cover, etc.)."""
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        resultados = yt.search(query, filter="songs", limit=limite)
        if not resultados:
            resultados = yt.search(query, limit=limite)
        if not resultados:
            return None

        item = resultados[0]
        video_id = item.get("videoId")
        if not video_id:
            return None

        titulo = item.get("title") or "audio"

        # Los artistas vienen como lista de dicts con 'name'
        artists_raw = item.get("artists") or []
        if isinstance(artists_raw, list):
            nombres = [a.get("name") for a in artists_raw if isinstance(a, dict) and a.get("name")]
            artistas = ", ".join(nombres) if nombres else None
        else:
            artistas = str(artists_raw) if artists_raw else None

        return {"videoId": video_id, "title": titulo, "artists": artistas}
    except Exception as e:
        print(f"[ytmusic] Error buscando '{query}': {e}", flush=True)
        return None


async def buscar_en_ytmusic(query, limite=1):
    """Wrapper async: corre la búsqueda síncrona en un hilo aparte para no
    bloquear el event loop del bot."""
    return await asyncio.to_thread(_buscar_en_ytmusic_sync, query, limite)

# ===================== FIN YTMUSICAPI =====================

async def buscar_por_scraping(session, texto):
    """Fallback: busca directo en youtube.com/results y parsea el primer video ID.
    Más preciso que la API, pero puede fallar."""
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

@bot.command(name="mp3")
async def mp3(ctx, *, entrada: str = None):
    """Busca (o recibe un link de) un video de YouTube y manda el audio como mp3.
    Busca primero en YouTube Music (ytmusicapi) y cae al scraping de YouTube
    normal si ytmusicapi no encuentra nada."""
    if not entrada:
        await ctx.reply(embed=crear_embed(descripcion="Decime qué buscar, o pasame un link. Ejemplo: `!mp3 bruh` o `!mp3 Oasis Wonderwall`"))
        return

    if not RAPIDAPI_KEY:
        await ctx.reply(embed=crear_embed(descripcion="❌ Falta configurar RAPIDAPI_KEY en las variables de entorno de Render."))
        return

    embed = crear_embed(titulo=entrada, descripcion="⏳ Buscando video...")
    aviso = await ctx.reply(embed=embed)

    # Los pasos se van ACUMULANDO en el embed (no se reemplazan), así el usuario
    # ve todo el progreso junto: ✅ Buscando video / ✅ Convirtiendo / ⏳ Descargando.
    pasos = ["⏳ Buscando video..."]

    async def mostrar_pasos():
        embed.description = "\n\n".join(pasos)
        await aviso.edit(embed=embed)

    async def siguiente_paso(texto):
        if pasos:
            pasos[-1] = pasos[-1].replace("⏳", "✅")
        pasos.append(f"⏳ {texto}")
        await mostrar_pasos()

    async def mostrar_error(mensaje):
        if pasos:
            pasos[-1] = pasos[-1].replace("⏳", "❌")
        pasos.append(f"❌ {mensaje}")
        embed.description = "\n\n".join(pasos)
        await aviso.edit(embed=embed)

    match = re.search(r"(?:youtube\.com\/(?:watch\?v=|shorts\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})", entrada)

    try:
        async with aiohttp.ClientSession() as session:
            titulo = "audio"
            canal = None

            if match:
                video_id = match.group(1)
            else:
                # 1) Buscamos en YouTube Music primero (mejores resultados para música)
                resultado_ytmusic = await buscar_en_ytmusic(entrada, limite=1)

                if resultado_ytmusic:
                    video_id = resultado_ytmusic["videoId"]
                    titulo = resultado_ytmusic.get("title") or "audio"
                    canal = resultado_ytmusic.get("artists")
                else:
                    # 2) Fallback: scraping de YouTube normal
                    video_id = await buscar_por_scraping(session, entrada)
                    if not video_id:
                        await mostrar_error("No encontré nada para eso. Probá con otro nombre o pasame el link directo.")
                        return

            await siguiente_paso("Convirtiendo a MP3...")

            headers = {
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": RAPIDAPI_HOST,
            }

            mp3_url = None
            duracion = None

            for _ in range(10):
                async with session.get(f"https://{RAPIDAPI_HOST}/dl", params={"id": video_id}, headers=headers) as resp:
                    data = await resp.json()

                estado = data.get("status")
                if estado == "ok":
                    mp3_url = data.get("link")
                    # Si RapidAPI devuelve un título distinto, lo preferimos (suele estar más limpio)
                    if data.get("title"):
                        titulo = data["title"]
                    duracion = data.get("duration")
                    break
                elif estado == "processing":
                    await asyncio.sleep(3)
                else:
                    await mostrar_error(f"No se pudo convertir: {data.get('msg', 'error desconocido')}")
                    return

            if not mp3_url:
                await mostrar_error("Tardó demasiado en procesar el video, probá de nuevo en un rato.")
                return

            await siguiente_paso("Descargando audio...")

            headers_descarga = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Referer": "https://ytjar.info/",
            }

            LIMITE_MB = 9.5
            contenido = None
            demasiado_grande = False

            for intento in range(3):
                async with session.get(mp3_url, headers=headers_descarga) as resp_mp3:
                    if resp_mp3.status != 200:
                        await asyncio.sleep(2)
                        continue

                    # Antes de bajar nada: si el header Content-Length dice que pesa
                    # más de lo que Discord permite, ni lo descargamos. Nos ahorramos
                    # bajarnos 100MB a RAM para después tirarlos.
                    content_length = resp_mp3.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) / (1024 * 1024) > LIMITE_MB:
                                demasiado_grande = True
                                break
                        except (ValueError, TypeError):
                            pass

                    contenido = await resp_mp3.read()
                    break

            # Caso 1: demasiado grande, ni lo bajamos → solo link
            if demasiado_grande:
                embed.title = "Descarga completada"
                embed.description = (
                    f"🎵 **{titulo}**\n"
                    + (f"👤 {canal}\n" if canal else "")
                    + f"\n🔗 Pesa más de 10MB, no entra en Discord. Link directo:\n{mp3_url}\n\n"
                    f"-# Video original: https://youtu.be/{video_id}"
                )
                await aviso.edit(embed=embed)
                return

            # Caso 2: no pudimos bajarlo, pero el link sirve igual
            if contenido is None:
                embed.title = "Descarga completada"
                embed.description = (
                    f"🎵 **{titulo}**\n"
                    + (f"👤 {canal}\n" if canal else "")
                    + f"\n🔗 No pude bajarlo yo misma, pero acá tenés el link directo:\n{mp3_url}\n\n"
                    f"-# Video original: https://youtu.be/{video_id}"
                )
                await aviso.edit(embed=embed)
                return

            tamaño_mb = len(contenido) / (1024 * 1024)

            # Caso 3: lo bajamos pero igual pesa más de lo permitido (Content-Length
            # mentía o no venía) → link en vez de adjunto
            if tamaño_mb > LIMITE_MB:
                embed.title = "Descarga completada"
                embed.description = (
                    f"🎵 **{titulo}**\n"
                    + (f"👤 {canal}\n" if canal else "")
                    + f"\n🔗 Pesa {tamaño_mb:.1f}MB, no entra en Discord. Link directo:\n{mp3_url}\n\n"
                    f"-# Video original: https://youtu.be/{video_id}"
                )
                await aviso.edit(embed=embed)
                return

            # Resultado final: título + canal + tamaño/kbps + link al video de YT
            bits_info = [f"💾 {tamaño_mb:.1f} MB"]
            if duracion and duracion > 0:
                bitrate_kbps = round((len(contenido) * 8) / duracion / 1000)
                bits_info.append(f"{bitrate_kbps} kbps")

            embed.title = "Descarga completada"
            embed.description = (
                f"🎵 **{titulo}**\n"
                + (f"👤 {canal}\n" if canal else "")
                + " · ".join(bits_info) + "\n"
                + f"🔗 https://youtu.be/{video_id}"
            )

            nombre_archivo = re.sub(r'[\\/*?:"<>|]', "", titulo)[:80] or "audio"
            await aviso.edit(embed=embed, attachments=[discord.File(io.BytesIO(contenido), filename=f"{nombre_archivo}.mp3")])

    except Exception as e:
        await mostrar_error(f"Hubo un error: {str(e)}")

# Iniciar el bot
bot.run(os.environ.get("DISCORD_TOKEN"))
