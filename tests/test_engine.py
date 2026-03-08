"""Tests for the Dharma grammar engine and extensions."""
from __future__ import annotations

import io
import random
from pathlib import Path

import pytest

from dharma.core.dharma import (
    DharmaMachine,
    DharmaValue,
    DharmaVariable,
    GenState,
    Section,
    String,
)
from dharma.core.extensions import DharmaConst, MetaRange


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GRAMMAR_DIR = Path(__file__).parent.parent / "dharma" / "grammars"

ALL_GRAMMARS = sorted(p.stem for p in GRAMMAR_DIR.glob("*.dg") if p.stem != "common")


def _make_machine(text: str, name: str = "test") -> DharmaMachine:
    """Parse a grammar string into a DharmaMachine."""
    g = io.StringIO(text)
    g.name = f"{name}.dg"
    m = DharmaMachine()
    m._default_grammars = []
    m.process_grammars([g])
    return m


def _simple_grammar(value_body: str = "hello", output_body: str = "+x+") -> str:
    return (
        f"%section% := value\nx :=\n\t{value_body}\n\n"
        f"%section% := output\nmain :=\n\t{output_body}\n"
    )


# ---------------------------------------------------------------------------
# DharmaConst
# ---------------------------------------------------------------------------

class TestDharmaConst:
    def test_reset_on_new_machine(self):
        DharmaConst.OUTPUT_MIN = 99
        DharmaMachine()  # __init__ calls reset()
        assert DharmaConst.OUTPUT_MIN == 1

    def test_first_writer_wins(self):
        g1 = io.StringIO("%const% OUTPUT_MIN := 5\n%section% := value\na :=\n\t1\n\n%section% := output\nm :=\n\t+a+\n")
        g1.name = "first.dg"
        g2 = io.StringIO("%const% OUTPUT_MIN := 99\n%section% := value\nb :=\n\t2\n")
        g2.name = "second.dg"
        m = DharmaMachine()
        m._default_grammars = []
        m.process_grammars([g1, g2])
        assert DharmaConst.OUTPUT_MIN == 5  # first grammar wins


# ---------------------------------------------------------------------------
# MetaRange
# ---------------------------------------------------------------------------

class TestMetaRange:
    def test_inverted_range(self):
        class FP:
            def id(self): return "t"
            ident = "t"
        r = MetaRange("10", "5", FP())
        for _ in range(50):
            v = int(r.generate(GenState()))
            assert 5 <= v <= 10

    def test_negative_range(self):
        class FP:
            def id(self): return "t"
            ident = "t"
        r = MetaRange("-10", "10", FP())
        for _ in range(50):
            v = int(r.generate(GenState()))
            assert -10 <= v <= 10


# ---------------------------------------------------------------------------
# Variable generation
# ---------------------------------------------------------------------------

class TestVariable:
    def test_count_zero_min_max_zero(self):
        """Variable with count=0 and MIN=MAX=0 should not crash."""
        DharmaConst.reset()
        DharmaConst.VARIABLE_MIN = 0
        DharmaConst.VARIABLE_MAX = 0
        m = DharmaMachine()
        m._default_grammars = []
        m.set_namespace("test")
        m.section = Section.VARIABLE
        v = DharmaVariable("thing", m)
        v.count = 0
        v.add_alternative(([String("let ", v)], [String(" = 1;", v)]), 1.0)
        name = v.generate(GenState())
        assert name == "thing1"
        DharmaConst.reset()

    def test_variable_ordering(self):
        """Variables with dependencies should be ordered correctly."""
        text = (
            "%section% := variable\n"
            "device :=\n\t@ = !adapter!.req();\n\n"
            "adapter :=\n\t@ = nav.req();\n\n"
            "%section% := value\ndummy :=\n\tval\n\n"
            "%section% := output\nmain :=\n\t+dummy+\n"
        )
        m = _make_machine(text)
        ordered = m._ordered_variables()
        names = [v.ident for v in ordered]
        assert names.index("test:adapter") < names.index("test:device")


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------

