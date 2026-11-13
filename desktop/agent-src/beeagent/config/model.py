"""Typed configuration, without a compiler in the loop.

pydantic is a Rust extension, and PyPI publishes no wheel for it on Android
(measured against the index: manylinux and musllinux only, and musl there is
Alpine's, not bionic's). Termux has no compiler either. So the one object that
decides whether BeeCode starts at all was the reason it could not start on a
phone — for a project whose whole point is running free on whatever the user
already owns.

What BeeCode actually asks of a config is short enough to own: named fields with
defaults, a `model_dump()` that writes JSON, and a load that refuses a wrong
value instead of running with it. That is this file. The names are the ones the
rest of the package and every plugin already call.
"""
from __future__ import annotations

import typing
from typing import Any, get_args, get_origin


class ValidationError(ValueError):
    """A value the field cannot take — with one line per problem."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("invalid configuration:\n  " + "\n  ".join(self.problems))


class _Missing:
    def __repr__(self):
        return "<required>"


MISSING = _Missing()


class _Default:
    """A field whose value has to be built fresh per instance."""

    def __init__(self, producer):
        self.producer = producer


def default(producer) -> Any:
    """Declare a list, dict or sub-model default that two instances share nowhere.

    `allowed = default(list)` gives every config its own empty list; writing
    `allowed: list[str] = []` would hand both of them the same one.
    """
    return _Default(producer)


class _Bad(ValueError):
    """One field's complaint, raised by _coerce and collected by Model.__init__."""


def _described(annotation) -> str:
    origin = get_origin(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation))
    args = ", ".join(_described(a) for a in get_args(annotation))
    name = {list: "list", dict: "dict", tuple: "tuple", set: "set"}.get(origin, str(origin))
    return f"{name}[{args}]"


def _is_optional(annotation) -> bool:
    origin = get_origin(annotation)
    if origin is typing.Union:
        return type(None) in get_args(annotation)
    return origin is getattr(typing, "UnionType", None) and type(None) in get_args(annotation)


def _unwrap_optional(annotation):
    for arg in get_args(annotation):
        if arg is not type(None):
            return arg
    return str


class Model:
    """Fields are plain annotated class attributes:

        model: str = "command-a-03-2025"
        turns: int = 50
        names: list[str] = default(list)
        nested: Other = default(Other)

    A field with no default is required, and a value that cannot be read as the
    annotation says raises `ValidationError` naming every field at once.
    """

    model_fields: dict[str, tuple[Any, Any]] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        hints: dict[str, Any] = {}
        for base in reversed(cls.__mro__):
            hints.update(getattr(base, "__annotations__", {}))
        fields: dict[str, tuple[Any, Any]] = {}
        for name, annotation in hints.items():
            if name.startswith("_") or name == "model_fields":
                continue
            fields[name] = (annotation, getattr(cls, name, MISSING))
        cls.model_fields = fields

    def __init__(self, **values):
        problems: list[str] = []
        for name, (annotation, fallback) in type(self).model_fields.items():
            if name in values:
                try:
                    object.__setattr__(self, name, _coerce(values[name], annotation, name))
                except _Bad as problem:
                    problems.append(str(problem))
            elif fallback is MISSING:
                problems.append(f"{name}: required (expected {_described(annotation)})")
            else:
                produced = fallback.producer() if isinstance(fallback, _Default) else fallback
                object.__setattr__(self, name, produced)
        if problems:
            raise ValidationError(problems)
        # Unknown names are ignored rather than refused: beeagent.json is written
        # by an older or newer BeeCode too, and a key it no longer knows must not
        # stop the program. `load_config` warns about them before the next save
        # drops them.

    def model_dump(self) -> dict:
        """Plain JSON-ready data, recursively."""
        return {name: _plain(getattr(self, name)) for name in type(self).model_fields}

    def __eq__(self, other) -> bool:
        return isinstance(other, type(self)) and other.model_dump() == self.model_dump()

    def __repr__(self) -> str:
        inner = ", ".join(f"{name}={getattr(self, name)!r}" for name in type(self).model_fields)
        return f"{type(self).__name__}({inner})"


def _coerce(value, annotation, path: str):
    """Read `value` as `annotation` or complain about `path`."""
    if annotation is Any or annotation is None:
        return value
    if _is_optional(annotation):
        if value is None:
            return None
        return _coerce(value, _unwrap_optional(annotation), path)
    if isinstance(annotation, type) and issubclass(annotation, Model):
        if isinstance(value, annotation):
            return value
        if isinstance(value, dict):
            try:
                return annotation(**value)
            except ValidationError as e:
                # Name the entry, not just the field: "custom_providers[1].url"
                # is the line the user can act on in beeagent.json.
                raise _Bad("; ".join(f"{path}.{problem}" for problem in e.problems)) from e
        raise _Bad(f"{path}: expected {_described(annotation)}, got {value!r}")

    origin = get_origin(annotation)
    if origin in (list, set, tuple):
        if not isinstance(value, (list, tuple, set)):
            raise _Bad(f"{path}: expected {_described(annotation)}, got {value!r}")
        inner = get_args(annotation)
        item_type = inner[0] if inner else Any
        out = []
        for index, item in enumerate(value):
            out.append(_coerce(item, item_type, f"{path}[{index}]"))
        return list(out) if origin is list else (set(out) if origin is set else tuple(out))
    if origin is dict:
        if not isinstance(value, dict):
            raise _Bad(f"{path}: expected a mapping, got {value!r}")
        args = get_args(annotation)
        key_type, value_type = (args[0], args[1]) if len(args) == 2 else (Any, Any)
        coerced = {}
        for key, item in value.items():
            coerced[_coerce(key, key_type, f"{path}.key")] = _coerce(item, value_type,
                                                                    f"{path}[{key}]")
        return coerced
    if origin is not None:
        return value                      # a generic we do not model — take it as written

    if annotation is bool:
        if isinstance(value, bool):
            return value
        raise _Bad(f"{path}: expected true or false, got {value!r}")
    if annotation is int:
        if isinstance(value, bool):
            raise _Bad(f"{path}: expected a whole number, got {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                raise _Bad(f"{path}: expected a whole number, got {value!r}") from None
        raise _Bad(f"{path}: expected a whole number, got {value!r}")
    if annotation is str:
        if isinstance(value, str):
            return value
        raise _Bad(f"{path}: expected text, got {value!r}")
    if isinstance(value, annotation):
        return value
    raise _Bad(f"{path}: expected {_described(annotation)}, got {value!r}")


def _plain(value):
    if isinstance(value, Model):
        return value.model_dump()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(_plain(item) for item in value)
    return value
