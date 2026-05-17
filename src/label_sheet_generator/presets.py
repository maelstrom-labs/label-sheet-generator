from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from label_sheet_generator.io import load_template_definition
from label_sheet_generator.models import LabelTemplate, TemplateError, TextLayoutTemplate


TEMPLATES_DIR_NAME = "templates"
LABEL_TEMPLATES_DIR_NAME = "labels"
LAYOUT_TEMPLATES_DIR_NAME = "layouts"


@dataclass(frozen=True, slots=True)
class LocalTemplate:
    key: str
    path: Path
    template_name: str | None
    template_type: Literal["label", "text-layout"]


def get_templates_dir(base_dir: str | Path = ".") -> Path:
    return Path(base_dir) / TEMPLATES_DIR_NAME


def get_label_templates_dir(base_dir: str | Path = ".") -> Path:
    return get_templates_dir(base_dir) / LABEL_TEMPLATES_DIR_NAME


def get_layout_templates_dir(base_dir: str | Path = ".") -> Path:
    return get_templates_dir(base_dir) / LAYOUT_TEMPLATES_DIR_NAME


def _add_candidate_paths(candidates: list[Path], path: Path) -> None:
    candidates.append(path)
    if path.suffix == "":
        candidates.append(Path(f"{path}.json"))


def resolve_template_input_path(template_ref: str | Path, *, base_dir: str | Path = ".") -> Path:
    raw_reference = str(template_ref)
    template_path = Path(template_ref)

    if template_path.exists():
        return template_path
    if template_path.is_absolute() or raw_reference.startswith("."):
        return template_path

    templates_dir = get_templates_dir(base_dir)
    candidates: list[Path] = []
    if "/" in raw_reference or "\\" in raw_reference:
        _add_candidate_paths(candidates, templates_dir / template_path)
    else:
        for directory in (
            templates_dir,
            get_label_templates_dir(base_dir),
            get_layout_templates_dir(base_dir),
        ):
            _add_candidate_paths(candidates, directory / template_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    if templates_dir.exists() and "/" not in raw_reference and "\\" not in raw_reference:
        recursive_matches = sorted(
            candidate
            for candidate in templates_dir.rglob("*.json")
            if candidate.stem == template_path.name
        )
        if len(recursive_matches) == 1:
            return recursive_matches[0]
        if len(recursive_matches) > 1:
            matches = ", ".join(
                candidate.relative_to(templates_dir).with_suffix("").as_posix()
                for candidate in recursive_matches
            )
            raise TemplateError(f"template name is ambiguous; use one of: {matches}")

    return template_path


def resolve_template_output_path(
    output_ref: str | Path,
    *,
    base_dir: str | Path = ".",
    template_type: Literal["label", "text-layout"] = "label",
) -> Path:
    raw_reference = str(output_ref)
    output_path = Path(output_ref)

    if output_path.is_absolute() or "/" in raw_reference or "\\" in raw_reference or raw_reference.startswith("."):
        return output_path

    if template_type == "text-layout":
        return get_layout_templates_dir(base_dir) / output_path
    return get_label_templates_dir(base_dir) / output_path


def iter_local_templates(base_dir: str | Path = ".") -> tuple[LocalTemplate, ...]:
    templates_dir = get_templates_dir(base_dir)
    if not templates_dir.exists():
        return ()

    templates: list[LocalTemplate] = []
    for template_path in sorted(templates_dir.rglob("*.json")):
        try:
            template = load_template_definition(template_path)
        except (OSError, TemplateError, ValueError):
            continue

        template_type: Literal["label", "text-layout"]
        if isinstance(template, LabelTemplate):
            template_type = "label"
        elif isinstance(template, TextLayoutTemplate):
            template_type = "text-layout"
        else:
            continue

        relative_key = template_path.relative_to(templates_dir).with_suffix("").as_posix()

        templates.append(
            LocalTemplate(
                key=relative_key,
                path=template_path,
                template_name=template.name,
                template_type=template_type,
            )
        )

    return tuple(templates)