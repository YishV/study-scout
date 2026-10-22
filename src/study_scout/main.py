import logging
from pathlib import Path

from .config import Settings
from .storage import Store
from .web.app import create_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
settings = Settings.from_env()
store = Store(Path('data/study-scout.sqlite3'))
store.init()
app = create_app(settings, store)
