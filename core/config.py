"""Typed config loading: config.yaml (business config) + .env (secrets)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class KronosConfig(BaseModel):
    model: str
    tokenizer: str
    device: str
    lookback: int
    mc_samples: int


class ApifyConfig(BaseModel):
    actor: str


class RiskConfig(BaseModel):
    bankroll_paper: float
    kelly_multiplier: float
    f_max: float
    fee: float


class LlmConfig(BaseModel):
    provider: str
    model: str


class AppConfig(BaseModel):
    assets: list[str]
    symbols: dict[str, str]
    horizon: str
    ohlcv_interval: str
    ohlcv_limit: int
    kronos: KronosConfig
    apify: ApifyConfig
    risk: RiskConfig
    llm: LlmConfig
    venues: list[str]
    mode: str
    db_path: str


class Settings(BaseModel):
    apify_token: str | None = None
    openrouter_api_key: str | None = None


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)


def load_settings(env_path: str | Path = ".env") -> Settings:
    load_dotenv(env_path, override=False)
    return Settings(
        apify_token=os.getenv("APIFY_TOKEN") or None,
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
    )
