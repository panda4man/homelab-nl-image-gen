FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001

CMD ["sh", "-c", "gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-5001} server:app"]
