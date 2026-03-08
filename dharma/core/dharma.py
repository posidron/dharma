"""Core grammar engine for Dharma.

Parses .dg grammar files into an in-memory representation and generates
randomized test cases from them. Supports three grammar sections:

  - value:    Named productions with weighted alternatives
  - variable: Stateful object constructors (auto-declaring variables)
  - output:   Entry-point rules for test case assembly
"""
from __future__ import annotations

import logging
import random
import re
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from pathlib import Path
from string import Template
from typing import Any

from .errors import GrammarError, ParseError, XRefError
from .extensions import (
    DharmaConst,
    MetaBlock,
    MetaChoice,
    MetaOptional,
    MetaRange,
    MetaRepeat,
    MetaUnique,
    MetaURI,
)


# ---------------------------------------------------------------------------
# Generation state
# ---------------------------------------------------------------------------

@dataclass
class GenState:
    """Tracks mutable state during a single test-case generation pass."""
    leaf_mode: bool = False
    leaf_trigger: int = 0


# ---------------------------------------------------------------------------
# Grammar sections
# ---------------------------------------------------------------------------

class Section(Enum):
    """The three recognized grammar sections."""
    VALUE = "value"
    VARIABLE = "variable"
    OUTPUT = "output"

    @classmethod
    def from_string(cls, raw: str) -> Section:
        return cls(raw.strip().lower())


# ---------------------------------------------------------------------------
# Token types — every token exposes .generate(state) -> str
# ---------------------------------------------------------------------------

class String:
    """Literal text that needs no further evaluation."""
    __slots__ = ("value", "parent")

    def __init__(self, value: str, parent: DharmaObject) -> None:
        self.value = value
        self.parent = parent

    def generate(self, state: GenState) -> str:
        return self.value


class ValueXRef:
    """Cross-reference to a +value+ rule."""
    __slots__ = ("value", "parent")

    def __init__(self, value: str, parent: DharmaObject) -> None:
        self.value = f"{parent.namespace}:{value}" if ":" not in value else value
        self.parent = parent
        parent.value_xref[self.value] = None

    def generate(self, state: GenState) -> str:
        ref = self.parent.value_xref.get(self.value)
        if ref is None:
            raise XRefError(
                f"Value xref inconsistency in {self.parent.ident} looking for {self.value}"
            )
        return ref.generate(state)


class VariableXRef:
    """Cross-reference to a !variable! rule."""
    __slots__ = ("value", "parent")

    def __init__(self, value: str, parent: DharmaObject) -> None:
        self.value = f"{parent.namespace}:{value}" if ":" not in value else value
        self.parent = parent
        parent.variable_xref[self.value] = None

    def generate(self, state: GenState) -> str:
        ref = self.parent.variable_xref.get(self.value)
        if ref is None:
            raise XRefError(
                f"Variable xref inconsistency in {self.parent.ident} looking for {self.value}"
            )
        return ref.generate(state)


class ElementXRef:
    """Cross-reference to an @element@ (variable definition site)."""
    __slots__ = ("value", "parent")

    def __init__(self, value: str, parent: DharmaObject) -> None:
        self.value = f"{parent.namespace}:{value}" if ":" not in value else value
        self.parent = parent
        parent.element_xref[self.value] = None

    def generate(self, state: GenState) -> str:
        ref = self.parent.element_xref.get(self.value)
        if ref is None:
            raise XRefError(
                f"Element xref inconsistency in {self.parent.ident} looking for {self.value}"
            )
        return ref.generate(state)


class SelfRef:
    """Bare ``@`` placeholder in a variable body — expands to the auto-numbered name.

    This is syntactic sugar so that instead of writing::

        context :=
            let @context@ = canvas.getContext('2d');

    you can write::

        context :=
            let @ = canvas.getContext('2d');
    """
    __slots__ = ("parent",)

    def __init__(self, parent: DharmaObject) -> None:
        self.parent = parent

    def generate(self, state: GenState) -> str:
        # Should never be called — SelfRef is resolved at parse time
        raise RuntimeError("SelfRef.generate() should not be called directly")


# Type alias for a sequence of tokens that together produce one alternative
TokenList = list[Any]


# ---------------------------------------------------------------------------
# Grammar objects — one per named identifier in a section
# ---------------------------------------------------------------------------

