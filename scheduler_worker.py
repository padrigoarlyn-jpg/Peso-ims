"""Dedicated scheduler process; do not run this alongside a web scheduler.

Set ENABLE_SCHEDULER=false on all web instances and start this process once.
"""
import os
import time

os.environ["ENABLE_SCHEDULER"] = "true"
from website import create_app

app = create_app()
app.logger.info("PESO dedicated scheduler running")
try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    app.logger.info("PESO dedicated scheduler stopped")
