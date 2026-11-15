"""Independent checks of budget accounting and admission across midnight."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.causes import Cause
from rhi_common.students import StudentKey
from rhi_common.tables import USAGE
from rhi_gateway.budget import DynamoDBLedger, InMemoryLedger, Ledger, Reservation
from rhi_gateway.errors import GatewayError

NOON = int(datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp())
BODY = b"x" * 8000
AMOUNT = 2500


@dataclass
class Clock:
    now: int = NOON

    def __call__(self) -> float:
        return self.now


class Client:
    """Serialize individual moto operations, and count successful writes only."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.lock = threading.Lock()
        self.writes = 0

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            with self.lock:
                result = getattr(self.raw, name)(**kwargs)
                if name in {"put_item", "update_item"}:
                    self.writes += 1
                return result

        return call


@pytest.fixture
 def_placeholder
