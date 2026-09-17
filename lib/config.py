"""Загрузка конфигурации: .env, benchmarks.yaml, clients/*/config.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(
            f"Не задана переменная {name}. Скопируй .env.example в .env и заполни её."
        )
    return value


@lru_cache(maxsize=1)
def benchmarks() -> dict[str, Any]:
    with open(ROOT / "config" / "benchmarks.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=None)
def client_config(client_code: str) -> dict[str, Any]:
    path = ROOT / "clients" / client_code / "config.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def all_client_configs() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    clients_dir = ROOT / "clients"
    if not clients_dir.exists():
        return out
    for child in sorted(clients_dir.iterdir()):
        if (child / "config.yaml").exists():
            out[child.name] = client_config(child.name)
    return out


def db_url() -> str | None:
    """Строка подключения к Postgres. None — значит БД ещё не подключена,
    и скрипты работают в файловом режиме (сырьё в JSONL)."""
    return env("SUPABASE_DB_URL") or env("DATABASE_URL")


def data_dir() -> Path:
    path = Path(env("ATS_PARSER_DATA_DIR") or (ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspaces() -> list[dict[str, str]]:
    """Воркспейсы Instantly. Пока БД не подключена, список берётся из config/workspaces.yaml,
    а при подключённой БД его можно перевести на core.workspace без правки вызывающего кода."""
    path = ROOT / "config" / "workspaces.yaml"
    if not path.exists():
        return [{"code": "default", "name": "default", "env_key": "INSTANTLY_API_KEY"}]
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return [w for w in data.get("workspaces", []) if w.get("active", True)]
