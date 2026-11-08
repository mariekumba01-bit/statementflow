# Exemple n8n — StatementFlow dans un workflow d'automatisation
#
# Deux intégrations gratuites, sans abonnement ni nœud payant :
#
# ── Option A : nœud HTTP Request vers l'API locale ─────────────────
# 1. Lancer l'API :  uvicorn statementflow.api:app --host 127.0.0.1 --port 8080 (mode ouvert local : export STATEMENTFLOW_ALLOW_OPEN_AUTH=1)
#    (si n8n tourne en Docker, utiliser host.docker.internal:8080)
# 2. Dans n8n :
#    - Trigger : Schedule (ex. quotidien) ou IMAP Email (pièce jointe relevé)
#    - Nœud "Read Binary File" / "Extract from File" : récupérer le PDF/CSV
#    - Nœud "HTTP Request" :
#        Method  : POST
#        URL     : http://127.0.0.1:8080/v1/analyze
#        Body    : multipart/form-data, champ "file" = données binaires
#        Headers : X-Currency-Hint: XOF (optionnel)
#    - Nœud "HTTP Request" (résultat) :
#        Method  : GET
#        URL     : http://127.0.0.1:8080/v1/results/{{ $json.job_id }}
#    - Nœud final : Google Sheets / Postgres / e-mail du comptable
#
# ── Option B : nœud Execute Command vers la CLI (le plus simple) ───
#    Nœud "Execute Command" :
#        statementflow parse /data/releves/entrant.pdf \
#          --outdir /data/json/ --quiet --currency-hint XOF
#    Puis "Read Binary File" sur /data/json/sorti.json
#
# ── Contrôle comptable automatisé ──────────────────────────────────
#    Après parsing, brancher un nœud IF sur :
#        {{ $json.checks.opening_closing_ok }}
#    false → notification (le relevé est incohérent, vérification humaine)
#    true  → import comptable
#
# JSON de workflow minimal (importable dans n8n) : voir
# examples/workflow-n8n-statementflow.json (nœud Execute Command).
#
# NOTE : aucune fonctionnalité n8n Cloud payante n'est requise ;
# l'API et la CLI tournent sur votre propre machine.