class DharmaObject:
    """Base for all grammar section entries (value, variable, output)."""

    def __init__(self, ident: str, machine: DharmaMachine) -> None:
        self.ident: str = f"{machine.namespace}:{ident}"
        self.machine: DharmaMachine = machine
        self.namespace: str = machine.namespace
        self.lineno: int = machine.lineno

        self.alternatives: list[Any] = []
        self.weights: list[float] = []

        self.value_xref: dict[str, DharmaValue | None] = {}
        self.variable_xref: dict[str, DharmaVariable | None] = {}
        self.element_xref: dict[str, DharmaVariable | None] = {}

    # -- helpers --

    def id(self) -> str:  # noqa: A003
        return f"Line {self.lineno} [{self.namespace}]"

    def __hash__(self) -> int:
        return hash(self.ident)

    def add_alternative(self, tokens: Any, weight: float = 1.0) -> None:
        """Append one alternative production with an optional weight."""
        self.alternatives.append(tokens)
        self.weights.append(weight)

    def _choose(self) -> Any:
        """Pick a random alternative respecting weights."""
        return random.choices(self.alternatives, weights=self.weights, k=1)[0]

    @staticmethod
    def eval(tokens: TokenList, state: GenState) -> str:
        """Evaluate a token list into a concrete string."""
        return "".join(tok.generate(state) for tok in tokens)


# ---------------------------------------------------------------------------
# value section
# ---------------------------------------------------------------------------

class DharmaValue(DharmaObject):
    """Manages the ``value`` section — named productions with weighted alternatives.

    Tracks *leaf* alternatives (those containing no recursive ValueXRef or
    MetaRepeat) so the engine can force termination when recursion runs deep.
    """

    def __init__(self, ident: str, machine: DharmaMachine) -> None:
        super().__init__(ident, machine)
        # Leaf tracking
        self.leaf_indices: list[int] = []
        self.leaf_weights: list[float] = []
        # Leaf-path analysis (populated by DharmaMachine._calculate_leaf_paths)
        self.leaf_path: list[tuple[str, str, int]] = []
        self.path_idents: set[str] = set()
        self._minimized_indices: list[int] | None = None
        self._minimized_weights: list[float] | None = None

    def add_alternative(self, tokens: TokenList, weight: float = 1.0) -> None:
        idx = len(self.alternatives)
        super().add_alternative(tokens, weight)
        # If no token is a recursive ref or repeat → this is a leaf
        for tok in tokens:
            if isinstance(tok, (MetaRepeat, ValueXRef)):
                return
        self.leaf_indices.append(idx)
        self.leaf_weights.append(weight)

    # -- leaf-path helpers --

    def n_xrefs(self, tokens: TokenList) -> tuple[bool, bool, int]:
        """Analyse *tokens* for value cross-refs along known leaf paths.

        Returns ``(is_leaf_path, has_repeats, n_xrefs)``
        """
        repeats, n = False, 0
        for tok in tokens:
            if isinstance(tok, ValueXRef):
                n += 1
                if tok.value not in self.path_idents:
                    return False, False, 0
            elif isinstance(tok, MetaRepeat):
                repeats = True
                n += self.n_xrefs(tok.repeat)[2]
        return True, repeats, max(1, min(n, 8))

    def _compute_minimized(self) -> None:
        """Pre-compute a minimal set of alternatives for forced-leaf mode."""
        groups: dict[int, list[int]] = {}
        have_non_repeats = False
        for i, tokens in enumerate(self.alternatives):
            is_leaf, has_rep, n = self.n_xrefs(tokens)
            if not is_leaf:
                continue
            if not has_rep:
                if not have_non_repeats:
                    groups = {}
                    have_non_repeats = True
            if not has_rep or not have_non_repeats:
                groups.setdefault(n, []).append(i)
        for _, indices in sorted(groups.items()):
            self._minimized_indices = indices
            self._minimized_weights = [self.weights[i] for i in indices]
            return
        # Fallback — no leaf path found; use everything to avoid a hard crash
        logging.warning(
            "No path to leaf in force-leaf mode for value %s, using all alternatives",
            self.ident,
        )
        self._minimized_indices = list(range(len(self.alternatives)))
        self._minimized_weights = list(self.weights)

    # -- generation --

    def generate(self, state: GenState) -> str:
        if not state.leaf_mode:
            state.leaf_trigger += 1
            if state.leaf_trigger > DharmaConst.LEAF_TRIGGER:
                state.leaf_mode = True

        if not self.alternatives:
            return ""

        if state.leaf_mode and self.leaf_indices:
            idx = random.choices(self.leaf_indices, weights=self.leaf_weights, k=1)[0]
            tokens = self.alternatives[idx]
        elif state.leaf_mode:
            if self._minimized_indices is None:
                self._compute_minimized()
            assert self._minimized_indices is not None
            assert self._minimized_weights is not None
            idx = random.choices(
                self._minimized_indices, weights=self._minimized_weights, k=1
            )[0]
            tokens = self.alternatives[idx]
        else:
            tokens = self._choose()

        return self.eval(tokens, state)


