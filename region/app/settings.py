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
    #
    # `base_latency_ms` is the fixed part — time to first token, dominated by
    # network distance, which is why it differs per region.
    base_latency_ms: int = 15
    jitter_ms: int = 5

    # The variable part. Real generation costs TTFT + n x inter-token latency,
    # so a response's cost scales with how much was asked for. A flat sleep
    # makes a 512-token request cost exactly what an 8-token one does, which is
    # precisely why a large read timeout had nothing behind it: no measurement
    # of this stack could ever justify one. Same value across regions on
    # purpose — inter-token latency is a property of the accelerator, not of
    # the distance to it.
    per_token_ms: float = 0.25

    # Streaming's inter-token gap is configured separately from `per_token_ms`
    # because the demo deliberately runs a compressed profile: a stream that
    # emits its whole body in 2 ms cannot be caught mid-flight by a chaos
    # injection, so `truncate` would have nothing to truncate.
    stream_inter_token_ms: float = 5.0

    # --- optional real upstream -------------------------------------------
    # Set MRFP_UPSTREAM_BASE_URL and this region stops faking and forwards to a
    # real OpenAI-compatible endpoint. This is what lets `make calibrate`
    # measure a real model's latency distribution instead of a mock's, which is
    # the only way the derived timeouts stop being a property of this repo.
    upstream_base_url: str = ""
    upstream_model: str = "gpt-4o-mini"
    upstream_api_key: str = ""
    # Deliberately generous: this bounds the REGION's patience with the model,
    # not the proxy's patience with the region. The proxy's own budget is the
    # thing being calibrated and must stay the tighter of the two.
    upstream_timeout_s: float = 120.0

    admin_token: str = "dev-chaos"
    max_prompt_chars: int = 8192

    @property
    def upstream_enabled(self) -> bool:
        return bool(self.upstream_base_url)


settings = RegionSettings()
