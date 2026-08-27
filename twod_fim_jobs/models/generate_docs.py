"""Generate schemas, example JSON, and markdown input/result tables for each job.

Writes to:
  schemas/<job>/{inputs,result,manifest}.json
  docs/jobs/<job>/<job>.example.jsonc
  docs/jobs/<job>/<job>.md  (injects between <!-- AUTO:* --> sentinels only)
  docs/jobs/run_scenarios/{scenario_manifest.example.jsonc,scenario_manifest.schema.json}

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
from twod_fim_jobs.models.run_kwse_scenarios import (
    HotStart,
    KWSEScenario,
    RunKWSEScenariosInputs,
    RunKWSEScenariosResult,
)
from twod_fim_jobs.models.solvers import RunScenarioManifest


JOBS: list[
    tuple[
        str,
        type[BaseModel],
        type[BaseModel],
        type[BaseModel] | None,
        str | None,
        dict[str, type[BaseModel]] | None,
    ]
] = [
    ("build_model", BuildModelInputs, BuildModelResult, ModelManifest, None, None),
    (
        "run_nd_scenarios",
        RunNDScenariosInputs,
        RunNDScenariosResult,
        RunScenarioManifest,
        "run_scenarios",
        None,
    ),
    (
        "run_kwse_scenarios",
        RunKWSEScenariosInputs,
        RunKWSEScenariosResult,
        RunScenarioManifest,
        "run_scenarios",
        {"kwse_scenario_table": KWSEScenario, "hotstart_table": HotStart},
    ),
]

SENTINEL_START = "<!-- AUTO:{key} -->"
SENTINEL_END = "<!-- /AUTO:{key} -->"


def _extract_field_descriptions(
    prop_schema: dict[str, Any], defs: dict[str, Any]
) -> "str | dict[str, Any]":
    """Return a description string for leaf fields, or a nested dict for object fields."""
    desc = prop_schema.get("description", "")
    resolved = prop_schema
    if "$ref" in prop_schema:
        resolved = _resolve_ref(prop_schema["$ref"], defs)
        desc = desc or resolved.get("description", "")
    for combiner in ("anyOf", "oneOf"):
        if combiner in resolved:
            for branch in resolved[combiner]:
                r = _resolve_ref(branch["$ref"], defs) if "$ref" in branch else branch
                if r.get("type") != "null":
                    child = _extract_field_descriptions(r, defs)
                    return child if isinstance(child, dict) else desc or child
            break
    if resolved.get("type") == "object" and "properties" in resolved:
        return {
            k: _extract_field_descriptions(v, defs)
            for k, v in resolved["properties"].items()
        }
    return desc


def _build_descriptions(model_cls: type[BaseModel]) -> dict[str, Any]:
    schema = model_cls.model_json_schema()
    defs = schema.get("$defs", {})
    return {
        name: _extract_field_descriptions(prop, defs)
        for name, prop in schema.get("properties", {}).items()
    }


def _to_jsonc(data: Any, descriptions: dict[str, Any], indent: int = 0) -> str:
    """Render data as JSONC with inline // description comments."""
    pad = "  " * indent
    inner = "  " * (indent + 1)
    if isinstance(data, list):
        if not data or not any(isinstance(item, (dict, list)) for item in data):
            return json.dumps(data)
        rendered_items = [inner + _to_jsonc(item, {}, indent + 1) for item in data]
        return "[\n" + ",\n".join(rendered_items) + "\n" + pad + "]"
    if not isinstance(data, dict):
        return json.dumps(data)
    lines = ["{"]
    items = list(data.items())
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        field_desc = descriptions.get(key, "")
        if isinstance(value, (dict, list)) and isinstance(field_desc, dict):
            nested = _to_jsonc(value, field_desc, indent + 1)
            lines.append(f'{inner}"{key}": {nested}{comma}')
        else:
            if isinstance(value, (dict, list)):
                rendered = _to_jsonc(value, {}, indent + 1)
            else:
                rendered = json.dumps(value)
            comment = (
                f"  // {field_desc}"
                if isinstance(field_desc, str) and field_desc
                else ""
            )
            lines.append(f'{inner}"{key}": {rendered}{comma}{comment}')
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def export_schemas(schemas_dir: Path) -> None:
    for job_name, inputs_cls, result_cls, manifest_cls, *_ in JOBS:
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
    if prop_schema.get("examples"):
        return prop_schema["examples"][0]
    if "default" in prop_schema:
        return prop_schema["default"]

    # Resolve $ref
    if "$ref" in prop_schema:
        prop_schema = _resolve_ref(prop_schema["$ref"], defs)
        if prop_schema.get("examples"):
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
    for job_name, _inputs_cls, _result_cls, manifest_cls, md_dir, *_ in JOBS:
        if manifest_cls is None or md_dir is not None:
            continue
        job_dir = docs_dir / "jobs" / job_name
        job_dir.mkdir(parents=True, exist_ok=True)
        example = build_example(manifest_cls)
        descriptions = _build_descriptions(manifest_cls)
        path = job_dir / f"{job_name}.example.jsonc"
        path.write_text(_to_jsonc(example, descriptions) + "\n")
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


def _render_inputs_table(model_cls: type[BaseModel], heading_level: int = 3) -> str:
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

    h = "#" * heading_level
    parts = []
    if rows_required:
        parts.append(f"{h} Required\n\n" + _table(rows_required, show_default=False))
    if rows_optional:
        parts.append(f"{h} Optional\n\n" + _table(rows_optional, show_default=True))
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
    for job_name, inputs_cls, result_cls, manifest_cls, md_dir, sub_models, *_ in JOBS:
        md_path = docs_dir / "jobs" / (md_dir or job_name) / f"{job_name}.md"
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
        if sub_models:
            for key, model_cls in sub_models.items():
                content = _inject_sentinel(
                    content, key, _render_inputs_table(model_cls, heading_level=4)
                )
        md_path.write_text(content)
        print(f"  updated {md_path}")


def export_scenario_manifest(docs_dir: Path) -> None:
    """Export shared scenario_manifest example and schema to run_scenarios directory."""
    run_scenarios_dir = docs_dir / "jobs" / "run_scenarios"
    run_scenarios_dir.mkdir(parents=True, exist_ok=True)

    example = build_example(RunScenarioManifest)
    descriptions = _build_descriptions(RunScenarioManifest)

    example_path = run_scenarios_dir / "scenario_manifest.example.jsonc"
    example_path.write_text(_to_jsonc(example, descriptions) + "\n")
    print(f"  wrote {example_path}")

    schema_path = run_scenarios_dir / "scenario_manifest.schema.json"
    schema_path.write_text(
        json.dumps(RunScenarioManifest.model_json_schema(), indent=2) + "\n"
    )
    print(f"  wrote {schema_path}")


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

    print("Exporting scenario manifest...")
    export_scenario_manifest(docs_dir)


if __name__ == "__main__":
    main()
