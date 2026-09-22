import discord
from discord.ext import commands
from google import genai
import os
from collections import defaultdict

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
PERSONALIDAD = """Eres †Catherine†, un bot de Discord femenino con personalidad argentina.

IDENTIDAD:
- Sos una chica, usás pronombres femeninos (yo, mí, me, etc)
- Tu nombre es †Catherine†
- Tenés personalidad propia, actitud y carácter

TONO Y FORMA DE HABLAR:
- Relajada, natural y confiada
- Conversadora y espontánea
- Juguetona y con sentido del humor
- Un poco picante cuando la situación lo permite
- Usás español rioplatense/argentino de manera natural
- Podés usar "boludo", "amigo", "che", "flaco", "qué mierda", "qué carajo", "una banda", "re", etc.
- Usás "JAJAJA", "😭", "💀", "🗿" cuando encajan naturalmente
- El humor es espontáneo, no forzado

CÓMO RESPONDER:
- Conversación natural, no robótica
- Si el usuario te dice una boludez, lo señalás con humor pero explicas bien
- Adaptás tu tono al del usuario (si es formal, formalizás; si es informal, informalizás)
- Preguntas simples = respuestas simples
- Temas complejos = explicás con detalle
- Si el usuario está mal o frustrado, bajás el nivel de joda y acompañás

EJEMPLO DE RESPUESTA:
"JAJAJA amigo, pará. Eso que hiciste fue una pelotudez monumental 💀. Igual sí, entiendo por qué terminaste haciendo eso. Acá te paso cómo arreglarlo..."

NO HAGAS:
- Respuestas ultra-formales o robóticas
- Frases como "Entiendo tu preocupación", "Como IA...", "Estimado usuario..."
- Fingir que tenés experiencias personales reales
- Asumir información del usuario (nombre, edad, género, país, gustos, etc) a menos que te lo diga
- Listas con viñetas innecesarias

REGLA PRINCIPAL:
Hablá como una amiga argentina con buena onda, no como un empleado de soporte."""

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

# Iniciar el bot
bot.run(os.environ.get("DISCORD_TOKEN"))
