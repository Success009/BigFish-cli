"""Split out of main.py (unchanged logic)."""
import os, json

CLI_CONFIG_FILE = os.path.expanduser("~/gemini_local_agent/.cli_config.json")

def load_cli_config():
    if os.path.exists(CLI_CONFIG_FILE):
        try:
            with open(CLI_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cli_config(updates):
    try:
        cfg = load_cli_config()
        cfg.update(updates)
        os.makedirs(os.path.dirname(CLI_CONFIG_FILE), exist_ok=True)
        with open(CLI_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
