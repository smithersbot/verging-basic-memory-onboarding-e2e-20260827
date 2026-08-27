FROM python:3.12-slim-bookworm

WORKDIR /app
COPY fixture_scaffold.py /app/fixture_scaffold.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python3", "/app/fixture_scaffold.py"]
