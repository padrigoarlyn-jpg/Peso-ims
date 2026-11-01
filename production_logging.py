"""Opt-in production logging setup. Call configure_production_logging(app) once.

Logs error metadata and stack traces, not request bodies or authentication tokens.
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_production_logging(app):
    if os.getenv('APP_ENV', 'development').lower() != 'production':
        return
    log_dir = Path(os.getenv('LOG_DIR', './private_logs')).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        log_dir.chmod(0o700)
    log_path = log_dir / 'peso_ims_errors.log'
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', '') == str(log_path) for h in app.logger.handlers):
        handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
        handler.setLevel(logging.ERROR)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
