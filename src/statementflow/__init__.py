"""StatementFlow — moteur de conversion de relevés financiers en JSON.

PDF (natifs ou scannés), CSV et XLSX → transactions structurées avec
dates normalisées, débits/crédits séparés, devises détectées quand elles
sont identifiables, soldes d'ouverture/clôture, contrôles de cohérence
comptable et alertes sur les données incertaines.

v0.2.0 : validation calendaire stricte des dates, contrôles en Decimal,
identifiants stables de document/transaction, statut de validation en
quatre niveaux (validated/warning/inconsistent/unverifiable), API avec
authentification obligatoire par clé (hashée côté serveur) et
administration séparée.

Usage minimal :

    from statementflow import parse_file
    resultat = parse_file('releve.pdf')

Avertissement : les métriques publiées (corpus synthétique de test) ne
sont pas une garantie de performance sur des relevés réels.
"""
from .config import Config
from .engine import ParseError, parse_bytes, parse_file

__version__ = '0.6.0'
__all__ = ['parse_file', 'parse_bytes', 'ParseError', 'Config', '__version__']
