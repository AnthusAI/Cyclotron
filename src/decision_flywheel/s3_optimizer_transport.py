"""Durable S3 paid-call reservation around existing optimizer transports.

Every intentional optimizer call is a new authorized generation, including equal
prompts. A pending/uncertain generation cannot be repeated by another call. No
new provider client, credential, background retry or thread is introduced.
"""

import asyncio
import inspect
from dataclasses import asdict
from hashlib import sha256
from uuid import uuid4

from .batch_storage import BatchInProgress
from .journal import decode, encode
from .optimizer_agent import OptimizerReply
from .s3_batch_storage import S3BatchStorage


class S3OptimizerTransport:
    def __init__(
        self, transport, storage, *, identity, max_calls, max_request_bytes, max_reply_bytes, redact=(), execution_guard=None
    ):
        if not isinstance(storage, S3BatchStorage):
            raise ValueError("canonical S3 optimizer budget required")
        if (
            not isinstance(identity, dict)
            or not {"provider", "model"} <= set(identity)
            or set(identity) - {"provider", "model", "settings"}
            or any(not isinstance(identity[k], str) or not identity[k].strip() for k in ("provider", "model"))
        ):
            raise ValueError("explicit complete optimizer transport identity required")
        if any(type(v) is not int or v < 1 for v in (max_calls, max_request_bytes, max_reply_bytes)):
            raise ValueError("explicit optimizer call/request/reply budgets required")
        if getattr(transport, "model", identity["model"]) != identity["model"]:
            raise ValueError("optimizer identity differs from configured provider model")
        if execution_guard is not None and not callable(execution_guard):
            raise ValueError("trusted invocation guard required")
        self.execution_guard = execution_guard
        self.transport = transport
        self.storage = storage
        self.identity = decode(encode(identity))
        self.max_calls = max_calls
        self.model = identity["model"]
        self.max_request_bytes = max_request_bytes
        self.max_reply_bytes = max_reply_bytes
        self.redact = tuple(redact)
        candidate = getattr(transport, "acomplete", None)
        if not inspect.iscoroutinefunction(candidate):
            candidate = (
                transport
                if inspect.iscoroutinefunction(transport) or inspect.iscoroutinefunction(type(transport).__call__)
                else None
            )
        self._async = candidate
        if candidate is not None:
            self.acomplete = self._acomplete
        self._sync = (
            transport
            if callable(transport)
            and not inspect.iscoroutinefunction(transport)
            and not inspect.iscoroutinefunction(type(transport).__call__)
            else None
        )

    def with_execution_guard(self, guard):
        return S3OptimizerTransport(
            self.transport,
            self.storage,
            identity=self.identity,
            max_calls=self.max_calls,
            max_request_bytes=self.max_request_bytes,
            max_reply_bytes=self.max_reply_bytes,
            redact=self.redact,
            execution_guard=guard,
        )

    def _authorize(self):
        if self.execution_guard is not None:
            result = self.execution_guard()
            if result is not None:
                if inspect.iscoroutine(result):
                    result.close()
                raise ValueError("blocking worker guard must return None or raise")

    @property
    def calls(self):
        return self.storage.requests()

    def _safe(self, content, maximum):
        if len(content) > maximum:
            raise ValueError("optimizer request/reply exceeds byte budget")
        if any(secret and secret.encode() in content for secret in self.redact):
            raise ValueError("optimizer request/reply contains a credential")

    def _begin(self, messages):
        content = encode({"transport": self.identity, "messages": messages})
        self._safe(content, self.max_request_bytes)
        if not isinstance(messages, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("role"), str) or not isinstance(row.get("content"), str)
            for row in messages
        ):
            raise ValueError("structured optimizer messages required")
        fingerprint = sha256(content).hexdigest()
        owner = str(uuid4())
        self.storage.reserve(fingerprint, self.max_calls, owner, policy="refresh")
        if not self.storage.claim(fingerprint, owner):
            raise BatchInProgress("optimizer request already claimed; explicit reconciliation required")
        return fingerprint, owner

    def _finish(self, claim, reply):
        if not isinstance(reply, OptimizerReply):
            raise ValueError("optimizer transport must return OptimizerReply")
        raw = encode(asdict(reply))
        self._safe(raw, self.max_reply_bytes)
        # Transport/serialization/settlement exceptions leave the claimed request
        # protected. The caller cannot automatically repay an uncertain outcome.
        self.storage.settle(claim[0], claim[1], "complete", raw.decode())
        return reply

    def __call__(self, messages):
        if self._sync is None:
            raise TypeError("asynchronous optimizer transport must be awaited")
        self._authorize()
        claim = self._begin(messages)
        self._authorize()
        return self._finish(claim, self._sync(messages))

    async def _acomplete(self, messages):
        await asyncio.to_thread(self._authorize)
        claim = self._begin(messages)
        await asyncio.to_thread(self._authorize)
        return self._finish(claim, await self._async(messages))


def require_durable_optimizer(optimizer):
    if (
        optimizer is not None
        and getattr(optimizer, "enabled", True)
        and not isinstance(getattr(optimizer, "complete", None), S3OptimizerTransport)
    ):
        raise ValueError("cloud optimization requires a canonical S3 paid-call transport")