# ---------------------------------------------------------------------------
# variable section
# ---------------------------------------------------------------------------

class DharmaVariable(DharmaObject):
    """Manages the ``variable`` section — stateful object constructors.

    Each alternative is a ``(prefix_tokens, suffix_tokens)`` tuple.  The
    ``@element@`` marker separates prefix from suffix and becomes the
    auto-incremented variable name at generation time.
    """

    def __init__(self, ident: str, machine: DharmaMachine) -> None:
        super().__init__(ident, machine)
        self.var: str = ident
        self.count: int = 0
        self.default: str = ""

    def clear(self) -> None:
        """Reset between test-case generations."""
        self.count = 0
        self.default = ""

    def generate(self, state: GenState) -> str:
        """Return an existing variable name, or create a new one."""
        var_min = min(DharmaConst.VARIABLE_MIN, DharmaConst.VARIABLE_MAX)
        var_max = max(DharmaConst.VARIABLE_MIN, DharmaConst.VARIABLE_MAX)
        if self.count > 0 and self.count >= random.randint(var_min, var_max):
            return f"{self.var}{random.randint(1, self.count)}"

        prefix_tokens, suffix_tokens = self._choose()
        prefix = self.eval(prefix_tokens, state)
        suffix = self.eval(suffix_tokens, state)
        self.count += 1
        name = f"{self.var}{self.count}"
        self.default += f"{prefix}{name}{suffix}\n"
        return name


# ---------------------------------------------------------------------------
# output section  (formerly 'variance')
# ---------------------------------------------------------------------------

class DharmaOutput(DharmaObject):
    """Manages the ``output`` section — entry-point rules."""

    def generate(self, state: GenState) -> str:
        return self.eval(self._choose(), state)


# ---------------------------------------------------------------------------
# Compiled regex patterns (module-level, compiled once)
# ---------------------------------------------------------------------------

