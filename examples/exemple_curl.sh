# Exemple curl — API StatementFlow

# 1. Démarrer l'API (terminal 1) :
#    uvicorn statementflow.api:app --host 127.0.0.1 --port 8080

# 2. Santé :
curl -s http://127.0.0.1:8080/health

# 3. Analyser un relevé (PDF natif) :
JOB=$(curl -s -X POST http://127.0.0.1:8080/v1/analyze \
  -F "file=@tests/data/corpus/SYNTH_fr_societegenerale_A.pdf" \
  -H "X-API-Key: demo" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# 4. Récupérer le résultat JSON complet :
curl -s http://127.0.0.1:8080/v1/results/$JOB | python3 -m json.tool | head -40

# 5. Export mobile money sans devise lisible → indice explicite :
curl -s -X POST http://127.0.0.1:8080/v1/analyze \
  -F "file=@tests/data/corpus/SYNTH_csv_orange_money_V.csv" \
  -H "X-Currency-Hint: XOF"

# 6. Compteurs d'usage de la clé :
curl -s http://127.0.0.1:8080/v1/usage -H "X-API-Key: demo"

# 7. Documentation OpenAPI interactive :
#    ouvrir http://127.0.0.1:8080/docs dans un navigateur
