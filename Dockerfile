FROM python:3.13-slim

# Instalar ffmpeg a nivel del sistema (yt-dlp lo necesita para convertir a mp3)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements_gemini.txt .
RUN pip install --no-cache-dir -r requirements_gemini.txt

COPY . .

CMD ["python", "catherine_bot_final.py"]
