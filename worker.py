import logging
import os
import time

from app import sync_market


logging.basicConfig(level=logging.INFO)
while True:
    try:
        logging.info('Market update: %s', sync_market())
    except Exception:
        logging.exception('Market update failed')
    time.sleep(max(300, int(os.getenv('REFRESH_SECONDS') or '900')))
