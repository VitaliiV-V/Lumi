FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --user-group --no-create-home reminder \
    && install -d -m 0700 -o reminder -g reminder /app/data

COPY reminder_bot/*.py ./reminder_bot/

USER reminder
CMD ["python", "-m", "reminder_bot"]