class TestDirectives:
    def test_require_missing(self):
        text = "%require% nonexistent\n%section% := value\na :=\n\tx\n\n%section% := output\nm :=\n\t+a+\n"
        g = io.StringIO(text)
        g.name = "req.dg"
        m = DharmaMachine()
        m._default_grammars = []
        with pytest.raises(Exception, match="requires 'nonexistent'"):
            m.process_grammars([g])

    def test_require_satisfied(self):
        text = "%require% common\n%section% := value\na :=\n\t+common:number+\n\n%section% := output\nm :=\n\t+a+\n"
        g = io.StringIO(text)
        g.name = "req.dg"
        m = DharmaMachine()
        # common.dg auto-loads
        m.process_grammars([g])
        random.seed(42)
        result = m.generate_content()
        assert result  # Should produce something

    def test_invalid_const_value(self):
        text = "%const% OUTPUT_MIN := abc\n%section% := value\na :=\n\tx\n\n%section% := output\nm :=\n\t+a+\n"
        g = io.StringIO(text)
        g.name = "bad.dg"
        m = DharmaMachine()
        m._default_grammars = []
        with pytest.raises(Exception, match="Invalid constant value"):
            m.process_grammars([g])


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

class TestExtensions:
    def test_weight_zero(self):
        m = _make_machine(
            "%section% := value\nx :=\n\t%weight%(0) never\n\t%weight%(1) always\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        random.seed(42)
        for _ in range(100):
            r = m.generate_content()
            assert "never" not in r

    def test_unique_distinct(self):
        m = _make_machine(
            "%const% VARIABLE_MIN := 3\n%const% VARIABLE_MAX := 5\n"
            "%section% := value\nitem :=\n\ta\n\tb\n\tc\n\td\n\te\n\n"
            "%section% := variable\nthing :=\n\t@ = +item+;\n\n"
            "%section% := output\nmain :=\n\t%unique%(!thing!, !thing!)\n"
        )
        random.seed(42)
        for _ in range(50):
            r = m.generate_content()
            # Extract the unique pair from the output
            # The pair is formatted as "thing1, thing2" somewhere in the output
            lines = r.strip().split("\n")
            for line in lines:
                if ", " in line and "thing" in line:
                    parts = line.strip().split(", ")
                    if len(parts) == 2 and parts[0].startswith("thing") and parts[1].startswith("thing"):
                        assert parts[0] != parts[1], f"Got duplicate: {parts}"

    def test_optional_skips(self):
        m = _make_machine(
            "%section% := value\nx :=\n\t%optional%(PRESENT, 0.0)\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        random.seed(42)
        for _ in range(20):
            r = m.generate_content()
            assert "PRESENT" not in r

    def test_optional_includes(self):
        m = _make_machine(
            "%section% := value\nx :=\n\t%optional%(PRESENT, 1.0)\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        random.seed(42)
        for _ in range(20):
            r = m.generate_content()
            assert "PRESENT" in r


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class TestGeneration:
    def test_basic_output(self):
        m = _make_machine(_simple_grammar())
        random.seed(42)
        r = m.generate_content()
        assert "hello" in r

    def test_seed_comment(self):
        m = _make_machine(_simple_grammar())
        random.seed(42)
        r = m.generate_content()
        assert "/* dharma seed:" in r

    def test_recursive_terminates(self):
        m = _make_machine(
            "%section% := value\nx :=\n\t+x+ +x+\n\tleaf\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        random.seed(42)
        r = m.generate_content()
        assert "leaf" in r

    def test_output_template(self):
        m = _make_machine(
            '%const% OUTPUT_TEMPLATE := "wrap(%s)"\n'
            "%section% := value\nx :=\n\thello\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        random.seed(42)
        r = m.generate_content()
        assert "wrap(hello)" in r

    def test_stats_tracking(self):
        m = _make_machine(_simple_grammar())
        random.seed(42)
        for _ in range(10):
            m.generate_content()
        assert m._stats_total == 10
        assert sum(m._stats_output.values()) > 0


# ---------------------------------------------------------------------------
# Grammar file validation (dry-run all grammars)
# ---------------------------------------------------------------------------

class TestGrammars:
    @pytest.mark.parametrize("grammar", ALL_GRAMMARS)
    def test_grammar_loads(self, grammar: str):
        """Every .dg file should parse without errors."""
        path = GRAMMAR_DIR / f"{grammar}.dg"
        m = DharmaMachine()
        with open(path) as f:
            m.process_grammars([f])

    @pytest.mark.parametrize("grammar", ALL_GRAMMARS)
    def test_grammar_generates(self, grammar: str):
        """Every grammar should produce non-empty output."""
        path = GRAMMAR_DIR / f"{grammar}.dg"
        random.seed(42)
        m = DharmaMachine()
        with open(path) as f:
            m.process_grammars([f])
        result = m.generate_content()
        assert len(result) > 10, f"Grammar {grammar} produced too little output"


# ---------------------------------------------------------------------------
# Rule merging
# ---------------------------------------------------------------------------

class TestRuleMerging:
    def test_same_rule_merges_alternatives(self):
        """Two grammars defining the same rule should merge alternatives."""
        g1 = io.StringIO(
            "%section% := value\nx :=\n\tfrom_g1\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        g1.name = "a.dg"
        g2 = io.StringIO(
            "%section% := value\nx :=\n\tfrom_g2\n"
        )
        g2.name = "a.dg"  # same namespace
        m = DharmaMachine()
        m._default_grammars = []
        m.process_grammars([g1, g2])
        assert len(m.value["a:x"].alternatives) == 2

    def test_cross_namespace_merge(self):
        """Different namespaces create separate rules."""
        g1 = io.StringIO(
            "%section% := value\nx :=\n\tg1\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        g1.name = "ns1.dg"
        g2 = io.StringIO(
            "%section% := value\nx :=\n\tg2\n"
        )
        g2.name = "ns2.dg"
        m = DharmaMachine()
        m._default_grammars = []
        m.process_grammars([g1, g2])
        assert "ns1:x" in m.value
        assert "ns2:x" in m.value

    def test_output_merge(self):
        """Output rules from multiple grammars merge."""
        g1 = io.StringIO(
            "%section% := value\na :=\n\t1\n\n"
            "%section% := output\nmain :=\n\t+a+\n"
        )
        g1.name = "m.dg"
        g2 = io.StringIO(
            "%section% := value\nb :=\n\t2\n\n"
            "%section% := output\nmain :=\n\t+b+\n"
        )
        g2.name = "m.dg"
        m = DharmaMachine()
        m._default_grammars = []
        m.process_grammars([g1, g2])
        assert len(m.output["m:main"].alternatives) == 2


# ---------------------------------------------------------------------------
# Remaining extensions
# ---------------------------------------------------------------------------

class TestMoreExtensions:
    def test_choice(self):
        m = _make_machine(
            "%section% := value\nx :=\n\t%choice%(alpha, beta, gamma)\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        random.seed(42)
        results = {m.generate_content().strip().split("\n")[-1] for _ in range(100)}
        assert "alpha" in results or "beta" in results or "gamma" in results

    def test_repeat_with_separator(self):
        m = _make_machine(
            "%section% := value\n"
            "item :=\n\tX\n\n"
            "list :=\n\t%repeat%(+item+, \", \")\n\n"
            "%section% := output\nmain :=\n\t+list+\n"
        )
        random.seed(42)
        r = m.generate_content()
        assert "X" in r

    def test_repeat_nodups(self):
        m = _make_machine(
            "%section% := value\n"
            "item :=\n\tonly\n\n"
            "list :=\n\t%repeat%(+item+, \"-\", nodups)\n\n"
            "%section% := output\nmain :=\n\t+list+\n"
        )
        random.seed(42)
        r = m.generate_content()
        # With nodups and only one possible value, should produce just "only"
        assert "only-only" not in r

    def test_range_float(self):
        from dharma.core.extensions import MetaRange
        class FP:
            def id(self): return "t"
            ident = "t"
        r = MetaRange("0.0", "1.0", FP())
        for _ in range(50):
            v = float(r.generate(GenState()))
            assert 0.0 <= v <= 1.0

    def test_range_char(self):
        from dharma.core.extensions import MetaRange
        class FP:
            def id(self): return "t"
            ident = "t"
        r = MetaRange("a", "z", FP())
        for _ in range(50):
            c = r.generate(GenState())
            assert len(c) == 1 and "a" <= c <= "z"

    def test_range_hex(self):
        from dharma.core.extensions import MetaRange
        class FP:
            def id(self): return "t"
            ident = "t"
        r = MetaRange("0x00", "0xFF", FP())
        for _ in range(50):
            v = int(r.generate(GenState()), 16)
            assert 0 <= v <= 255


# ---------------------------------------------------------------------------
# Cross-grammar references
# ---------------------------------------------------------------------------

class TestCrossGrammarRefs:
    def test_namespace_value_ref(self):
        g1 = io.StringIO(
            "%section% := value\ncolor :=\n\tred\n\tblue\n\n"
        )
        g1.name = "lib.dg"
        g2 = io.StringIO(
            "%section% := value\nstyle :=\n\t+lib:color+\n\n"
            "%section% := output\nmain :=\n\t+style+\n"
        )
        g2.name = "app.dg"
        m = DharmaMachine()
        m._default_grammars = []
        m.process_grammars([g1, g2])
        random.seed(42)
        r = m.generate_content()
        assert "red" in r or "blue" in r

    def test_undefined_ref_raises(self):
        text = (
            "%section% := value\nx :=\n\t+nonexistent:rule+\n\n"
            "%section% := output\nmain :=\n\t+x+\n"
        )
        g = io.StringIO(text)
        g.name = "bad.dg"
        m = DharmaMachine()
        m._default_grammars = []
        with pytest.raises(Exception, match="Undefined"):
            m.process_grammars([g])


# ---------------------------------------------------------------------------
# CLI (subprocess tests)
# ---------------------------------------------------------------------------

class TestCLI:
    def test_dry_run(self):
        import subprocess
        result = subprocess.run(
            ["poetry", "run", "dharma", "-grammars", "dharma/grammars/test.dg", "-dry-run"],
            capture_output=True, text=True, cwd=str(GRAMMAR_DIR.parent.parent),
        )
        assert result.returncode == 0
        assert "Grammar OK" in result.stdout

    def test_seed_reproducibility(self):
        import subprocess
        runs = []
        for _ in range(2):
            result = subprocess.run(
                ["poetry", "run", "dharma", "-grammars", "dharma/grammars/test.dg",
                 "-seed", "12345", "-count", "1"],
                capture_output=True, text=True, cwd=str(GRAMMAR_DIR.parent.parent),
            )
            runs.append(result.stdout)
        assert runs[0] == runs[1], "Same seed should produce identical output"

    def test_stats_output(self):
        import subprocess
        result = subprocess.run(
            ["poetry", "run", "dharma", "-grammars", "dharma/grammars/test.dg",
             "-seed", "42", "-count", "10", "-stats"],
            capture_output=True, text=True, cwd=str(GRAMMAR_DIR.parent.parent),
        )
        assert result.returncode == 0
        assert "Coverage Statistics" in result.stderr

    def test_invalid_grammar_fails(self):
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dg", delete=False) as f:
            f.write("%const% OUTPUT_MIN := bad\n%section% := value\na :=\n\tx\n")
            f.flush()
            result = subprocess.run(
                ["poetry", "run", "dharma", "-grammars", f.name],
                capture_output=True, text=True, cwd=str(GRAMMAR_DIR.parent.parent),
            )
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# Content wrapping
# ---------------------------------------------------------------------------

class TestContentWrapping:
    def test_fallback_template_wraps(self):
        from dharma.core.websocket import DharmaWebSocketServer
        m = _make_machine(_simple_grammar())
        server = DharmaWebSocketServer(m, "127.0.0.1", 0)
        random.seed(42)
        content = m.generate_content()
        wrapped = server._wrap_content(content)
        assert "<script type=\"module\">" in wrapped
        assert "hello" in wrapped

    def test_template_bypasses_fallback(self):
        from dharma.core.websocket import DharmaWebSocketServer
        m = DharmaMachine(template="<html>$testcase_content</html>")
        g = io.StringIO(_simple_grammar())
        g.name = "t.dg"
        m._default_grammars = []
        m.process_grammars([g])
        server = DharmaWebSocketServer(m, "127.0.0.1", 0)
        random.seed(42)
        content = m.generate_content()
        wrapped = server._wrap_content(content)
        # With a template, _wrap_content returns content as-is (template already applied)
        assert wrapped == content
