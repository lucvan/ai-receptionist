FROM python:3.12-slim

# Runs as a non-root user with no shell. This container is reachable from the
# public internet by design, so a shell in it is a liability, not a convenience.
RUN useradd --system --create-home --shell /usr/sbin/nologin receptionist

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY prompts/ ./prompts/

RUN mkdir -p /app/logs && chown -R receptionist:receptionist /app

USER receptionist

EXPOSE 5050 5051

# No healthcheck that boots a second Python interpreter - a CLI probe on a short
# interval burns measurable idle CPU. Compose uses a plain TCP/HTTP check instead.
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "5050", "--no-server-header"]
