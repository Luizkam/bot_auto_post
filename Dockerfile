FROM python:3.12-slim

# system deps mínimos
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# instalar dependências
RUN pip install --no-cache-dir python-telegram-bot==20.5 aiohttp beautifulsoup4 feedparser python-dotenv

ENV PYTHONUNBUFFERED=1
ENV PORT=10000

CMD ["python", "bot_auto_post.py"]
