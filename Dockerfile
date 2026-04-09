FROM python:3.12-slim

WORKDIR /app

COPY Code.py README.md ./

ENV PYTHONUNBUFFERED=1

# By default starts the bot with config mounted/provided by user.
CMD ["python", "Code.py", "run", "--config", "/app/config.json"]
