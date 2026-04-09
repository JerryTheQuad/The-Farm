FROM python:3.12-slim

WORKDIR /app

COPY Code.py README.md ./

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

EXPOSE 8080

# Back4App expects an exposed TCP port; health server listens on $PORT.
CMD ["sh", "-c", "python Code.py run --config /app/config.json --health-port ${PORT:-8080} --health-host 0.0.0.0"]
