#!/bin/sh
# Démarrage de l'API StatementFlow — port dynamique pour Render.
#
# Render injecte la variable PORT (unique port exposé autorisé sur le
# plan gratuit) ; en local ou docker run, le repli est 8080 (ou
# STATEMENTFLOW_API_PORT si défini).
set -eu

PORT="${PORT:-${STATEMENTFLOW_API_PORT:-8080}}"
export STATEMENTFLOW_API_PORT="$PORT"

# Les erreurs de démarrage (import cassé, config invalide) doivent faire
# échouer le conteneur immédiatement : uvicorn s'arrête de lui-même en
# cas d'erreur de configuration, et l'absence de boucle "restart" rend
# l'échec visible dans les logs Render.
exec python -m uvicorn statementflow.api:app \
    --host "${STATEMENTFLOW_API_HOST:-0.0.0.0}" \
    --port "$PORT" \
    --timeout-graceful-shutdown 10
