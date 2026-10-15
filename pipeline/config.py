"""Configuration from environment, optionally loaded from a .env file."""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file():
    for path in (os.environ.get("PIPELINE_ENV"), os.path.join(_ROOT, ".env"), "/etc/pipeline.env"):
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"'))
            return


_load_env_file()

DB_PATH = os.environ.get("PIPELINE_DB", os.path.join(_ROOT, "data", "pipeline.db"))
MOCK_HOST = os.environ.get("MOCK_HOST", "127.0.0.1")
MOCK_PORT = int(os.environ.get("MOCK_PORT", "7100"))
CLOUD_URL = os.environ.get("CLOUD_URL", "").rstrip("/")          # https://<ref>.supabase.co/functions/v1/pipeline
CLOUD_ANON_KEY = os.environ.get("CLOUD_ANON_KEY", "")            # publishable key (public)
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "")                # shared secret checked by the function
