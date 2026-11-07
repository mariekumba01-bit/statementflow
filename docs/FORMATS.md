# Formats supportés

Tous les exemples ci-dessous sont des **modèles synthétiques** (préfixe `SYNTH_`, corpus `tests/data/corpus/`, nature déclarée dans `inventaire.csv`) : ils imitent les structures observées, ils ne sont pas des relevés réels.

## PDF bancaires

| Famille | Modèles testés | Caractéristiques couvertes |
|---|---|---|
| France | SG, BNP Paribas, Crédit Agricole, LCL, Banque Postale, Caisse d'Épargne, Crédit Mutuel, Boursorama, Fortuneo, Monabanq | colonnes Débit/Crédit séparées ou montant signé ; `1 234,56` avec espace insécable ; dates `JJ/MM/AAAA` (ou ISO chez Boursorama, `JJ/MM/AA` chez CE) ; multi-pages ; ligne TOTAL ; préambules |
| Europe | Deutsche Bank, ING, Santander, Barclays | `Soll/Haben`, `Af/Bij`, `Money Out/In` ; dates `JJ.MM.AAAA`, `JJ-MM-AAAA` ; montants `1.234,56` ; signe suffixe espagnol `1.234,56-` ; GBP |
| UEMOA | Ecobank SN, BOA SN | `1 234 567 XOF` / FCFA entiers ; entêtes sans accents ; multi-pages avec « (suite) » |

## PDF scannés (OCR)

Repli automatique tesseract (300 dpi) quand une page n'a pas de couche texte. Fusion des fragments de milliers (« 3 » + « 105,54 » → « 3 105,54 »), confiance abaissée, alertes `ocr_used` / `ocr_low_confidence`. Limite assumée : un scan de très mauvaise qualité peut produire des montants erronés — d'où les contrôles de chaîne des soldes.

## CSV / TSV / XLSX (banques et mobile money)

| Source | Modèle | Points clés |
|---|---|---|
| Wave / Wave Business | `SYNTH_csv_wave_v2_U`, `SYNTH_csv_wave_bom_semicolon_Y` | FCFA entiers, colonne Sens (RECEPTION/ENVOI), `;` ou `,`, BOM UTF-8, parenthèses |
| Orange Money | `SYNTH_csv_orange_money_V` | montant signé, datetime ISO, **aucune devise dans le contenu → currency_hint** |
| MTN MoMo | `SYNTH_csv_mtn_momo_W` | `Transaction ID` ignoré, Amount signé |
| Moov | `SYNTH_csv_moov_X.csv` | Débit/Crédit séparés |
| Banque (Excel) | `SYNTH_xlsx_banque_Z` | 2 lignes de préambule, entête ligne 3 |
| Divers | quoted-thousands, accounting-parentheses (DE), multi-devises (colonne Devise), préambule 3 lignes, TSV ISO datetime, suffixes `XOF` | pièges volontaires du corpus |

## Comportements volontaires (pas des échecs)

| Situation | Comportement du moteur |
|---|---|
| Devise absente du contenu (ex. CSV Orange Money) | `currency: null` + alerte `currency_not_identified` — **jamais inventée** ; `--currency-hint XOF` la fournit |
| Date impossible (`33/07/2026`) | transaction conservée avec `date: null` + alerte `date_unparseable`, confiance abaissée |
| Montant positif sans colonne Débit/Crédit ni signal de sens | débit supposé + avertissement `side_assumed_debit` |
| Document dont la chaîne des soldes est incohérente | alertes `chain_break` / `unbalanced_statement`, confiance ≤ 0,5 — le moteur signale, il ne corrige pas silencieusement |

## Ajouter un format

Voir [`ARCHITECTURE.md`](ARCHITECTURE.md) § « Ajouter un format bancaire » : adaptateur + corpus + seuils figés. Aucune banque n'exige aujourd'hui de parser dédié au-delà du moteur générique ; les adaptateurs existants (`wave-csv`, `orange-money-csv`, `mtn-momo-csv`) servent de modèle.
