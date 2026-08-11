"""Generate schemas, example JSON, and markdown input/result tables for each job.

Writes to:
  schemas/<job>/{inputs,result,manifest}.json
  docs/jobs/<job>/<job>.example.json
  docs/jobs/<job>/<job>.md  (injects between <!-- AUTO:* --> sentinels only)

Usage:
  generate_docs
  python -m twod_fim_jobs.models.generate_docs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from twod_fim_jobs.models.build_model import (
    BuildModelInputs,
    BuildModelResult,
    ModelManifest,
)
from twod_fim_jobs.models.run_nd_scenarios import (
    RunNDScenariosInputs,
    RunNDScenariosResult,
)
from twod_fim_jobs.models.common import ScenarioRunManifest


JOBS: list[tuple[str, type[BaseModel], type[BaseModel], type[BaseModel] | None]] = [
    ("build_model", BuildModelInputs, BuildModelResult, ModelManifest),
    (
        "run_nd_scenarios",
        RunNDScenariosInputs,
        RunNDScenariosResult,
        ScenarioRunManifest,
    ),
]

SENTINEL_START = "<!-- AUTO:{key} -->"
SENTINEL_END = "<!-- /AUTO:{key} -->"


def export_schemas(schemas_dir: Path) -> None:
    for job_name, inputs_cls, result_cls, manifest_cls in JOBS:
        job_dir = schemas_dir / job_name
        job_dir.mkdir(parents=True, exist_ok=True)
        specs: list[tuple[str, type[BaseModel]]] = [
            ("inputs", inputs_cls),
            ("result", result_cls),
        ]
        if manifest_cls is not None:
            specs.append(("manifest", manifest_cls))
        for schema_name, model_cls in specs:
            path = job_dir / f"{schema_name}.json"
            path.write_text(json.dumps(model_cls.model_json_schema(), indent=2) + "\n")
            print(f"  wrote {path}")


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    key = ref.removeprefix("#/$defs/")
    return defs.get(key, {})


def _extract_example(prop_schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    """Return the first examples value, or fall back to default, or a typed placeholder."""
    # Check default/examples on the raw property before any resolution (catches nullable defaults)
    if "examples" in prop_schema and prop_schema["examples"]:
        return prop_schema["examples"][0]
    if "default" in prop_schema:
        return prop_schema["default"]

    # Resolve $ref
    if "$ref" in prop_schema:
        prop_schema = _resolve_ref(prop_schema["$ref"], defs)
        if "examples" in prop_schema and prop_schema["examples"]:
            return prop_schema["examples"][0]
        if "default" in prop_schema:
            return prop_schema["default"]

    # Enum — return the first member
    if "enum" in prop_schema:
        return prop_schema["enum"][0]

    # anyOf / allOf — check outer default first, then recurse into first non-null branch
    for combiner in ("anyOf", "allOf", "oneOf"):
        if combiner in prop_schema:
            is_nullable = any(
                (b if "$ref" not in b else _resolve_ref(b["$ref"], defs)).get("type")
                == "null"
                for b in prop_schema[combiner]
            )
            for branch in prop_schema[combiner]:
                resolved = (
                    _resolve_ref(branch["$ref"], defs) if "$ref" in branch else branch
                )
                if resolved.get("type") != "null":
                    result = _extract_example(resolved, defs)
                    # If the non-null branch has no example and the field is nullable, prefer null
                    if (
                        isinstance(result, str)
                        and result.startswith("<required:")
                        and is_nullable
                    ):
                        return None
                    return result
            return None

    # Recurse into objects
    if prop_schema.get("type") == "object" and "properties" in prop_schema:
        return {
            k: _extract_example(v, defs) for k, v in prop_schema["properties"].items()
        }

    if prop_schema.get("type") == "array":
        return [_extract_example(prop_schema.get("items", {}), defs)]

    # Typed placeholder for required fields with no examples yet
    type_hint = prop_schema.get("type", "any")
    return f"<required: {type_hint}>"


def build_example(model_cls: type[BaseModel]) -> dict[str, Any]:
    schema = model_cls.model_json_schema()
    defs = schema.get("$defs", {})
    props = schema.get("properties", {})
    return {name: _extract_example(prop, defs) for name, prop in props.items()}


def export_examples(docs_dir: Path) -> None:
    for job_name, _inputs_cls, _result_cls, manifest_cls in JOBS:
        if manifest_cls is None:
            continue
        job_dir = docs_dir / "jobs" / job_name
        job_dir.mkdir(parents=True, exist_ok=True)
        example = build_example(manifest_cls)
        path = job_dir / f"{job_name}.example.json"
        path.write_text(json.dumps(example, indent=2) + "\n")
        print(f"  wrote {path}")
        schema_path = job_dir / f"{job_name}.schema.json"
        schema_path.write_text(
            json.dumps(manifest_cls.model_json_schema(), indent=2) + "\n"
        )
        print(f"  wrote {schema_path}")


def _type_str(prop_schema: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in prop_schema:
        return prop_schema["$ref"].removeprefix("#/$defs/")
    if "anyOf" in prop_schema:
        parts = []
        for branch in prop_schema["anyOf"]:
            if branch.get("type") == "null":
                continue
            parts.append(_type_str(branch, defs))
        return " | ".join(parts) or "any"
    t = prop_schema.get("type")
    if t == "array":
        items = prop_schema.get("items", {})
        return f"list[{_type_str(items, defs)}]"
    if t == "object" and "additionalProperties" in prop_schema:
        val_type = _type_str(prop_schema["additionalProperties"], defs)
        return f"dict[str, {val_type}]"
    return t or "any"


def _render_inputs_table(model_cls: type[BaseModel]) -> str:
    schema = model_cls.model_json_schema()
    defs = schema.get("$defs", {})
    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    rows_required: list[tuple[str, str, str, str]] = []
    rows_optional: list[tuple[str, str, str, str]] = []

    for name, prop in props.items():
        type_s = _type_str(prop, defs)
        desc = prop.get("description", "")
        default = prop.get("default", "")
        default_s = json.dumps(default) if default != "" else ""
        row = (name, type_s, default_s, desc)
        if name in required:
            rows_required.append(row)
        else:
            rows_optional.append(row)

    def _table(rows: list[tuple[str, str, str, str]], show_default: bool) -> str:
        if not rows:
            return ""
        if show_default:
            header = (
                "| Name | Type | Default | Description |\n| --- | --- | --- | --- |"
            )
            lines = [f"| `{n}` | `{t}` | {d} | {desc} |" for n, t, d, desc in rows]
        else:
            header = "| Name | Type | Description |\n| --- | --- | --- |"
            lines = [f"| `{n}` | `{t}` | {desc} |" for n, t, _, desc in rows]
        return header + "\n" + "\n".join(lines)

    parts = []
    if rows_required:
        parts.append("### Required\n\n" + _table(rows_required, show_default=False))
    if rows_optional:
        parts.append("### Optional\n\n" + _table(rows_optional, show_default=True))
    return "\n\n".join(parts)


def _render_result_table(model_cls: type[BaseModel]) -> str:
    schema = model_cls.model_json_schema()
    defs = schema.get("$defs", {})
    props = schema.get("properties", {})

    if not props:
        return "_No fields defined._"

    header = "| Name | Type | Description |\n| --- | --- | --- |"
    rows = [
        f"| `{name}` | `{_type_str(prop, defs)}` | {prop.get('description', '')} |"
        for name, prop in props.items()
    ]
    return header + "\n" + "\n".join(rows)


def _render_artifacts_table(manifest_cls: type[BaseModel]) -> str:
    """Render the fields of the manifest's `assets` sub-model as an artifacts table."""
    schema = manifest_cls.model_json_schema()
    defs = schema.get("$defs", {})
    assets_prop = schema.get("properties", {}).get("assets", {})

    # Resolve the assets field to its concrete sub-schema
    if "$ref" in assets_prop:
        assets_schema = _resolve_ref(assets_prop["$ref"], defs)
    else:
        assets_schema = assets_prop

    props = assets_schema.get("properties", {})
    if not props:
        return "_No artifacts defined._"

    header = "| Name | Description |\n| --- | --- |"
    rows = [
        f"| `{name}` | {prop.get('description', '')} |" for name, prop in props.items()
    ]
    return header + "\n" + "\n".join(rows)


