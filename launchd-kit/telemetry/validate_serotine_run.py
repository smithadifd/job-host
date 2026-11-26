#!/usr/bin/env python3
"""Vendored minimal JSON Schema (draft 2020-12) validator for serotine.run.v1.

VENDORED, DELIBERATELY MINIMAL: python3's stdlib has no jsonschema module and
launchd-kit takes no package-manager dependencies, so this file implements ONLY
the draft-2020-12 keywords that serotine-run-v1.schema.json actually uses:

    type (object|array|string|integer|number), const, enum, pattern,
    minLength, minimum, maximum, minItems, properties, required,
    additionalProperties (boolean form), items (single-schema form)

It is NOT a general-purpose validator. If the schema grows a keyword this file
does not know, validation fails loudly (unknown-keyword error) rather than
silently passing — extend the keyword table in the same commit as the schema.

Usage:
    python3 validate_serotine_run.py <fixture.jsonl> [...]

Each fixture file must be exactly one non-empty JSONL line. Exit 0 if every
file validates; exit 1 on the first failure, with the failing file, JSON path,
and violated keyword on stderr (the test harness greps this).
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "serotine-run-v1.schema.json")

# Keywords this validator understands. Annotation-only keys are ignored.
_ANNOTATIONS = {"$schema", "$id", "title", "description"}
_KNOWN = _ANNOTATIONS | {
    "type", "const", "enum", "pattern", "minLength", "minimum", "maximum",
    "minItems", "properties", "required", "additionalProperties", "items",
}


class ValidationError(Exception):
    def __init__(self, path, keyword, message):
        self.path = path or "$"
        self.keyword = keyword
        super().__init__(f"{self.path}: [{keyword}] {message}")


def _check_type(instance, expected, path):
    # bool is an int subclass in Python; JSON booleans are never integers/numbers.
    ok = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    }
    if expected not in ok:
        raise ValidationError(path, "type", f"unsupported type {expected!r} in schema")
    if not ok[expected](instance):
        raise ValidationError(path, "type", f"expected {expected}, got {type(instance).__name__}")


def validate(instance, schema, path=""):
    unknown = set(schema) - _KNOWN
    if unknown:
        raise ValidationError(path, "schema", f"unknown keyword(s) {sorted(unknown)} — extend validate_serotine_run.py")

    if "type" in schema:
        _check_type(instance, schema["type"], path)

    if "const" in schema and instance != schema["const"]:
        raise ValidationError(path, "const", f"expected {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        raise ValidationError(path, "enum", f"{instance!r} not in {schema['enum']}")

    if "pattern" in schema:
        _check_type(instance, "string", path)
        if not re.search(schema["pattern"], instance):
            raise ValidationError(path, "pattern", f"{instance!r} does not match {schema['pattern']!r}")

    if "minLength" in schema:
        _check_type(instance, "string", path)
        if len(instance) < schema["minLength"]:
            raise ValidationError(path, "minLength", f"length {len(instance)} < {schema['minLength']}")

    if "minimum" in schema:
        _check_type(instance, "number", path)
        if instance < schema["minimum"]:
            raise ValidationError(path, "minimum", f"{instance} < {schema['minimum']}")

    if "maximum" in schema:
        _check_type(instance, "number", path)
        if instance > schema["maximum"]:
            raise ValidationError(path, "maximum", f"{instance} > {schema['maximum']}")

    if "minItems" in schema:
        _check_type(instance, "array", path)
        if len(instance) < schema["minItems"]:
            raise ValidationError(path, "minItems", f"{len(instance)} item(s) < {schema['minItems']}")

    if "required" in schema:
        _check_type(instance, "object", path)
        for key in schema["required"]:
            if key not in instance:
                raise ValidationError(path, "required", f"missing required property {key!r}")

    if "properties" in schema:
        _check_type(instance, "object", path)
        for key, subschema in schema["properties"].items():
            if key in instance:
                validate(instance[key], subschema, f"{path}.{key}" if path else f"$.{key}")

    if schema.get("additionalProperties") is False:
        _check_type(instance, "object", path)
        allowed = set(schema.get("properties", {}))
        extra = set(instance) - allowed
        if extra:
            raise ValidationError(path, "additionalProperties", f"unexpected propert(ies) {sorted(extra)}")

    if "items" in schema:
        _check_type(instance, "array", path)
        for i, item in enumerate(instance):
            validate(item, schema["items"], f"{path}[{i}]" if path else f"$[{i}]")


def load_one_jsonl_line(fixture_path):
    with open(fixture_path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    if len(lines) != 1:
        raise ValidationError("$", "jsonl", f"expected exactly one non-empty JSONL line, found {len(lines)}")
    try:
        return json.loads(lines[0])
    except json.JSONDecodeError as e:
        raise ValidationError("$", "jsonl", f"invalid JSON: {e}") from e


def main(argv):
    if len(argv) < 2:
        print(f"usage: {os.path.basename(argv[0])} <fixture.jsonl> [...]", file=sys.stderr)
        return 2
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)
    for fixture in argv[1:]:
        try:
            validate(load_one_jsonl_line(fixture), schema)
        except ValidationError as e:
            print(f"INVALID {fixture}: {e}", file=sys.stderr)
            return 1
        print(f"valid {fixture}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
