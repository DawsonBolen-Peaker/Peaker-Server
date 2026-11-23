"""config.json in the docs_db folder: the lists Claude picks from.

    {"doc_types": [...], "departments": [...], "manufacturers": [...], "categories": [...]}

The first run copies server/db/config.default.json there. New categories
Claude comes up with are added to "categories" automatically.
"""
import json, os, shutil, threading
from system.paths import CONFIG_PATH, DEFAULT_CONFIG_PATH

_lock = threading.Lock()


def ensure_config() -> None:
    if not CONFIG_PATH.is_file():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_CONFIG_PATH, CONFIG_PATH)
        print("Created", CONFIG_PATH)


def load_config() -> dict:
    ensure_config()
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def config_list(key: str) -> list[str]:
    """Non-blank entries of one list in config.json, in order."""
    return [s.strip() for s in load_config().get(key) or [] if isinstance(s, str) and s.strip()]


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def track_new_categories(cats: list[str]) -> None:
    with _lock:
        cfg = load_config()
        categories = cfg.setdefault("categories", [])
        existing = {c.strip().lower() for c in categories}
        added = False
        for c in cats:
            c = c.strip().lower()
            if c and c not in existing:
                categories.append(c)
                existing.add(c)
                added = True
        if added:
            save_config(cfg)
