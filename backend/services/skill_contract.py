"""WorkflowContract primitives for Superskills.

This module is intentionally business-agnostic.  It describes generic dataflow:
steps, inputs, outputs, foreach loops, output collection, resources and final
artifacts.  It should be the single source of truth used by Creator, SKILL.md
rendering, script generation prompts, trial-run validation and sandbox runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, asdict
from pathlib import Path
from typing import Any, Literal
import json


JsonType = Literal["string", "number", "integer", "boolean", "object", "array", "file_path", "file_paths", "any"]
LoadPolicy = Literal["always", "on_demand", "never"]


@dataclass
class ContractIssue:
    code: str
    message: str
    step_id: str | None = None
    script_path: str | None = None
    field: str | None = None
    severity: Literal["error", "warning"] = "error"
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InputSpec:
    type: JsonType = "any"
    required: bool = True
    default: Any = None
    source: str | None = None
    description: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "InputSpec":
        if isinstance(raw, InputSpec):
            return raw
        if isinstance(raw, str):
            return cls(type=_normalize_type(raw))
        if isinstance(raw, dict):
            return cls(
                type=_normalize_type(str(raw.get("type") or "any")),
                required=bool(raw.get("required", True)),
                default=raw.get("default"),
                source=raw.get("source"),
                description=str(raw.get("description") or ""),
            )
        return cls()


@dataclass
class OutputSpec:
    type: JsonType = "any"
    required: bool = True
    min_length: int | None = None
    min_items: int | None = None
    item_type: JsonType | None = None
    item_required: list[str] = dataclass_field(default_factory=list)
    path_must_exist: bool = False
    description: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "OutputSpec":
        if isinstance(raw, OutputSpec):
            return raw
        if isinstance(raw, str):
            typ = _normalize_type(raw)
            return cls(type=typ, path_must_exist=typ in {"file_path", "file_paths"})
        if isinstance(raw, dict):
            typ = _normalize_type(str(raw.get("type") or "any"))
            items = raw.get("items") if isinstance(raw.get("items"), dict) else {}
            item_type = _normalize_type(str(items.get("type"))) if items.get("type") else None
            item_required = list(items.get("required") or raw.get("item_required") or [])
            return cls(
                type=typ,
                required=bool(raw.get("required", True)),
                min_length=_maybe_int(raw.get("min_length")),
                min_items=_maybe_int(raw.get("min_items")),
                item_type=item_type,
                item_required=[str(x) for x in item_required],
                path_must_exist=bool(raw.get("path_must_exist", typ in {"file_path", "file_paths"})),
                description=str(raw.get("description") or ""),
            )
        return cls()


@dataclass
class LoopSpec:
    collection: str
    item_name: str = "loop_item"

    @classmethod
    def from_raw(cls, raw: Any) -> "LoopSpec | None":
        if raw in (None, "", False):
            return None
        if isinstance(raw, LoopSpec):
            return raw
        if isinstance(raw, str):
            return cls(collection=raw)
        if isinstance(raw, dict):
            collection = str(raw.get("collection") or raw.get("path") or "").strip()
            if not collection:
                return None
            return cls(collection=collection, item_name=str(raw.get("item_name") or "loop_item"))
        return None


@dataclass
class CollectSpec:
    target: str
    source: str
    type: JsonType = "array"
    step_id: str | None = None
    script_path: str | None = None
    step_index: int | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "CollectSpec":
        return cls(
            target=str(raw.get("target") or ""),
            source=str(raw.get("source") or ""),
            type=_normalize_type(str(raw.get("type") or "array")),
            step_id=raw.get("step_id"),
            script_path=raw.get("script_path"),
            step_index=_maybe_int(raw.get("step_index")),
        )


@dataclass
class ResourceSpec:
    path: str
    kind: str = "reference"
    summary: str = ""
    keywords: list[str] = dataclass_field(default_factory=list)
    applies_to_roles: list[str] = dataclass_field(default_factory=list)
    applies_to_steps: list[str] = dataclass_field(default_factory=list)
    when_to_read: str = ""
    load_policy: LoadPolicy = "on_demand"

    @classmethod
    def from_raw(cls, raw: Any) -> "ResourceSpec":
        if isinstance(raw, str):
            return cls(path=raw)
        return cls(
            path=str(raw.get("path") or ""),
            kind=str(raw.get("kind") or "reference"),
            summary=str(raw.get("summary") or ""),
            keywords=[str(x) for x in raw.get("keywords") or []],
            applies_to_roles=[str(x) for x in raw.get("applies_to_roles") or []],
            applies_to_steps=[str(x) for x in raw.get("applies_to_steps") or []],
            when_to_read=str(raw.get("when_to_read") or ""),
            load_policy=str(raw.get("load_policy") or "on_demand"),  # type: ignore[arg-type]
        )


@dataclass
class StepContract:
    id: str
    script_path: str
    role: str = "generic_script"
    inputs: dict[str, InputSpec] = dataclass_field(default_factory=dict)
    outputs: dict[str, OutputSpec] = dataclass_field(default_factory=dict)
    default_values: dict[str, Any] = dataclass_field(default_factory=dict)
    required_capabilities: list[str] = dataclass_field(default_factory=list)
    dependencies: list[str] = dataclass_field(default_factory=list)
    command_template: str = ""
    foreach: LoopSpec | None = None
    collect: list[CollectSpec] = dataclass_field(default_factory=list)
    resources: list[ResourceSpec] = dataclass_field(default_factory=list)
    final_artifacts: list[str] = dataclass_field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "StepContract":
        inputs = {str(k): InputSpec.from_raw(v) for k, v in (raw.get("inputs") or {}).items()}
        outputs = {str(k): OutputSpec.from_raw(v) for k, v in (raw.get("outputs") or {}).items()}

        defaults = dict(raw.get("default_values") or {})
        for key, spec in inputs.items():
            if spec.default is not None and key not in defaults:
                defaults[key] = spec.default

        collect_raw = raw.get("collect") or raw.get("collections") or []
        if isinstance(collect_raw, dict):
            collect = [
                CollectSpec(target=str(k), source=str(v.get("from") or v.get("source") or ""), type=_normalize_type(str(v.get("type") or "array")))
                if isinstance(v, dict) else CollectSpec(target=str(k), source=str(v))
                for k, v in collect_raw.items()
            ]
        else:
            collect = [CollectSpec.from_raw(x) for x in collect_raw if isinstance(x, dict)]

        return cls(
            id=str(raw.get("id") or Path(str(raw.get("script_path") or "step")).stem),
            script_path=str(raw.get("script_path") or ""),
            role=str(raw.get("role") or "generic_script"),
            inputs=inputs,
            outputs=outputs,
            default_values=defaults,
            required_capabilities=[str(x) for x in raw.get("required_capabilities") or []],
            dependencies=[str(x) for x in raw.get("dependencies") or []],
            command_template=str(raw.get("command_template") or ""),
            foreach=LoopSpec.from_raw(raw.get("foreach") or raw.get("loop")),
            collect=collect,
            resources=[ResourceSpec.from_raw(x) for x in raw.get("resources") or []],
            final_artifacts=[str(x) for x in raw.get("final_artifacts") or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def input_keys(self) -> list[str]:
        return list(self.inputs.keys())

    def output_keys(self) -> list[str]:
        return list(self.outputs.keys())


@dataclass
class WorkflowContract:
    skill_name: str
    steps: list[StepContract] = dataclass_field(default_factory=list)
    resources: list[ResourceSpec] = dataclass_field(default_factory=list)
    final_outputs: list[str] = dataclass_field(default_factory=list)
    version: str = "1"

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "WorkflowContract":
        return cls(
            skill_name=str(raw.get("skill_name") or raw.get("name") or ""),
            steps=[StepContract.from_raw(x) for x in raw.get("steps") or []],
            resources=[ResourceSpec.from_raw(x) for x in raw.get("resources") or []],
            final_outputs=[str(x) for x in raw.get("final_outputs") or raw.get("outputs") or []],
            version=str(raw.get("version") or "1"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def step_by_id(self, step_id: str) -> StepContract | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def step_by_script_path(self, script_path: str) -> StepContract | None:
        normalized = script_path.replace("\\", "/").lstrip("./")
        for step in self.steps:
            if step.script_path.replace("\\", "/").lstrip("./") == normalized:
                return step
        return None

    def write_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def read_json(cls, path: Path) -> "WorkflowContract":
        return cls.from_raw(json.loads(path.read_text(encoding="utf-8")))


def _normalize_type(value: str) -> JsonType:
    value = (value or "any").strip().lower()
    aliases = {
        "str": "string",
        "text": "string",
        "int": "integer",
        "float": "number",
        "dict": "object",
        "list": "array",
        "path": "file_path",
        "file": "file_path",
        "files": "file_paths",
        "artifact": "file_path",
    }
    value = aliases.get(value, value)
    if value in {"string", "number", "integer", "boolean", "object", "array", "file_path", "file_paths", "any"}:
        return value  # type: ignore[return-value]
    return "any"


def _maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
