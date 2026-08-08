"""
Cloud WebSocket client.

Maintains a long-lived connection to the PluginCoordinator DO. On every
connect:
    1. Load session private key from keychain.
    2. Send `hello` with `last_seen_seq` (persisted in the offline queue).
    3. Receive `resume_ack` with any pending instructions.
    4. Dispatch each instruction to `hooks.cloud.dispatch`.
    5. Sign the resulting receipt and send it back.

Failures reconnect with capped exponential backoff. All work is idempotent
under `(task_id, instruction_id)`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

from .config import Config
from .crypto import load_private_key_seed, sign_receipt
from . import keychain
from . import queue as _queue

logger = logging.getLogger("dynos.cloud.client")


@dataclass(slots=True)
class ClientState:
    task_id: str
    session_key_fingerprint: str
    config: Config


DispatchFn = Callable[[dict[str, Any], ClientState], Awaitable[dict[str, Any]]]


def _msg_id() -> str:
    return "m_" + uuid.uuid4().hex[:12]


async def run_client(
    state: ClientState,
    dispatch_fn: DispatchFn,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run until stop_event is set. Reconnects on disconnect."""
    import websockets

    backoff = 1.0
    stop_event = stop_event or asyncio.Event()

    ws_base = state.config.derived_ws_url.rstrip("/")
    url = f"{ws_base}/v1/tasks/{state.task_id}/ws"

    jwt = keychain.get_secret(keychain.SESSION_JWT)
    seed_b64 = keychain.get_secret(keychain.SESSION_PRIVATE_KEY)
    pub_b64 = keychain.get_secret(keychain.SESSION_PUBLIC_KEY)
    if not jwt or not seed_b64 or not pub_b64:
        raise RuntimeError("session not provisioned — run `dynos login` first")
    priv = load_private_key_seed(base64.b64decode(seed_b64))

    queue = _queue.OfflineQueue(state.config.offline_queue_path)
    last_seen_seq = queue.last_seen_seq(state.task_id)

    while not stop_event.is_set():
        try:
            async with websockets.connect(  # type: ignore[attr-defined]
                url,
                additional_headers={"Authorization": f"Bearer {jwt}"},
                open_timeout=15,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                hello = {
                    "v": 1,
                    "type": "hello",
                    "msg_id": _msg_id(),
                    "task_id": state.task_id,
                    "client_version": "0.1.0",
                    "session_pubkey_b64": pub_b64,
                    "last_seen_seq": last_seen_seq,
                }
                await ws.send(json.dumps(hello))
                logger.info("sent hello last_seen_seq=%d", last_seen_seq)

                backoff = 1.0  # reset on successful connect

                async for raw in ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("received non-JSON frame")
                        continue

                    ftype = frame.get("type")
                    if ftype == "resume_ack":
                        pending = frame.get("pending_instructions", [])
                        logger.info("resume_ack next_seq=%s pending=%d", frame.get("next_seq"), len(pending))
                        for inst in pending:
                            await _handle_instruction(ws, inst, state, dispatch_fn, priv, queue)
                    elif ftype == "instruction":
                        inst = frame.get("instruction", {})
                        await _handle_instruction(ws, inst, state, dispatch_fn, priv, queue)
                    elif ftype == "pong":
                        pass
                    elif ftype == "abort":
                        logger.info("abort received: %s", frame.get("reason"))
                        await ws.close(code=1000)
                        return
                    elif ftype == "error":
                        recoverable = frame.get("recoverable", True)
                        logger.warning("error frame: %s", frame.get("message"))
                        if not recoverable:
                            await ws.close(code=1008)
                            return
        except Exception as e:
            logger.warning("ws session ended: %s", e)

        if stop_event.is_set():
            return
        await asyncio.sleep(backoff)
        backoff = min(30.0, backoff * 2)


async def _handle_instruction(
    ws: Any,
    inst: dict[str, Any],
    state: ClientState,
    dispatch_fn: DispatchFn,
    priv: Any,
    queue: _queue.OfflineQueue,
) -> None:
    instruction_id = inst.get("instruction_id", "")
    seq = int(inst.get("seq", 0))
    try:
        outcome = await dispatch_fn(inst, state)
    except Exception as e:
        logger.exception("dispatch threw")
        outcome = {
            "status": "failed",
            "error_code": "runtime_error",
            "error_message": str(e),
            "retryable": True,
        }
        receipt_body = _make_receipt_body(instruction_id, state, outcome, precondition_met=True, artifact_refs=[])
    else:
        precondition_met = outcome.pop("__precondition_met", True)
        artifact_refs = outcome.pop("__artifact_refs", [])
        receipt_body = _make_receipt_body(
            instruction_id,
            state,
            outcome,
            precondition_met=precondition_met,
            artifact_refs=artifact_refs,
        )

    signed = sign_receipt(priv, receipt_body, state.session_key_fingerprint)

    frame = {
        "v": 1,
        "type": "receipt",
        "msg_id": _msg_id(),
        "task_id": state.task_id,
        "receipt": signed,
    }
    try:
        await ws.send(json.dumps(frame))
        queue.record_seq(state.task_id, seq)
    except Exception:
        queue.enqueue_receipt(state.task_id, instruction_id, frame)


def _make_receipt_body(
    instruction_id: str,
    state: ClientState,
    outcome: dict[str, Any],
    *,
    precondition_met: bool,
    artifact_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    import datetime as dt

    return {
        "receipt_id": "rcpt_" + uuid.uuid4().hex[:12],
        "instruction_id": instruction_id,
        "task_id": state.task_id,
        "executed_at": dt.datetime.now(dt.UTC).isoformat(),
        "executor_target": "plugin",
        "session_key_fingerprint": state.session_key_fingerprint,
        "precondition_met": precondition_met,
        "outcome": outcome,
        "artifact_refs": artifact_refs,
    }


__all__ = ["ClientState", "run_client"]
