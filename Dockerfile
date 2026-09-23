FROM python:3.13-slim

# Instalar ffmpeg (para convertir a mp3) y curl (para instalar Deno)
RUN apt-get update && \
    apt-get install -y ffmpeg curl unzip && \
    rm -rf /var/lib/apt/lists/*

# Instalar Deno: yt-dlp lo necesita como motor de JavaScript para resolver
# los desafíos que pone YouTube antes de dejar extraer los videos
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app

COPY requirements_gemini.txt .
RUN pip install --no-cache-dir -r requirements_gemini.txt

COPY . .

CMD ["python", "catherine_bot_final.py"]
