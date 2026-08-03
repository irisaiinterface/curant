FROM python:3.12-slim

# SQLCipher requires libsqlcipher — the system library that sqlcipher3
# Python package wraps. Everything else is pure Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlcipher-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Data lives in a named volume so the DB survives container restarts
VOLUME ["/data"]
ENV DATABASE_PATH=/data/curant_cloud.db

# Not root — run as a non-privileged user
RUN useradd -r -s /bin/false curant
USER curant

EXPOSE 5051

# Gunicorn in production (installed via requirements); fallback to Flask dev server
# if gunicorn isn't present. Add gunicorn to requirements.txt for production.
CMD ["python", "app.py"]
