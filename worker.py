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
    default_interval = '900' if os.getenv('ALPHA_VANTAGE_API_KEY') else '3600'
    time.sleep(max(300, int(os.getenv('REFRESH_SECONDS') or default_interval)))
