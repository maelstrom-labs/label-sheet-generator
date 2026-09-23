"""Shared fixtures.

Everything is built on the packaged built-in templates rather than on files
written into a tmp_path, so the tests exercise the same catalog a deployed
container sees.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from label_sheet_generator.settings import Settings, default_settings

if TYPE_CHECKING:
    from label_sheet_generator.catalog import Catalog
    from label_sheet_generator.service import LabelSheetService


@pytest.fixture
def settings() -> Settings:
    base = default_settings()
    base.validate()
    return base


@pytest.fixture
def catalog(settings: Settings) -> Catalog:
    from label_sheet_generator.catalog import Catalog

    return Catalog.build(settings)


@pytest.fixture
def service(settings: Settings, catalog: Catalog) -> Iterator[LabelSheetService]:
    from label_sheet_generator.service import LabelSheetService

    instance = LabelSheetService(settings, catalog)
    yield instance
    instance.close()


@pytest.fixture
def asset_dir(tmp_path: Path) -> Path:
    """An asset root with one real PNG and a secret sitting just outside it."""
    from PIL import Image

    root = tmp_path / "assets"
    root.mkdir()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(root / "logo.png")
    (tmp_path / "secret.png").write_bytes((root / "logo.png").read_bytes())
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return root


@pytest.fixture
def client(settings: Settings, tmp_path: Path):
    """A TestClient with rate limiting effectively disabled.

    The limiter is exercised deliberately in test_security.py; leaving it at
    production settings here would make unrelated tests flaky as they share a
    client address.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from label_sheet_generator.api.app import create_app

    relaxed = replace(
        settings,
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        render_rate_limit_burst=10_000,
        render_rate_limit_per_second=10_000.0,
    )
    with TestClient(create_app(relaxed)) as test_client:
        yield test_client


@pytest.fixture
def basic_template_id() -> str:
    return "labels/basic-address"


@pytest.fixture
def sample_document() -> str:
    return json.dumps(
        {
            "schema": ["name", "address_1", "address_2", "sku"],
            "records": [
                {
                    "name": "Ada Lovelace",
                    "address_1": "12 Analytical Engine Way",
                    "address_2": "London",
                    "sku": "AL-1001",
                },
                {
                    "name": "Alan Turing",
                    "address_1": "1 Bletchley Park",
                    "address_2": "Milton Keynes",
                    "sku": "AT-1002",
                },
            ],
        }
    )


def render_payload(template_id: str, document: str = "", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"template_id": template_id, "document": document}
    payload.update(overrides)
    return payload
