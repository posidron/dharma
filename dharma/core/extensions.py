"""Dharma grammar extensions and configuration constants."""
from __future__ import annotations

import math
import random
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dharma import GenState, DharmaObject


class DharmaConst:
    """Configuration settings for the Dharma generator.

    Override per-grammar via %const% directives.

    Example:
        %const% OUTPUT_MAX := 10
        %const% OUTPUT_TEMPLATE := "try { %s } catch(e) {}"
    """

    URI_TABLE: dict[str, str] = {}
    LEAF_TRIGGER: int = 256
    MAX_REPEAT_POWER: int = 12
    OUTPUT_TEMPLATE: str = "%s"
    OUTPUT_MIN: int = 1
    OUTPUT_MAX: int = 8
    VARIABLE_MIN: int = 1
    VARIABLE_MAX: int = 4

    @classmethod
    def reset(cls) -> None:
        """Reset all configuration to defaults."""
        cls.URI_TABLE = {}
        cls.LEAF_TRIGGER = 256
        cls.MAX_REPEAT_POWER = 12
        cls.OUTPUT_TEMPLATE = "%s"
        cls.OUTPUT_MIN = 1
        cls.OUTPUT_MAX = 8
        cls.VARIABLE_MIN = 1
        cls.VARIABLE_MAX = 4


class MetaBlock:
    """Load code fragments from a file into the grammar.

    Grammar syntax: %block%(path/to/file)
    """

    __slots__ = ("parent", "content")

    def __init__(self, path: str, parent: DharmaObject) -> None:
        self.parent = parent
        resolved = Path(path).expanduser()
        if resolved.exists():
            self.content = resolved.read_text()
        else:
            logging.warning('%s: Unable to load resource for block() "%s"', parent.id(), path)
            self.content = path

    def generate(self, state: GenState) -> str:
        return self.content


class MetaURI:
    """Load a random file URI into the generated code.

    Grammar syntax: %uri%(path_or_key)
    If the path matches a key in DharmaConst.URI_TABLE, the mapped path is used.
    """

    __slots__ = ("parent", "paths")

    def __init__(self, path: str, parent: DharmaObject) -> None:
        self.parent = parent
        if path in DharmaConst.URI_TABLE:
            path = DharmaConst.URI_TABLE[path]
        resolved = Path(path).expanduser()
        if resolved.is_dir():
            self.paths = [str(p) for p in resolved.iterdir() if p.is_file()]
        elif resolved.exists():
            self.paths = [str(resolved)]
        else:
            logging.warning('%s: Unable to identify argument of uri() "%s"', parent.id(), path)
            self.paths = [path]

    def generate(self, state: GenState) -> str:
        return random.choice(self.paths)


class MetaRepeat:
    """Repeat an arbitrary expression a random number of times.

    Grammar syntax:
        %repeat%(+expr+)
        %repeat%(+expr+, ", ")
        %repeat%(+expr+, ", ", nodups)
    """

    __slots__ = ("parent", "repeat", "separator", "nodups")

    def __init__(self, repeat: list[Any], separator: str, nodups: str, parent: DharmaObject) -> None:
        self.parent = parent
        self.repeat = repeat
        self.separator = separator
        self.nodups = bool(nodups)

    def generate(self, state: GenState) -> str:
        count = random.randint(1, int(math.pow(2, random.randint(1, DharmaConst.MAX_REPEAT_POWER))))
        strings = [self.parent.eval(self.repeat, state) for _ in range(count)]
        if self.nodups:
            strings = list(dict.fromkeys(strings))  # Preserve insertion order (unlike set)
        return self.separator.join(strings)


class MetaChoice:
    """Choose a random item from a comma-separated list.

    Grammar syntax: %choice%(foo, bar, baz)
    """

    __slots__ = ("parent", "choices")

    def __init__(self, choices: str, parent: DharmaObject) -> None:
        self.parent = parent
        self.choices = [x.strip() for x in choices.split(",")]

    def generate(self, state: GenState) -> str:
        return random.choice(self.choices)


