# Guide pilote — démonstration technique à budget zéro

**Principe** : démonstration 100 % locale, aucun prospect contacté, aucun
document réel de tiers, aucune dépense. Ce guide rend la démonstration
reproductible et l'évaluation traçable, sans rien promettre de plus.

## 1. Procédure de démonstration locale (15 minutes)

```bash
# installation (une fois)
git clone https://github.com/mariekumba01-bit/statementflow.git
cd statementflow && pip install -e ".[dev,api,mcp]"

# démonstration CLI — relevé PDF synthétique
statementflow parse tests/data/corpus/SYNTH_fr_societegenerale_A.pdf \
    --output demo_sg.json --pretty
statementflow check demo_sg.json

# démonstration mobile money (export CSV Wave, devise XOF)
statementflow parse tests/data/corpus/SYNTH_csv_wave_v2_U.csv \
    --output demo_wave.json --pretty --currency-hint XOF

# démonstration PDF scanné (OCR) — montre les alertes et la confiance
statementflow parse tests/data/corpus/SYNTH_stress_scanned_T.pdf \
    --output demo_scan.json --pretty
```

Points à montrer pendant la démo :
1. chaque transaction porte `transaction_id`, `description`,
   `original_description`, `amount` signé et `trace` (page/méthode/ligne) ;
2. `validation.status` et `checks` (chaîne des soldes, règle d'ensemble) ;
3. les alertes codifiées : `ocr_used` sur le PDF scanné, `side_assumed_debit`
   sur les montants sans direction, `currency_not_identified` si la devise
   est absente — le moteur signale, il n'invente jamais ;
4. une date impossible (`31/02/2026`) ressort `date: null` + alerte
   `date_unparseable` — jamais convertie en une autre date.

## 2. Jeu de données de démonstration

Le corpus synthétique de 32 documents (`tests/data/corpus/`, inventaire :
`inventaire.csv`) couvre : PDF français (SG, BNP, La Poste, Crédit
Agricole…), PDF européens (DE, NL, ES, UK), UEMOA/FCFA, exports Wave /
Orange Money / MTN MoMo / Moov, un multi-pages 40 transactions, un PDF
scanné (OCR). **Tous ces documents sont fabriqués** (générateur interne,
métadonnées anonymes) : ils ne représentent aucun client et aucune banque
réelle. Ne jamais utiliser de document réel sans autorisation écrite.

## 3. Procédure d'évaluation pilote

1. Le partenaire pilote fournit 5 à 20 documents **dont il détient les
   droits**, par un canal convenu avec lui.
2. Chaque document est passé en CLI locale (`parse` puis `check`).
3. Pour chaque document, noter dans la fiche de retour (section 5) :
   nombre de transactions attendu vs extrait, dates/montants corrects,
   alertes déclenchées, verdict (utilisable / à vérifier / échec).
4. Les documents sources sont supprimés immédiatement après évaluation
   (section 7) ; seuls les JSON de résultats agrégés sont conservés si le
   partenaire l'accepte.
5. Aucune métrique issue du pilote n'est publiée sans accord écrit du
   partenaire, et jamais présentée comme une garantie générale.

## 4. Fiche de limites (à remettre telle quelle)

- Métriques publiées = corpus synthétique interne ; **aucune garantie sur
  des relevés réels** avant tests autorisés.
- Devise jamais inventée : document sans devise lisible → `null` + alerte.
- Date impossible → `null` + alerte (jamais de correction silencieuse).
- Montant positif sans signal de direction → débit **supposé** + alerte.
- OCR : confiance plafonnée, montants à vérifier (`ocr_low_confidence`).
- Formats de colonnes hors des gabarits testés → adapter
  (`docs/FORMATS.md`) ou signaler le cas.
- Lecture flottante des montants : précision garantie jusqu'à ~1e13
  d'unités (au-delà, une alerte de rupture apparaît — comportement sûr).
- Aucune certification RGPD/CDP : le pilote reste sous la responsabilité
  juridique du partenaire (voir `docs/SECURITY.md`).

## 5. Modèle de demande de test (à copier)

> Objet : demande de test StatementFlow (documents autorisés)
>
> Bonjour,
> Dans le cadre d'une évaluation technique de StatementFlow (outil de
> conversion de relevés en JSON vérifié), je sollicite votre accord pour
> tester l'outil sur un échantillon de 5 à 20 documents dont vous détenez
> les droits (relevés de vos propres comptes, ou de vos clients dans le
> cadre de votre mandat). Conditions : traitement 100 % local, aucun
> stockage, suppression immédiate après évaluation, résultats anonymisés
> si vous le souhaitez. Aucun document ne sera transmis à un tiers.
> Merci de confirmer par retour de mail votre accord et le canal
> d'envoi que vous préférez.

## 6. Modèle de retour utilisateur (à remplir par document)

```
Document (référence anonymisée) : ____________
Famille (PDF FR / PDF EU / UEMOA / mobile money / scanné) : ____________
Transactions attendues : ____   extraites : ____
Dates correctes : ____/____      Montants corrects : ____/____
Soldes corrects : ____/____      Devise détectée : ____
Alertes déclenchées : ______________________________________
Verdict : [ ] utilisable tel quel  [ ] utilisable avec vérification
          [ ] échec (décrire) : ____________________________________
Commentaire libre : _______________________________________________
```

## 7. Procédure de suppression des documents

1. Traitement en mémoire uniquement : rien à supprimer côté moteur ; le
   processus qui a lu les octets les libère en fin d'exécution.
2. Fichiers reçus d'un pilote : supprimer les originaux et les JSON de
   résultats de la machine dès la fiche de retour remplie
   (`shred -u <fichiers>` ou équivalent), puis vider la corbeille.
3. Si l'API a été utilisée : redémarrer le processus (les résultats en
   mémoire TTL sont intégralement perdus).
4. Si `STATEMENTFLOW_USAGE_STORE_PATH` est configuré : les compteurs ne
   contiennent aucun document, seulement des agrégats par hash de clé —
   supprimer le fichier pour purger l'historique d'usage.
5. Conserver uniquement la fiche de retour anonymisée, sans document.
