"""Demo Provider Adapter.

This module is a *Demo* provider. It does not contact any external
service. It is shipped as an example implementation of the
Provider Adapter contract and as a template for new providers.

DO NOT mistake this for a real provider. It will not produce useful
language-model output; it only echoes the request.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class AdapterRequest:
    prompt: str
    model: str = "demo-model"
    max_tokens: int = 64
    task_id: Optional[str] = None


@dataclass
class AdapterResponse:
    status: str  # "ok" | "rate_limited" | "budget_exceeded" | "error"
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None


class DemoProvider:
    """Demo Provider. Disabled by default. No external network calls."""

    name = "demo_provider"
    enabled = False

    def invoke(self, request: AdapterRequest) -> AdapterResponse:
        if not self.enabled:
            return AdapterResponse(
                status="error",
                error="demo_provider is disabled; enable explicitly in features.toml",
            )
        # Deterministic echo + a fake usage block.
        h = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()[:8]
        text = (
            f"[demo_provider echo model={request.model} sha={h} "
            f"prompt_len={len(request.prompt)}]"
        )
        return AdapterResponse(
            status="ok",
            text=text,
            input_tokens=max(1, len(request.prompt) // 4),
            output_tokens=max(1, len(text) // 4),
        )

    def health(self) -> dict:
        return {"status": "ok", "provider": self.name, "enabled": self.enabled}

    def shutdown(self) -> None:
        # No resources to release.
        return None


if __name__ == "__main__":
    p = DemoProvider()
    p.enabled = True
    resp = p.invoke(AdapterRequest(prompt="hello, world"))
    print(resp)