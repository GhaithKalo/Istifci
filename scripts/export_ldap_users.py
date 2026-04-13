#!/usr/bin/env python3
"""Export LDAP users to CSV.

Safeguards:
- By default this script will NOT include the `userPassword` attribute.
- To export `userPassword` (hashes or plaintext if your server stores them), you must both pass
  --include-passwords and set the environment variable ALLOW_PASSWORD_EXPORT=1.

Usage examples:
  python scripts/export_ldap_users.py --host 172.28.1.103 --port 3890 \
    --bind-dn 'cn=admin,dc=example,dc=com' --bind-pass secret \
    --base-dn 'dc=example,dc=com' --out-file users.csv

  # To export password attribute (explicit guard):
  export ALLOW_PASSWORD_EXPORT=1
  python scripts/export_ldap_users.py --include-passwords ...

"""
from __future__ import annotations
import os
import csv
import argparse
import sys
from ldap3 import Server, Connection, ALL, SUBTREE, Tls


def parse_args():
    p = argparse.ArgumentParser(description='Export LDAP users to CSV')
    p.add_argument('--host', default='172.28.1.103', help='LDAP host')
    p.add_argument('--port', default=3890, type=int, help='LDAP port')
    p.add_argument('--use-ssl', action='store_true', help='Use LDAPS (SSL)')
    p.add_argument('--use-starttls', action='store_true', help='StartTLS after connect')
    p.add_argument('--bind-dn', required=True, help='Bind DN or user (admin)')
    p.add_argument('--bind-pass', required=True, help='Bind password')
    p.add_argument('--base-dn', required=True, help='Search base DN')
    p.add_argument('--filter', default='(&(objectClass=person)(|(uid=*)(sAMAccountName=*)))',
                   help='LDAP search filter')
    p.add_argument('--attributes', nargs='+', default=['dn', 'cn', 'uid', 'sAMAccountName', 'mail'],
                   help='Attributes to fetch (default: common ones)')
    p.add_argument('--include-passwords', action='store_true', help='Also fetch userPassword attribute')
    p.add_argument('--size-limit', type=int, default=0, help='Max number of entries to fetch (0 = unlimited)')
    p.add_argument('--out-file', default='ldap_users.csv', help='CSV output file')
    return p.parse_args()


def main():
    args = parse_args()

    if args.include_passwords and os.environ.get('ALLOW_PASSWORD_EXPORT') != '1':
        print('Refusing to export password attribute: set ALLOW_PASSWORD_EXPORT=1 to confirm.', file=sys.stderr)
        sys.exit(2)

    attrs = list(args.attributes)
    if args.include_passwords and 'userPassword' not in attrs:
        attrs.append('userPassword')

    # Ensure DN (entry_dn) is always present
    if 'dn' not in attrs:
        attrs.insert(0, 'dn')

    server = Server(args.host, port=args.port, use_ssl=args.use_ssl, get_info=ALL)

    try:
        conn = Connection(server, user=args.bind_dn, password=args.bind_pass, auto_bind=True)
    except Exception as e:
        print('Failed to bind to LDAP server:', e, file=sys.stderr)
        sys.exit(1)

    try:
        conn.search(
            search_base=args.base_dn,
            search_filter=args.filter,
            search_scope=SUBTREE,
            attributes=attrs,
            size_limit=args.size_limit
        )
    except Exception as e:
        print('LDAP search failed:', e, file=sys.stderr)
        conn.unbind()
        sys.exit(1)

    entries = conn.entries
    # Prepare CSV header
    # Map ldap3 entry attributes to simple string values
    header = ['dn'] + [a for a in attrs if a != 'dn']

    with open(args.out_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for entry in entries:
            row = []
            # DN
            row.append(entry.entry_dn)
            for a in header[1:]:
                # If attribute missing, write empty
                try:
                    val = entry[a].value
                except Exception:
                    val = ''
                # If bytes (e.g., userPassword), represent safely
                if isinstance(val, (bytes, bytearray)):
                    # Write hex for binary data
                    val = val.hex()
                writer.writerow([entry.entry_dn] + [entry[attr].value if attr in entry else '' for attr in header[1:]])

    conn.unbind()
    print(f'Wrote {len(entries)} entries to {args.out_file}')


if __name__ == '__main__':
    main()