_GRAMMAR_LEVEL_RE = re.compile(
    r"""^(
        (?P<comment>%%%).*|
        %const%\s*(?P<const>[A-Z_]+)\s*:=\s*(?P<value>.*)|
        %require%\s+(?P<require>[a-zA-Z0-9_]+)|
        %section%\s*:=\s*(?P<section>value|variable|output)|
        (?P<ident>[a-zA-Z0-9_]+)\s*:=\s*|
        (?P<empty>\s*)|
        (\t|[ ]+)(?P<assign>.*)
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

# Simple xref pattern:  +value+  !variable!  @element@
_XREF_SIMPLE_RE = re.compile(r"(?P<type>\+|!|@)(?P<xref>[a-zA-Z0-9:_]+)(?P=type)")

# Detects the start of a %keyword%( extension call
_EXT_START_RE = re.compile(r"%(?P<keyword>[a-z]+)%\(", re.IGNORECASE)

# Range argument splitting:  start-end  (non-greedy start to handle -5-5 etc.)
_RANGE_RE = re.compile(r"^(.+?)-(.+)$")

_WEIGHT_RE = re.compile(r"^%weight%\(\s*([0-9.]+)\s*\)\s*")


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class DharmaMachine:
    """Grammar fuzzer engine.

    Parses ``.dg`` grammar files, resolves cross-references, and generates
    randomised test cases.

    Usage::

        machine = DharmaMachine()
        machine.process_grammars([open("grammar.dg")])
        print(machine.generate_content())
    """

    def __init__(
        self,
        prefix: str = "",
        suffix: str = "",
        template: str = "",
    ) -> None:
        # Reset global constants to defaults for a clean slate
        DharmaConst.reset()

        # Parser state
        self.section: Section | None = None
        self.level: str = "top"
        self.namespace: str = ""
        self.lineno: int = 0
        self.current_obj: DharmaObject | None = None

        # Section stores
        self.value: dict[str, DharmaValue] = {}
        self.variable: OrderedDict[str, DharmaVariable] = OrderedDict()
        self.output: dict[str, DharmaOutput] = {}

        # Content wrappers
        self.prefix = prefix
        self.suffix = suffix
        self.template = template

        # Constant tracking — first-writer-wins per grammar namespace
        self._consts_set: dict[str, object] = {}
        self._consts_owner: dict[str, str] = {}  # const name → namespace that set it

        # Required grammar namespaces (declared via %require%)
        self._requires: list[tuple[str, str, int]] = []  # (namespace, required_ns, lineno)

        # Loaded grammar namespaces for %require% validation
        self._loaded_namespaces: set[str] = set()

        # Default grammars auto-loaded before user grammars
        self._default_grammars = ["../grammars/common.dg"]

    # -- identification --

    def id(self) -> str:  # noqa: A003
        return f"Line {self.lineno} [{self.namespace}]"

    def set_namespace(self, name: str) -> None:
        self.namespace = name
        self.lineno = 0

    # ------------------------------------------------------------------
    # Line-level parser
    # ------------------------------------------------------------------

    def parse_line(self, line: str) -> None:
        """Parse one line of a grammar file."""
        self.lineno += 1
        m = _GRAMMAR_LEVEL_RE.match(line)

        if m is None:
            pass
        elif m.group("comment"):
            return
        elif m.group("const"):
            self._handle_const(m.group("const"), m.group("value"))
            return
        elif m.group("require"):
            self._requires.append((self.namespace, m.group("require"), self.lineno))
            return
        elif m.group("section"):
            self._handle_empty_line()
            try:
                self.section = Section.from_string(m.group("section"))
            except ValueError:
                raise ParseError(
                    f"Unknown section: {m.group('section')!r}",
                    self.namespace,
                    self.lineno,
                )
            return
        elif m.group("empty") is not None:
            self._handle_empty_line()
            return
        elif self.section is None:
            raise ParseError("Non-empty line outside any section", self.namespace, self.lineno)
        elif self.level == "top":
            self._handle_top_level(m.group("ident"))
            return
        elif self.level == "assign":
            self._handle_assign_level(m.group("assign"))
            return

        raise ParseError("Unhandled line", self.namespace, self.lineno)

    # ------------------------------------------------------------------
    # Const handler
    # ------------------------------------------------------------------

    def _handle_const(self, const: str, value: str) -> None:
        if not hasattr(DharmaConst, const):
            raise ParseError(
                f"Unknown constant: {const}", self.namespace, self.lineno
            )

        try:
            if value.startswith('"') and value.endswith('"'):
                parsed: str | int | float = value[1:-1]
            elif "." in value:
                parsed = float(value)
            else:
                parsed = int(value)
        except ValueError as exc:
            raise ParseError(
                f"Invalid constant value: {value!r}", self.namespace, self.lineno
            ) from exc

        # First-writer-wins: the first grammar to set a constant owns it.
        # Later grammars can set the same value (silently) but cannot change it.
        owner = self._consts_owner.get(const)
        if owner is not None and owner != self.namespace:
            previous = self._consts_set.get(const)
            if previous != parsed:
                logging.info(
                    "%s: Ignoring %%const%% %s := %s (owned by %s, value: %s)",
                    self.id(), const, value, owner, previous,
                )
                return
        else:
            setattr(DharmaConst, const, parsed)
            self._consts_owner[const] = self.namespace

        self._consts_set[const] = parsed

    # ------------------------------------------------------------------
    # Structural handlers
    # ------------------------------------------------------------------

    def _handle_empty_line(self) -> None:
        if self.current_obj is None:
            pass
        elif not self.current_obj.alternatives:
            raise ParseError("Empty assignment", self.namespace, self.lineno)
        else:
            self._add_section_object()
        self.level = "top"
        self.current_obj = None

    def _handle_top_level(self, ident: str | None) -> None:
        if ident is None:
            raise ParseError("Top level syntax error", self.namespace, self.lineno)

        cls_map: dict[Section, type] = {
            Section.VALUE: DharmaValue,
            Section.VARIABLE: DharmaVariable,
            Section.OUTPUT: DharmaOutput,
        }
        if self.section not in cls_map:
            raise ParseError("Invalid state for top-level", self.namespace, self.lineno)

        self.current_obj = cls_map[self.section](ident, self)
        self.level = "assign"

    def _handle_assign_level(self, assign: str | None) -> None:
        if assign is None:
            raise ParseError("Assign level syntax error", self.namespace, self.lineno)

        # Extract optional weight prefix: %weight%(N) <rest>
        weight = 1.0
        wm = _WEIGHT_RE.match(assign)
        if wm:
            weight = float(wm.group(1))
            assign = assign[wm.end():]

        tokens = self._parse_xrefs(assign)

        match self.section:
            case Section.VALUE:
                self._assign_value(tokens, weight)
            case Section.VARIABLE:
                self._assign_variable(tokens, weight)
            case Section.OUTPUT:
                self._assign_output(tokens, weight)
            case _:
                raise ParseError("Invalid state for assignment", self.namespace, self.lineno)

    # ------------------------------------------------------------------
    # Token / xref parser — balanced-paren scanner
    # ------------------------------------------------------------------

    @staticmethod
    def _find_balanced_close(text: str, open_pos: int) -> int:
        """Find the matching ``)`` for the ``(`` at *open_pos*.

        Respects nesting and quoted strings so that
        ``%repeat%(%range%(0-9), ", ")`` is correctly matched.
        Returns the index of the closing paren, or ``-1``.
        """
        depth = 1
        i = open_pos + 1
        in_quote = False
        while i < len(text):
            ch = text[i]
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_quote = not in_quote
            elif not in_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1

    @staticmethod
    def _split_top_level(text: str, delim: str = ",") -> list[str]:
        """Split *text* on *delim*, but only at the top nesting level.

        Parentheses and double-quoted strings are respected.
        """
        parts: list[str] = []
        depth = 0
        in_quote = False
        start = 0
        for i, ch in enumerate(text):
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_quote = not in_quote
            elif not in_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == delim and depth == 0:
                    parts.append(text[start:i])
                    start = i + 1
        parts.append(text[start:])
        return parts

    def _parse_xrefs(self, text: str) -> TokenList:
        """Expand a raw string into a list of typed tokens.

        Uses a left-to-right scanner with balanced-parenthesis matching,
        so nested extensions like ``%repeat%(%range%(0-9), ", ")`` work
        correctly.
        """
        assert self.current_obj is not None
        text = text.replace("\\n", "\n")
        out: TokenList = []
        pos = 0
        literal_start = 0

        while pos < len(text):
            # --- xref markers: +value+  !variable!  @element@  or bare @ ---
            if text[pos] in ("+", "!", "@"):
                # Check for bare @ (self-reference — only valid in variable section)
                if (
                    text[pos] == "@"
                    and self.section == Section.VARIABLE
                    and (pos + 1 >= len(text) or not text[pos + 1].isalnum())
                ):
                    if pos > literal_start:
                        out.append(String(text[literal_start:pos], self.current_obj))
                    out.append(SelfRef(self.current_obj))
                    pos += 1
                    literal_start = pos
                    continue
                m = _XREF_SIMPLE_RE.match(text, pos)
                if m:
                    if pos > literal_start:
                        out.append(String(text[literal_start:pos], self.current_obj))
                    xref_cls: type = {
                        "+": ValueXRef,
                        "!": VariableXRef,
                        "@": ElementXRef,
                    }[m.group("type")]
                    out.append(xref_cls(m.group("xref"), self.current_obj))
                    pos = m.end()
                    literal_start = pos
                    continue

            # --- %keyword%(...) extension calls ---
            if text[pos] == "%":
                m = _EXT_START_RE.match(text, pos)
                if m:
                    open_paren = m.end() - 1  # index of '('
                    close = self._find_balanced_close(text, open_paren)
                    if close >= 0:
                        if pos > literal_start:
                            out.append(String(text[literal_start:pos], self.current_obj))
                        args_text = text[open_paren + 1 : close]
                        out.append(self._build_extension(m.group("keyword").lower(), args_text))
                        pos = close + 1
                        literal_start = pos
                        continue

            pos += 1

        if literal_start < len(text):
            out.append(String(text[literal_start:], self.current_obj))

        return out

    def _build_extension(self, keyword: str, args: str) -> Any:
        """Construct the appropriate extension token for *keyword* with *args*."""
        assert self.current_obj is not None
        match keyword:
            case "uri":
                return MetaURI(args.strip(), self.current_obj)
            case "block":
                return MetaBlock(args.strip(), self.current_obj)
            case "range":
                m = _RANGE_RE.match(args)
                if not m:
                    raise ParseError(
                        f"Malformed %range% arguments: {args!r}",
                        self.namespace,
                        self.lineno,
                    )
                return MetaRange(m.group(1), m.group(2), self.current_obj)
            case "choice":
                return MetaChoice(args.strip(), self.current_obj)
            case "repeat":
                return self._build_repeat(args)
            case "optional":
                return self._build_optional(args)
            case "unique":
                return self._build_unique(args)
            case _:
                raise ParseError(
                    f"Unknown extension: %{keyword}%",
                    self.namespace,
                    self.lineno,
                )

    def _build_repeat(self, args: str) -> MetaRepeat:
        """Parse ``%repeat%`` arguments: ``content[, "sep"][, nodups]``."""
        assert self.current_obj is not None
        parts = self._split_top_level(args)
        content = parts[0].strip()
        separator = ""
        nodups = ""
        if len(parts) >= 2:
            sep = parts[1].strip()
            if sep.startswith('"') and sep.endswith('"'):
                separator = sep[1:-1]
            else:
                separator = sep
        if len(parts) >= 3:
            nodups = parts[2].strip()
        tokens = self._parse_xrefs(content)
        return MetaRepeat(tokens, separator, nodups, self.current_obj)

    def _build_optional(self, args: str) -> MetaOptional:
        """Parse ``%optional%`` arguments: ``content, probability``."""
        assert self.current_obj is not None
        parts = self._split_top_level(args)
        if len(parts) < 2:
            raise ParseError(
                "%%optional%% requires content and probability",
                self.namespace,
                self.lineno,
            )
        tokens = self._parse_xrefs(parts[0].strip())
        probability = float(parts[-1].strip())
        return MetaOptional(tokens, probability, self.current_obj)

    def _build_unique(self, args: str) -> MetaUnique:
        """Parse ``%unique%`` arguments: ``expr, expr[, expr ...]``."""
        assert self.current_obj is not None
        parts = self._split_top_level(args)
        if len(parts) < 2:
            raise ParseError(
                "%%unique%% requires at least two expressions",
                self.namespace,
                self.lineno,
            )
        token_groups = [self._parse_xrefs(p.strip()) for p in parts]
        return MetaUnique(token_groups, self.current_obj)

    # ------------------------------------------------------------------
    # Section-specific assignment handlers
    # ------------------------------------------------------------------

    def _assign_value(self, tokens: TokenList, weight: float) -> None:
        if not isinstance(self.current_obj, DharmaValue):
            raise ParseError("Value in non-value context", self.namespace, self.lineno)
        self.current_obj.add_alternative(tokens, weight)

    def _assign_variable(self, tokens: TokenList, weight: float) -> None:
        """Parse a variable line, splitting at the @ or @element@ marker."""
        element_idx: int | None = None
        variable: str | None = None
        for i, tok in enumerate(tokens):
            if isinstance(tok, SelfRef):
                # Bare @ — convert to an ElementXRef pointing to this variable
                variable = self.current_obj.ident
                element_idx = i
                # Register the self-reference in the xref table
                self.current_obj.element_xref[variable] = None
                break
            if isinstance(tok, ElementXRef):
                variable = tok.value
                element_idx = i
                break

        if variable is None or element_idx is None:
            raise ParseError(
                "Variable line missing @ placeholder or @element@ marker",
                self.namespace,
                self.lineno,
            )
        if variable != self.current_obj.ident:
            raise ParseError("Variable name mismatch", self.namespace, self.lineno)
        if not isinstance(self.current_obj, DharmaVariable):
            raise ParseError("Inconsistent object for variable", self.namespace, self.lineno)

        prefix, suffix = tokens[:element_idx], tokens[element_idx + 1:]
        self.current_obj.add_alternative((prefix, suffix), weight)

    def _assign_output(self, tokens: TokenList, weight: float) -> None:
        if not isinstance(self.current_obj, DharmaOutput):
            raise ParseError("Inconsistent object for output", self.namespace, self.lineno)
        self.current_obj.add_alternative(tokens, weight)

    # ------------------------------------------------------------------
    # Object registration
    # ------------------------------------------------------------------

    def _add_section_object(self) -> None:
        assert self.current_obj is not None
        store = {
            Section.VALUE: self.value,
            Section.VARIABLE: self.variable,
            Section.OUTPUT: self.output,
        }.get(self.section)

        if store is None:
            raise ParseError("Inconsistent section state", self.namespace, self.lineno)
        if self.current_obj.ident in store:
            # Merge alternatives from duplicate definitions instead of replacing.
            # This allows multiple grammars to contribute to the same rule
            # (e.g. both webgpu.dg and webgl.dg defining 'main' in the output section).
            existing = store[self.current_obj.ident]
            for i, alt in enumerate(self.current_obj.alternatives):
                existing.add_alternative(alt, self.current_obj.weights[i])
            # Also merge xref tables
            existing.value_xref.update(self.current_obj.value_xref)
            existing.variable_xref.update(self.current_obj.variable_xref)
            existing.element_xref.update(self.current_obj.element_xref)
            logging.debug(
                "%s: '%s' merged %d alternatives into existing %s definition",
                self.id(),
                self.current_obj.ident,
                len(self.current_obj.alternatives),
                self.section.value,
            )
            return
        store[self.current_obj.ident] = self.current_obj  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Cross-reference resolution
    # ------------------------------------------------------------------

    def _resolve_xref(self) -> None:
        """Wire up all +value+, !variable!, and @element@ references."""
        for obj in chain(
            self.value.values(),
            self.variable.values(),
            self.output.values(),
        ):
            for key in list(obj.value_xref):
                if key not in self.value:
                    raise XRefError(f"Undefined value reference from {obj.ident} to {key}")
                obj.value_xref[key] = self.value[key]

            for key in list(obj.variable_xref):
                if key not in self.variable:
                    raise XRefError(f"Undefined variable reference from {obj.ident} to {key}")
                obj.variable_xref[key] = self.variable[key]

            for key in list(obj.element_xref):
                if key not in self.variable:
                    raise XRefError(f"Element reference without matching variable from {obj.ident} to {key}")
                obj.element_xref[key] = self.variable[key]

    # ------------------------------------------------------------------
    # Leaf-path analysis
    # ------------------------------------------------------------------

    def _calculate_leaf_paths(self) -> None:
        """Build a reverse-xref map and mark paths from every value back to leaves."""
        reverse_xref: dict[str, list[str]] = {}
        leaves: set[DharmaValue] = set()

        for v in self.value.values():
            if v.leaf_indices:
                leaves.add(v)
            for xref_name in v.value_xref:
                reverse_xref.setdefault(xref_name, []).append(v.ident)

        for leaf in leaves:
            if leaf.ident not in reverse_xref:
                continue
            for name in reverse_xref[leaf.ident]:
                target = self.value[name]
                target.leaf_path.append((leaf.ident, leaf.ident, 0))
                target.path_idents.add(leaf.ident)
                self._propagate_leaf(leaf.ident, target, {target}, 1, reverse_xref)

    def _propagate_leaf(
        self,
        leaf_ident: str,
        obj: DharmaValue,
        seen: set[DharmaValue],
        depth: int,
        reverse_xref: dict[str, list[str]],
    ) -> None:
        if obj.ident not in reverse_xref:
            return
        for name in reverse_xref[obj.ident]:
            target = self.value[name]
            target.leaf_path.append((leaf_ident, obj.ident, depth))
            target.path_idents.add(obj.ident)
            if target in seen:
                continue
            seen.add(target)
            self._propagate_leaf(leaf_ident, target, seen, depth + 1, reverse_xref)

    # ------------------------------------------------------------------
    # Content generation
    # ------------------------------------------------------------------

    def generate_content(self) -> str:
        """Generate one complete test case as a string."""
        if not self.output:
            raise GrammarError("No output section defined in grammar")

        # Capture seed state for reproducibility
        seed_state = random.getstate()
        seed_ints = seed_state[1] if len(seed_state) > 1 else ()
        seed_tag = ":".join(str(x) for x in seed_ints[:3]) if seed_ints else "unknown"

        # Reset variable state
        for var in self.variable.values():
            var.clear()

        # Generate output statements
        outputs: list[str] = []
        out_min = min(DharmaConst.OUTPUT_MIN, DharmaConst.OUTPUT_MAX)
        out_max = max(DharmaConst.OUTPUT_MIN, DharmaConst.OUTPUT_MAX)
        count = random.randint(out_min, out_max)
        for _ in range(count):
            rule = random.choice(list(self.output.values()))
            generated = rule.generate(GenState())
            try:
                outputs.append(DharmaConst.OUTPUT_TEMPLATE % generated)
            except TypeError:
                outputs.append(generated)
            outputs.append("\n")

        # Collect variable declarations in dependency order.
        # Variables that reference other variables (via !var!) come after them.
        variables: list[str] = []
        for var in self._ordered_variables():
            if var.default:
                try:
                    variables.append(DharmaConst.OUTPUT_TEMPLATE % var.default)
                except TypeError:
                    variables.append(var.default)
                variables.append("\n")

        # Assemble with seed comment for reproducibility
        seed_comment = f"/* dharma seed: {seed_tag} */\n"
        content = "".join(chain([self.prefix, seed_comment], variables, outputs, [self.suffix]))
        if self.template:
            return Template(self.template).safe_substitute(testcase_content=content)
        return content

    def _ordered_variables(self) -> list[DharmaVariable]:
        """Return variables in dependency order (topological sort).

        Variables that reference other variables (via ``!var!``) are placed
        after the variables they depend on.  Cycles are silently tolerated
        (original insertion order is used as tiebreaker).
        """
        # Build adjacency: var -> set of vars it depends on
        all_vars = list(self.variable.values())
        name_to_var = {v.ident: v for v in all_vars}
        deps: dict[str, set[str]] = {v.ident: set() for v in all_vars}
        for var in all_vars:
            for ref_name in var.variable_xref:
                if ref_name in name_to_var and ref_name != var.ident:
                    deps[var.ident].add(ref_name)

        # Kahn's algorithm
        ordered: list[DharmaVariable] = []
        remaining = {v.ident for v in all_vars}
        var_keys = list(self.variable.keys())
        key_index = {k: i for i, k in enumerate(var_keys)}
        while remaining:
            # Find vars with no unmet dependencies
            ready = [n for n in remaining if not (deps[n] & remaining - {n})]
            if not ready:
                # Cycle — add everything remaining in original order
                ordered.extend(v for v in all_vars if v.ident in remaining)
                break
            # Sort by original insertion order for determinism
            ready.sort(key=lambda n: key_index.get(n, 0))
            for n in ready:
                remaining.discard(n)
                ordered.append(name_to_var[n])
        return ordered

    def generate_testcases(self, path: str | Path, filetype: str, count: int) -> None:
        """Write *count* generated test cases to *path*."""
        out = Path(path)
        out.mkdir(parents=True, exist_ok=True)
        for n in range(count):
            (out / f"{n + 1}.{filetype}").write_text(self.generate_content())

    # ------------------------------------------------------------------
    # Grammar loading
    # ------------------------------------------------------------------

    def process_grammars(self, grammars: list) -> None:
        """Parse all supplied grammar file objects into the machine."""
        # Collect user-supplied grammar basenames to avoid duplicating defaults
        user_stems = {Path(fo.name).stem for fo in grammars}

        # Prepend built-in grammars (e.g. common.dg) unless already supplied
        for rel in self._default_grammars:
            resolved = (Path(__file__).parent / rel).resolve()
            if resolved.stem in user_stems:
                logging.debug("Skipping default grammar %s (already supplied)", resolved.name)
                continue
            if resolved.exists():
                grammars.insert(0, resolved.open())  # noqa: SIM115
            else:
                logging.warning("Default grammar not found: %s", resolved)

        for fo in grammars:
            logging.debug("Processing grammar: %s", fo.name)
            self.set_namespace(Path(fo.name).stem)
            self._loaded_namespaces.add(self.namespace)
            try:
                for line in fo:
                    self.parse_line(line)
                self._handle_empty_line()
            finally:
                fo.close()

        # Validate %require% directives
        for ns, required, lineno in self._requires:
            if required not in self._loaded_namespaces:
                raise XRefError(
                    f"Grammar '{ns}' (line {lineno}) requires '{required}' "
                    f"but it was not loaded. Add it to -grammars."
                )

        self._resolve_xref()
        self._calculate_leaf_paths()
