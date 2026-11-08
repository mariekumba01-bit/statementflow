"""CLI StatementFlow.

    statementflow parse releve.pdf --output result.json
    statementflow parse exports/ --format json --outdir out/
    statementflow check result.json        # contrôles seuls
    statementflow keys create              # génère une clé d'API + son hash
    statementflow keys hash                # empreinte SHA-256 d'une clé
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import Config
from .engine import ParseError, parse_file
from .security import generate_key, hash_key


def _emit(result: dict, args) -> None:
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            if args.pretty:
                json.dump(result, f, ensure_ascii=False, indent=2)
            else:
                json.dump(result, f, ensure_ascii=False)
        return
    json.dump(result, sys.stdout, ensure_ascii=False,
              indent=2 if args.pretty else None)
    sys.stdout.write('\n')


def _summary(result: dict) -> str:
    d = result['document']
    lines = [
        f"{d['filename']} : {len(result['transactions'])} transactions "
        f"({d['kind']}, {d['pages']} page(s), méthode {d['method']})",
        f"  devise={result['currency']} ouverture={result['opening_balance']} "
        f"clôture={result['closing_balance']} confiance={result['confidence']}",
        f"  contrôles : chaîne {result['checks']['chain_matches']}/"
        f"{result['checks']['chain_checked']} · ensemble "
        f"{result['checks']['opening_closing_ok']}",
    ]
    alerts = result.get('alerts') or []
    n_err = sum(1 for a in alerts if a['severity'] == 'error')
    n_warn = sum(1 for a in alerts if a['severity'] == 'warning')
    lines.append(f'  alertes : {n_err} erreur(s), {n_warn} avertissement(s)')
    return '\n'.join(lines)


def cmd_parse(args) -> int:
    config = Config.from_env()
    paths: list[str] = []
    if os.path.isdir(args.path):
        exts = ('.pdf', '.csv', '.tsv', '.xlsx', '.txt')
        paths = sorted(os.path.join(args.path, f)
                       for f in os.listdir(args.path)
                       if f.lower().endswith(exts)
                       and not f.startswith('.'))
        if not paths:
            print(f'Aucun document trouvé dans {args.path}', file=sys.stderr)
            return 1
    else:
        paths = [args.path]
    rc = 0
    outdir = args.outdir
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    for p in paths:
        try:
            result = parse_file(p, config,
                                currency_hint=args.currency_hint or None)
        except ParseError as e:
            print(f'ERREUR {p} : {e}', file=sys.stderr)
            rc = 1
            continue
        if outdir:
            base = os.path.splitext(os.path.basename(p))[0]
            args.output = os.path.join(outdir, base + '.json')
        _emit(result, args)
        if args.quiet:
            continue
        print(_summary(result), file=sys.stderr)
    return rc


def cmd_check(args) -> int:
    with open(args.path, encoding='utf-8') as f:
        result = json.load(f)
    print(json.dumps({'checks': result.get('checks'),
                      'validation': result.get('validation'),
                      'alerts': result.get('alerts'),
                      'confidence': result.get('confidence')},
                     ensure_ascii=False, indent=2))
    ok = result.get('checks', {}).get('opening_closing_ok')
    return 0 if ok in (True, None) else 2


def cmd_keys_create(args) -> int:
    """Génère des clés d'API : la clé en clair (à transmettre au client,
    affichée UNE SEULE FOIS) et son hash SHA-256 (à configurer côté serveur
    dans STATEMENTFLOW_API_KEY_HASHES)."""
    print('Clé (affichée UNE SEULE FOIS — stockez-la dans votre gestionnaire '
          'de secrets)          → Hash SHA-256 (STATEMENTFLOW_API_KEY_HASHES)')
    for _ in range(args.count):
        key = generate_key()
        print(f'{key}\n  → {hash_key(key)}')
    print('\nRotation : ajoutez le nouveau hash à STATEMENTFLOW_API_KEY_HASHES '
          '(virgule), poussez, puis retirez l\'ancien hash pour révoquer.')
    return 0


def cmd_keys_hash(args) -> int:
    """Empreinte SHA-256 d'une clé existante. Sans argument, lit la clé
    sur stdin (évite de laisser la clé dans l'historique du shell)."""
    key = args.key
    if not key:
        print('Clé (stdin) : ', file=sys.stderr, end='')
        key = sys.stdin.readline().strip()
    if not key:
        print('ERREUR : clé vide', file=sys.stderr)
        return 1
    print(hash_key(key))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='statementflow',
        description='Transforme des relevés bancaires et exports mobile money '
                    'en transactions JSON normalisées et vérifiées.')
    p.add_argument('--version', action='version', version=__version__)
    sub = p.add_subparsers(dest='command', required=True)

    sp = sub.add_parser('parse', help='analyser un document (ou un dossier)')
    sp.add_argument('path', help='fichier PDF/CSV/TSV/XLSX ou dossier')
    sp.add_argument('-o', '--output', help='écrire le JSON dans ce fichier')
    sp.add_argument('--outdir', help='dossier de sortie (mode dossier)')
    sp.add_argument('--pretty', action='store_true', help='JSON indenté')
    sp.add_argument('--quiet', action='store_true', help='résumé sur stderr omis')
    sp.add_argument('--currency-hint', dest='currency_hint', default='',
                    help='devise si elle est absente du document (ex. XOF)')
    sp.set_defaults(func=cmd_parse)

    sc = sub.add_parser('check', help='afficher les contrôles d’un résultat JSON')
    sc.add_argument('path', help='fichier JSON produit par statementflow')
    sc.set_defaults(func=cmd_check)

    sk = sub.add_parser('keys', help='gestion des clés d’API (côté serveur)')
    keys_sub = sk.add_subparsers(dest='keys_command', required=True)
    kc = keys_sub.add_parser(
        'create', help='générer une clé aléatoire + son hash SHA-256')
    kc.add_argument('--count', type=int, default=1,
                    help='nombre de clés à générer (défaut 1)')
    kc.set_defaults(func=cmd_keys_create)
    kh = keys_sub.add_parser(
        'hash', help='empreinte SHA-256 d’une clé (stdin si non fournie)')
    kh.add_argument('key', nargs='?', default='',
                    help='clé en clair (omise : lue sur stdin, recommandé)')
    kh.set_defaults(func=cmd_keys_hash)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