def _inject_sentinel(content: str, key: str, replacement: str) -> str:
    start_tag = SENTINEL_START.format(key=key)
    end_tag = SENTINEL_END.format(key=key)
    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)
    if start_idx == -1 or end_idx == -1:
        return content  # sentinel not present — skip
    before = content[: start_idx + len(start_tag)]
    after = content[end_idx:]
    return before + "\n" + replacement + "\n" + after


def update_markdown_tables(docs_dir: Path) -> None:
    for job_name, inputs_cls, result_cls, manifest_cls in JOBS:
        md_path = docs_dir / "jobs" / job_name / f"{job_name}.md"
        if not md_path.exists():
            print(f"  skipping {md_path} (not found)")
            continue
        content = md_path.read_text()
        content = _inject_sentinel(
            content, "inputs_table", _render_inputs_table(inputs_cls)
        )
        content = _inject_sentinel(
            content, "result_table", _render_result_table(result_cls)
        )
        if manifest_cls is not None:
            content = _inject_sentinel(
                content, "artifacts_table", _render_artifacts_table(manifest_cls)
            )
        md_path.write_text(content)
        print(f"  updated {md_path}")


def main(
    root: Path | None = None,
    schemas_dir: Path | None = None,
    docs_dir: Path | None = None,
) -> None:
    if root is None:
        root = Path(__file__).parents[2]
    if schemas_dir is None:
        schemas_dir = root / "schemas"
    if docs_dir is None:
        docs_dir = root / "docs"

    print("Exporting schemas...")
    export_schemas(schemas_dir)

    print("Exporting example JSON...")
    export_examples(docs_dir)

    print("Updating markdown tables...")
    update_markdown_tables(docs_dir)


if __name__ == "__main__":
    main()
