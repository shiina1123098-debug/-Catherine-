FROM python:3.13-slim

WORKDIR /app

COPY requirements_gemini.txt .
RUN pip install --no-cache-dir -r requirements_gemini.txt

COPY . .

CMD ["python", "catherine_bot_final.py"]
