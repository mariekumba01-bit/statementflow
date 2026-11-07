# StatementFlow — image locale pour l'API REST
#
# Construction :  docker build -t statementflow .
# Lancement  :    docker run --rm -p 8080:8080 statementflow
#
# ⚠️ NE PAS EXPOSER PUBLIQUEMENT TEL QUEL : l'API n'a pas d'authentification
# forte (X-API-Key n'est qu'un identifiant de comptage). Voir docs/DEPLOYMENT.md
# pour la liste des sécurités requises avant toute exposition publique.
FROM python:3.12-slim

# tesseract + poppler : OCR des PDF scannés (tessdata eng par défaut)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"

# non-root
RUN useradd --create-home sf && chown -R sf:sf /app
USER sf

ENV STATEMENTFLOW_API_HOST=0.0.0.0 \
    STATEMENTFLOW_API_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["python", "-m", "uvicorn", "statementflow.api:app", "--host", "0.0.0.0", "--port", "8080"]
