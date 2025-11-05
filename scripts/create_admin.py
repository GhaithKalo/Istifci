#!/usr/bin/env python3
"""
Create or update an admin user for the application.

Usage:
  ./scripts/create_admin.py --username admin [--password PWD] [--yes]

- If --password is not provided a secure random password will be generated and printed once.
- By default the script will ask for confirmation before making DB changes; use --yes to skip
  the prompt for automation.

Note: Run this on the server where the app and its virtualenv are available.
"""
import argparse
import getpass
import secrets
import sys
import os

# Ensure project root is on path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import app, db
from models import User


def create_or_update_admin(username: str, password: str, auto_confirm: bool = False):
    with app.app_context():
        existing = User.query.filter_by(username=username).first()
        if existing:
            print(f"User '{username}' already exists.")
            if not auto_confirm:
                resp = input(f"Do you want to update the password for '{username}'? [y/N]: ")
                if resp.lower() not in ('y', 'yes'):
                    print("Aborting: password not changed.")
                    return
            existing.set_password(password)
            db.session.commit()
            print(f"Password for '{username}' updated.")
        else:
            if not auto_confirm:
                resp = input(f"Create new admin user '{username}'? [y/N]: ")
                if resp.lower() not in ('y', 'yes'):
                    print("Aborting: no user created.")
                    return
            u = User(username=username, password=password, role='admin')
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            print(f"Admin user '{username}' created.")

    # Print the password once (operator should store it somewhere secure)
    print('\nIMPORTANT: The password is shown below only once. Store it securely.')
    print('username:', username)
    print('password:', password)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Create or update an admin user for the app')
    parser.add_argument('--username', '-u', default='admin', help='Admin username (default: admin)')
    parser.add_argument('--password', '-p', help='Password to set (if omitted, a secure one is generated)')
    parser.add_argument('--yes', action='store_true', help='Skip interactive confirmation')

    args = parser.parse_args()

    if not args.password:
        pwd = secrets.token_urlsafe(24)
    else:
        pwd = args.password

    create_or_update_admin(args.username, pwd, auto_confirm=args.yes)
