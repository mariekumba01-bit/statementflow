# StatementFlow — image API (locale ET Render)
#
# Construction :  docker build -t statementflow .
# Lancement  :    docker run --rm -p 8080:8080 statementflow
#
# ⚠️ NE PAS EXPOSER PUBLIQUEMENT SANS AUTHENTIFICATION CONFIGURÉE :
# définissez STATEMENTFLOW_API_KEY_HASHES ou STATEMENTFLOW_KEYS_PATH
# (sinon l'API répond 503 sur toutes les routes privées — verrou v0.3.1).
# Voir docs/DEPLOYMENT.md et docs/SECURITY.md.
FROM python:3.12-slim

# tesseract + poppler : OCR des PDF scannés (tessdata eng par défaut)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api,pg]"

# non-root
RUN useradd --create-home sf && chown -R sf:sf /app
USER sf

# Render injecte PORT ; en local le défaut reste 8080 (start.sh lit $PORT).
COPY --chown=sf:sf docker/start.sh /app/docker/start.sh
RUN chmod +x /app/docker/start.sh

ENV STATEMENTFLOW_API_HOST=0.0.0.0 \
    STATEMENTFLOW_API_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', os.environ.get('STATEMENTFLOW_API_PORT', '8080')) + '/health')" || exit 1

# start.sh : uvicorn sur le port dynamique $PORT (Render) avec repli 8080
ENTRYPOINT ["/app/docker/start.sh"]
