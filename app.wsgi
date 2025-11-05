import sys
import logging
import site

site.addsitedir('/var/www/venv/lib/python3.11/site-pachages')

sys.path.insert(0, '/var/www/html')

logging.basicConfig(stream=sys.stderr)

from app import app as application
