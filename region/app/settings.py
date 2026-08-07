"""Region configuration. One image, three services, differing only by env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class RegionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MRFP_", extra="ignore")

    region_id: str = "unknown-region"
    region_zone: str = "unknown-zone"
    model_name: str = "mock-echo-1"

    # Simulated service latency. Without it the priority order is a config line
    # rather than a physical property, failover costs nothing, and the circuit
    # breaker has nothing to optimise.
    base_latency_ms: int = 15
    jitter_ms: int = 5

    admin_token: str = "dev-chaos"
    max_prompt_chars: int = 8192


settings = RegionSettings()
