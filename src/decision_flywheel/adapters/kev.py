"""Optional HTTP adapter for Kev's TypeSafe-compatible System One server."""
from __future__ import annotations

from typing import Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities
from .system_one import SystemOneAdapter


class KevAdapter:
    """Call a local Kev server at its documented `/v1/systemone` endpoint."""
    name = "kev"
    capabilities = ModelCapabilities(
        supports_labeled_context=True, supports_probability_distributions=True
    )

    def __init__(self, base_url: str = "http://127.0.0.1:8009"):
        self.base_url = base_url.rstrip("/")

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        self.capabilities.validate_context(context)
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[kev] to call a Kev server.") from error

        class Client:
            async def system_one(_, *, state, questions):
                async with httpx.AsyncClient() as client:
                    response = await client.post(f"{self.base_url}/v1/systemone", json={"state": state, "questions": questions})
                    response.raise_for_status()
                    payload = response.json()
                return type("KevResponse", (), {"answers": payload.get("answers", {}), "model": payload.get("model"), "usage": payload.get("usage")})()
        return await SystemOneAdapter(Client(), name=self.name, capabilities=self.capabilities).decide(task, target, context)