class MetaRange:
    """Generate a random value within a range.

    Grammar syntax:
        %range%(0-9)        → int
        %range%(0.0-1.0)    → float
        %range%(a-z)        → char
        %range%(0x00-0xFF)  → hex
    """

    __slots__ = ("parent", "a", "b", "base", "fmt")

    def __init__(self, a: str, b: str, parent: DharmaObject) -> None:
        self.parent = parent
        self.base: int | None = None
        self.fmt: str = ""
        self.a: int | float = 0
        self.b: int | float = 0

        if a is None or b is None:
            raise ValueError(f"{parent.id()}: Malformed 'range' meta")

        if self._is_char(a) and self._is_char(b):
            self.a, self.b = ord(a), ord(b)
            self.fmt = "c"
        elif self._is_float(a) and self._is_float(b):
            self.a, self.b = float(a), float(b)
            self.fmt = "f"
        elif self._is_float(a) != self._is_float(b):
            raise ValueError(
                f"{parent.id()}: Type mismatch in 'range' meta in {parent.ident}"
            )
        else:
            self.fmt = "i"
            self.base = 16 if (self._is_hex(a) and self._is_hex(b)) else 0
            try:
                if self.base:
                    self.a, self.b = int(a, self.base), int(b, self.base)
                else:
                    self.a, self.b = int(a), int(b)
            except ValueError as exc:
                raise ValueError(
                    f"{parent.id()}: Conversion error in 'range' meta"
                ) from exc

    @staticmethod
    def _is_char(x: str) -> bool:
        return len(x) == 1

    @staticmethod
    def _is_float(x: str) -> bool:
        return "." in x

    @staticmethod
    def _is_hex(x: str) -> bool:
        return x.startswith("0x") or x.startswith("0X")

    def generate(self, state: GenState) -> str:
        match self.fmt:
            case "c":
                lo, hi = min(int(self.a), int(self.b)), max(int(self.a), int(self.b))
                return "%c" % random.randint(lo, hi)
            case "f":
                return "%g" % random.uniform(self.a, self.b)
            case "i":
                lo, hi = min(int(self.a), int(self.b)), max(int(self.a), int(self.b))
                value = random.randint(lo, hi)
                return "%x" % value if self.base == 16 else "%d" % value
            case _:
                return ""


class MetaOptional:
    """Conditionally include content based on a probability.

    Grammar syntax: %optional%(+expr+, 0.5)
    Includes the expression with the given probability (0.0 to 1.0).
    Generates empty string otherwise.
    """

    __slots__ = ("parent", "tokens", "probability")

    def __init__(self, tokens: list[Any], probability: float, parent: DharmaObject) -> None:
        self.parent = parent
        self.tokens = tokens
        self.probability = max(0.0, min(1.0, probability))

    def generate(self, state: GenState) -> str:
        if random.random() < self.probability:
            return self.parent.eval(self.tokens, state)
        return ""


class MetaUnique:
    """Generate distinct values for two or more variable references.

    Grammar syntax: %unique%(!var!, !var!)
    Re-rolls the second operand (up to 20 attempts) when it matches the first.
    Falls back to whatever was generated if all attempts match.
    """

    __slots__ = ("parent", "token_groups")

    def __init__(self, token_groups: list[list[Any]], parent: DharmaObject) -> None:
        self.parent = parent
        self.token_groups = token_groups

    def generate(self, state: GenState) -> str:
        if not self.token_groups:
            return ""
        first = self.parent.eval(self.token_groups[0], state)
        results = [first]
        for group in self.token_groups[1:]:
            candidate = self.parent.eval(group, state)
            for _ in range(20):
                if candidate not in results:
                    break
                candidate = self.parent.eval(group, state)
            results.append(candidate)
        return ", ".join(results)
