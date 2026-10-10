"""The Vault (Group 70): fleet credentials, bookmaker accounts and the runtime fleet configuration.

    catalog.py           bookmaker / provider / field / sport names, fuzzy-matched
    markdown_importer.py the resilient markdown parser, the import (encrypt + idempotent upsert) and its CLI
    registry.py          masked views, manual edits, execution credentials, the encrypted backup
    fleet_config.py      the runtime overlay (sports, markets per sport, currencies), DB -> Redis -> every process
    account_rotator.py   which account carries an order, and the stake held on it until it settles
"""
