FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .
COPY dashboard.html .

EXPOSE 7842

CMD ["sh", "-c", "python collector.py --key \"$REQUESTY_KEY\" --auth-token \"$AUTH_TOKEN\" --port 7842 --auto"]
