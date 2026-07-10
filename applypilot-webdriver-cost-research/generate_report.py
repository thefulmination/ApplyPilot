#!/usr/bin/env python3
"""Generate the consolidated ApplyPilot browser-cost research report."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml


BASE_DIR = Path(__file__).resolve().parent
OUTLINE_PATH = BASE_DIR / "outline.yaml"
FIELDS_PATH = BASE_DIR / "fields.yaml"
REPORT_PATH = BASE_DIR / "report.md"

CATEGORY_MAPPING = {
    "Basic Info": ["basic_info", "Basic Info"],
    "Technical Features": [
        "technical_features",
        "technical_characteristics",
        "Technical Features",
    ],
    "Performance Metrics": ["performance_metrics", "performance", "Performance Metrics"],
    "Milestone Significance": [
        "milestone_significance",
        "milestones",
        "Milestone Significance",
    ],
    "Business Info": ["business_info", "commercial_info", "Business Info"],
    "Competition & Ecosystem": [
        "competition_ecosystem",
        "competition",
        "Competition & Ecosystem",
    ],
    "History": ["history", "History"],
    "Market Positioning": ["market_positioning", "market", "Market Positioning"],
}

INTERNAL_FIELDS = {"_source_file", "uncertain"}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_results(output_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(output_dir.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} must contain a JSON object")
        records.append((path, value))
    return records


def snake_case(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return token.lower()


def anchor(value: str) -> str:
    token = re.sub(r"[^a-z0-9\s-]", "", value.lower())
    token = re.sub(r"[\s-]+", "-", token).strip("-")
    return token or "item"


def title(value: str) -> str:
    return value.replace("_", " ").strip().title()


def category_aliases(category: str) -> list[str]:
    aliases = list(CATEGORY_MAPPING.get(category, []))
    aliases.extend([category, snake_case(category)])
    return list(dict.fromkeys(aliases))


def walk_for_key(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        if field_name in value:
            return value[field_name]
        for child in value.values():
            hit = walk_for_key(child, field_name)
            if hit is not None:
                return hit
    elif isinstance(value, list):
        for child in value:
            hit = walk_for_key(child, field_name)
            if hit is not None:
                return hit
    return None


def find_value(data: dict[str, Any], category: str, field_name: str) -> Any:
    if field_name in data:
        return data[field_name]
    for alias in category_aliases(category):
        nested = data.get(alias)
        if isinstance(nested, dict) and field_name in nested:
            return nested[field_name]
    return walk_for_key(data, field_name)


def contains_uncertain(value: Any) -> bool:
    if isinstance(value, str):
        return "[uncertain]" in value.lower()
    if isinstance(value, dict):
        return any(contains_uncertain(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_uncertain(item) for item in value)
    return False


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def format_inline(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{title(str(key))}: {format_inline(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(format_inline(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value).strip()


def format_value(value: Any) -> tuple[str, bool]:
    """Return rendered text and whether it should be placed on its own block."""
    if isinstance(value, list):
        if not value:
            return "", False
        if all(isinstance(item, dict) for item in value):
            lines = [
                "- " + " | ".join(f"{title(str(key))}: {format_inline(item)}" for key, item in row.items())
                for row in value
            ]
            return "\n".join(lines), True
        rendered = [format_inline(item) for item in value]
        if len(rendered) <= 3 and sum(map(len, rendered)) <= 100:
            return ", ".join(rendered), False
        return "<br>".join(f"- {item}" for item in rendered), True
    if isinstance(value, dict):
        rendered = format_inline(value)
        return rendered, len(rendered) > 100
    rendered = format_inline(value)
    return rendered, len(rendered) > 100 or "\n" in rendered


def append_field(lines: list[str], label: str, value: Any) -> None:
    rendered, block = format_value(value)
    if not rendered:
        return
    if not block:
        lines.append(f"- **{label}:** {rendered}")
        return
    lines.extend([f"- **{label}:**", ""])
    if "\n" in rendered or rendered.startswith("<br>"):
        for item in rendered.splitlines():
            lines.append(f"  {item}")
    else:
        lines.append(f"  > {rendered}")


def iter_extra_fields(data: dict[str, Any], defined: set[str], aliases: set[str]) -> Iterable[tuple[str, Any]]:
    for key, value in data.items():
        if key in INTERNAL_FIELDS or key in defined or key in aliases:
            continue
        if isinstance(value, dict):
            yield from iter_extra_fields(value, defined, aliases)
        else:
            yield key, value


def generate_report() -> tuple[int, int]:
    outline = load_yaml(OUTLINE_PATH)
    fields_doc = load_yaml(FIELDS_PATH)
    output_dir = BASE_DIR / outline.get("execution", {}).get("output_dir", "./results")
    records = load_results(output_dir)
    categories = fields_doc.get("field_categories", [])
    if not records:
        raise ValueError(f"No JSON results found in {output_dir}")
    if not categories:
        raise ValueError("fields.yaml has no field_categories")

    field_names = {
        field["name"]
        for category in categories
        for field in category.get("fields", [])
    }
    aliases = {
        alias
        for category in categories
        for alias in category_aliases(category["category"])
    }

    rows: list[tuple[str, str, Path, dict[str, Any]]] = []
    for path, data in records:
        item_name = str(find_value(data, "identity", "name") or path.stem.replace("_", " "))
        item_category = str(find_value(data, "identity", "category") or "unknown")
        rows.append((item_name, item_category, path, data))
    rows.sort(key=lambda item: (item[1].lower(), item[0].lower()))

    topic = str(outline.get("topic") or fields_doc.get("topic") or "Research Report")
    lines = [
        f"# {topic}",
        "",
        f"Consolidated from {len(rows)} validated research records. Values explicitly marked uncertain are omitted from the detailed comparison and listed separately for each option.",
        "",
        "## Table of contents",
        "",
    ]
    for index, (item_name, item_category, _, _) in enumerate(rows, start=1):
        lines.append(f"{index}. [{item_name}](#{anchor(item_name)}) - Category: {item_category}")

    lines.extend(["", "## Detailed results", ""])
    for item_name, item_category, path, data in rows:
        lines.extend([f"### {item_name}", "", f"Source record: `{path.name}`", ""])
        uncertain = {str(item) for item in data.get("uncertain", [])}
        for category in categories:
            category_name = str(category["category"])
            category_lines: list[str] = []
            for field in category.get("fields", []):
                field_name = str(field["name"])
                value = find_value(data, category_name, field_name)
                if field_name in uncertain or is_empty(value) or contains_uncertain(value):
                    continue
                append_field(category_lines, title(field_name), value)
            if category_lines:
                lines.extend([f"#### {title(category_name)}", "", *category_lines, ""])

        extras = list(iter_extra_fields(data, field_names, aliases))
        if extras:
            lines.extend(["#### Other Info", ""])
            for key, value in extras:
                if not is_empty(value) and not contains_uncertain(value):
                    append_field(lines, title(key), value)
            lines.append("")

        if uncertain:
            lines.extend(["#### Uncertain Fields", ""])
            lines.extend(f"- `{field_name}`" for field_name in sorted(uncertain))
            lines.append("")

    REPORT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return len(rows), len(field_names)


if __name__ == "__main__":
    item_count, field_count = generate_report()
    print(f"report={REPORT_PATH}")
    print(f"items={item_count}")
    print(f"defined_fields={field_count}")
