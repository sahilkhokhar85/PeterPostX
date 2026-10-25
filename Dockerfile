FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py db.py helpers.py ./
RUN useradd -m app
USER app
CMD ["python", "bot.py"]
