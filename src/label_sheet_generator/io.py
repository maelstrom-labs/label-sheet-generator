from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from label_sheet_generator.models import (
    LabelTemplate,
    TemplateDefinition,
    TemplateError,
    TextLayoutTemplate,
    parse_template_definition,
)


_TEMPLATE_NUMBER_PRECISION = Decimal("0.01")


def _round_template_numbers(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _round_template_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_template_numbers(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = float(
            Decimal(str(value)).quantize(_TEMPLATE_NUMBER_PRECISION, rounding=ROUND_HALF_UP)
        )
        if rounded == 0:
            return 0.0
        return rounded
    return value


def _format_template_json(value: Any, *, indent: int = 0) -> str:
    if isinstance(value, dict):
        if not value:
            return "{}"
        child_indent = indent + 2
        items = [
            " " * child_indent + json.dumps(key) + ": " + _format_template_json(item, indent=child_indent)
            for key, item in value.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        child_indent = indent + 2
        items = [
            " " * child_indent + _format_template_json(item, indent=child_indent)
            for item in value
        ]
        return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == 0:
            value = 0.0
        return f"{value:.2f}"
    return json.dumps(value)


def load_structured_file(path: str | Path) -> Any:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    content = source_path.read_text(encoding="utf-8")

    if suffix == ".json":
        return json.loads(content)
    raise TemplateError(f"unsupported file format: {source_path.suffix}")


def save_structured_file(path: str | Path, payload: Any) -> None:
    target_path = Path(path)
    suffix = target_path.suffix.lower()

    if suffix == ".json":
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return
    raise TemplateError(f"unsupported output format: {target_path.suffix}")


def load_template(path: str | Path) -> LabelTemplate:
    definition = load_template_definition(path)
    if isinstance(definition, TextLayoutTemplate):
        raise TemplateError("template file is a text layout template; use it with generate --layout-template")
    return definition


def load_text_layout_template(path: str | Path) -> TextLayoutTemplate:
    definition = load_template_definition(path)
    if isinstance(definition, LabelTemplate):
        raise TemplateError("template file is a label template; use it as the main generate template")
    return definition


def load_template_definition(path: str | Path) -> TemplateDefinition:
    raw_data = load_structured_file(path)
    if not isinstance(raw_data, dict):
        raise TemplateError("template file must contain a mapping at the top level")
    return parse_template_definition(raw_data)


def load_records(path: str | Path) -> list[dict[str, Any]]:
    raw_data = load_structured_file(path)

    if raw_data is None:
        return []

    if isinstance(raw_data, list):
        records = raw_data
    elif isinstance(raw_data, dict) and isinstance(raw_data.get("records"), list):
        records = raw_data["records"]
    else:
        raise TemplateError("record file must be a list or a mapping with a 'records' list")

    normalized_records: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise TemplateError(f"record {index} must be a mapping")
        normalized_records.append(record)

    return normalized_records


def save_template(path: str | Path, template: LabelTemplate) -> None:
    target_path = Path(path)
    if target_path.suffix.lower() != ".json":
        raise TemplateError(f"unsupported output format: {target_path.suffix}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _round_template_numbers(template.to_dict())
    target_path.write_text(_format_template_json(payload) + "\n", encoding="utf-8")


def save_text_layout_template(path: str | Path, template: TextLayoutTemplate) -> None:
    target_path = Path(path)
    if target_path.suffix.lower() != ".json":
        raise TemplateError(f"unsupported output format: {target_path.suffix}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _round_template_numbers(template.to_dict())
    target_path.write_text(_format_template_json(payload) + "\n", encoding="utf-8")
