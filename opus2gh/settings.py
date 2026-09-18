#!/usr/bin/env python3
"""
Settings manager untuk opus2gh — simpan/muat konfigurasi di settings.json.
"""

import json
import os
import sys
from pathlib import Path

if getattr(sys, 'frozen', False):
    # Berjalan sebagai exe → settings.json di samping exe
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

SETTINGS_FILE = BASE_DIR / 'settings.json'

DEFAULTS = {
    'github_token': '',
    'github_owner': '',
    'github_repo': '',
    'github_branch': 'main',
    'repo_dir': 'music',
    'max_results': 20,
}


def load() -> dict:
    """Muat settings, merge dengan defaults."""
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                settings.update(saved)
        except Exception:
            pass
    return settings


def save(settings: dict):
    """Simpan settings ke settings.json."""
    # Hanya simpan key yang dikenal
    clean = {k: settings.get(k, DEFAULTS.get(k)) for k in DEFAULTS}
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def get(key: str, default=None):
    """Ambil satu setting."""
    return load().get(key, default)
