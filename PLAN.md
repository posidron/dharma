# PLAN: Next-Generation Dharma

This document outlines the roadmap from grammar-based generation to coverage-guided, type-aware, mutation-based fuzzing. Each phase builds on the previous, so dharma remains usable at every stage.

---

## Current State (v2.0)

Dharma is a **pure generation** fuzzer: pick random grammar alternatives, expand recursively, emit strings. No feedback, no types, no mutations. Strengths: fast, easy to author grammars, 11 API grammars, 3 delivery modes, zero dependencies.

**Key limitation**: ~80% of generated test cases fail at the first API call (wrong argument types, wrong object state, wrong call order). The `try/catch` wrapper hides these failures. Real API surface beyond error handling is rarely exercised.

---

## Phase 1: Smarter Generation (within current architecture)

**Goal**: Increase the percentage of test cases that reach interesting code paths, without changing the engine fundamentally.

### 1.1 Cross-API Grammars

Create grammars that combine APIs in realistic ways:

```
%%% WebGPU + WebCodecs: decode video → use as external texture
%%% Web Audio + WebRTC: audio processing → peer connection
%%% Canvas2D + WebCodecs: draw on canvas → encode frame → decode
```

**How**: New `.dg` files that reference rules from multiple API grammars via `+namespace:rule+`. Example: `webgpu-codecs.dg` loads with `webgpu.dg` + `webcodecs.dg` and defines output rules that chain decode → texture → render.

**Challenge**: Grammar merging. Currently, loading multiple grammars merges same-named rules. For cross-API grammars, we need rules with unique names that *compose* operations from different APIs, not merge alternatives.

**Solution**: Use long, unique rule names in cross-API grammars (e.g., `gpuCodecFlow` not `main`). The output section references these explicitly. No merging conflicts.

### 1.2 Better common.dg Boundary Values

Add JS-specific adversarial values:

```
%%% --- JS Type Confusion ---
jsAdversarial :=
    NaN
    Infinity
    -Infinity
    -0
    null
    undefined
    Symbol()
    Object.create(null)
    new Proxy({}, {})
    {[Symbol.toPrimitive]() { return 42; }}
    {valueOf() { throw new Error(); }}
    {toString() { return ""; }}

%%% --- Prototype Pollution ---
protoPayload :=
    "__proto__"
    "constructor"
    "prototype"
    "__defineGetter__"
    "__defineSetter__"
```

### 1.3 Grammar Composition Fix

The current merging behavior is confusing when loading multiple API grammars. The fix:

**Rule**: Output sections should NOT merge across different API grammars. Each API grammar's output rules are namespaced. When you load `webgpu.dg` + `webrtc.dg`, you get both APIs' operations interleaved (because the engine picks from ALL output rules), but their internal value/variable rules stay independent.

**Current behavior**: This already works correctly — `webgpu:main` and `webrtc:main` would be separate output rules, and the engine picks from all output rules randomly. The "finicky" feeling comes from grammars that accidentally share rule names (e.g., both define `main` in the output section with the same namespace because they're loaded from files with the same stem).

**Recommendation**: Rename output rules from `main` to API-specific names (`webgpuStmt`, `webrtcStmt`) to avoid accidental merging. Most grammars already do this internally — the `main` rule just wraps them.

---

## Phase 2: Coverage-Guided Feedback

**Goal**: Prioritize grammar alternatives that discover new code coverage in the browser.

### Architecture

```
┌─────────────┐     test case     ┌──────────────────┐
│   Dharma    │─────────────────► │  Instrumented    │
│   Engine    │                   │  Browser         │
│             │◄─────────────────│  (coverage data) │
│  (weighted  │   coverage map   │                   │
│   grammar)  │                  └──────────────────┘
└─────────────┘
```

### Implementation Plan

#### 2.1 Coverage Collection

**Option A: Source-based (compile-time instrumentation)**

Build Chromium with `-fsanitize-coverage=edge` or use SanitizerCoverage's PC-table mode. Coverage data is dumped as a bitmap (shared memory or file).

- Pros: Precise edge coverage, fast
- Cons: Requires building Chromium from source

**Option B: Binary-based (dynamic instrumentation)**

Use DynamoRIO, Frida, or Intel PT to collect edge coverage from a release browser binary.

- Pros: Works with release builds
- Cons: Slower (10-50x), less precise

**Option C: JS-level coverage**

Use V8's built-in coverage (`--js-flags=--lcov`) or CDP's `Profiler.startPreciseCoverage()`.

- Pros: Zero build requirements, works via CDP
- Cons: Only covers JS execution, not native code (where the bugs are)

**Recommendation**: Start with **Option C** (CDP coverage) for quick wins, then move to **Option A** for production fuzzing.

#### 2.2 CDP Coverage Integration

```python
# In cdp.py, after injecting a test case:
await conn.send("Profiler.enable")
await conn.send("Profiler.startPreciseCoverage", {
    "callCount": False,
    "detailed": True,
})

# After test case executes:
coverage = await conn.send("Profiler.takePreciseCoverage")
await conn.send("Profiler.stopPreciseCoverage")

# Extract covered ranges
new_ranges = extract_ranges(coverage["result"]["result"])
if has_new_coverage(new_ranges, global_coverage):
    save_to_corpus(test_case, new_ranges)
    boost_grammar_weights(test_case_metadata)
```

#### 2.3 Grammar Weight Feedback

Each grammar alternative has a weight. Currently static (from `%weight%` directives). Add dynamic weight boosting:

```python
class DharmaValue:
    def __init__(self, ...):
        self.static_weights = [...]   # From grammar
        self.dynamic_boost = [1.0] * len(alternatives)  # Multiplier

    def _choose(self):
        effective = [s * d for s, d in zip(self.static_weights, self.dynamic_boost)]
        return random.choices(self.alternatives, weights=effective, k=1)[0]

    def boost(self, alt_index: int, factor: float = 1.5):
        self.dynamic_boost[alt_index] *= factor
```

When a test case discovers new coverage:
1. Record which output rule and top-level value rules were used
2. Boost their dynamic weights by 1.5x
3. Over time, alternatives that discover coverage get picked more often
4. Apply decay (multiply all boosts by 0.99 per round) to prevent stagnation

#### 2.4 Data Flow

```
                 ┌──────────────────────────────────┐
                 │          Feedback Loop            │
                 │                                   │
  generate() ──► test case ──► browser ──► coverage  │
       ▲                                      │      │
       │         new edges? ◄─────────────────┘      │
       │              │                              │
       │         yes: boost weights, save corpus     │
       │         no:  decay weights slightly         │
       └─────────────────────────────────────────────┘
```

### Effort Estimate

- CDP coverage collection: ~200 lines in cdp.py
- Weight feedback in engine: ~100 lines in dharma.py
- Corpus storage: ~100 lines (save/load JSON + HTML)
- **Total: ~400 lines, 2-3 days**

### Open Issues

**Error classification**: Not all errors are equal. The feedback loop must distinguish:
- Validation errors (expected, low value) — don't boost weights
- Renderer crashes / DEVICE_LOST (high value) — boost + save to corpus immediately
- Timeouts/hangs (high value) — boost + save
- JS exceptions from generated code (noise) — ignore

Implementation: parse error messages for known patterns, classify into `ignore | low | high | crash` categories. Only `high` and `crash` trigger weight boosting.

**Browser state management**: After N test cases, accumulated state (leaked contexts, DOM nodes, GPU resources) causes OOM or slowdown. Strategy:
- CDP mode: `Page.navigate("about:blank")` already resets per test case, but native resources may leak. Add a hard restart (close + reconnect) every ~1000 test cases.
- Iframe mode: periodically recreate the iframe element (`frame.remove(); frame = document.createElement("iframe")`) every ~500 test cases.

**Throughput**: `Profiler.takePreciseCoverage()` adds ~5-20ms overhead per test case. Target: 20-50 test cases/sec in coverage mode (vs ~unlimited in pure generation mode). Acceptable for coverage-guided fuzzing where each test case is more valuable.

**Option A migration path**: Moving from CDP JS-level coverage (Option C) to SanitizerCoverage (Option A) requires:
1. Dharma launches the instrumented Chromium binary (new `--browser-path` flag)
2. Coverage bitmap is written to a shared memory region or `/tmp/cov.NNNN`
3. After each test case, dharma reads the bitmap, diffs against global coverage
4. Requires building Chromium with `use_clang_coverage=true` in GN args
5. Estimated additional effort: ~500 lines, separate phase (Phase 2b)

---

## Phase 3: Type-Directed Generation

**Goal**: Every generated expression is type-correct. Eliminate the ~80% failure rate.

### Architecture

Instead of grammar alternatives producing arbitrary strings, each alternative is annotated with its **return type**. The engine selects alternatives that match the expected type at each position.

### Grammar Syntax Extension

```
%section% := typed_value

%%% Type annotations on rules
buffer<GPUBuffer> :=
    !device!.createBuffer(+bufferDescriptor+)

texture<GPUTexture> :=
    !device!.createTexture(+textureDescriptor+)

%%% Type-constrained references
encoderOp :=
    !commandEncoder!.copyBufferToBuffer(!buffer<GPUBuffer>!, 0, !buffer<GPUBuffer>!, 0, 256)
```

### Engine Changes

```python
class TypedValue(DharmaValue):
    """A value rule with a return type annotation."""
    def __init__(self, ident, machine, return_type=None):
        super().__init__(ident, machine)
        self.return_type = return_type

class TypedVariableXRef(VariableXRef):
    """A variable reference with type filtering."""
    def __init__(self, value, parent, type_filter=None):
        super().__init__(value, parent)
        self.type_filter = type_filter

    def generate(self, state):
        # Only pick variables that match the type filter
        ref = self.parent.variable_xref[self.value]
        if self.type_filter:
            # Filter by type tag
            candidates = [v for v in ref.instances if v.type == self.type_filter]
            return random.choice(candidates) if candidates else ref.generate(state)
        return ref.generate(state)
```

### Key Design Decision

**Do NOT build a full type system.** Instead:

1. Types are just **string tags** — no inheritance, no subtyping, no generics
2. Variables get tagged when created: `buffer<GPUBuffer>` means "this variable is a GPUBuffer"
3. References can filter by tag: `!buffer<GPUBuffer>!` means "give me a variable tagged GPUBuffer"
4. Untagged references work as before (backwards compatible)
5. The engine doesn't validate types — it just filters. Wrong types still produce try/catch-wrapped code.

This gives us 80% of the benefit (correct argument types) with 10% of the complexity (no type checker, no inference, no error on mismatch).

### Known Limitation: Cross-Variable Identity

Type tags solve "give me a GPUBuffer" but NOT "give me a GPUBuffer that belongs to device2." The real constraint in WebGPU is identity — a buffer created on device1 can't be used with device2's command encoder. Type tags filter by type but not by lineage.

Solving this properly requires tracking which device created each resource — effectively a per-test-case object graph. This is Phase 3b territory:

```python
# Possible future extension: identity-aware variables
class VariableInstance:
    name: str           # "buffer1"
    type_tag: str       # "GPUBuffer"
    created_by: str     # "device2" — tracks lineage
```

For now, type tags + `try/catch` are sufficient. Cross-device mismatches hit the validation layer, which is still interesting surface to fuzz.

### Effort Estimate

- Parse `<Type>` annotations: ~50 lines
- Type-filtered variable selection: ~80 lines
- Update existing grammars: ~2 hours per grammar
- **Total: ~200 lines engine + grammar updates, 3-5 days**

---

## Phase 4: Mutation-Based Fuzzing

**Goal**: Take valid test cases (from corpus or generation), apply small semantic-preserving mutations, and test the mutated variants.

### Mutation Operators

```
┌───────────────────────────────────────────────────────┐
│  Original:   device.createBuffer({size: 256,          │
│              usage: GPUBufferUsage.STORAGE})           │
│                                                       │
│  Mutations:                                           │
│  1. Swap arguments:  {usage: ..., size: ...}          │
│  2. Change literal:  {size: 0, usage: ...}            │
│  3. Change enum:     {size: 256, usage: MAP_READ}     │
│  4. Drop argument:   {size: 256}                      │
│  5. Duplicate call:  createBuffer(..); createBuffer(..)│
│  6. Insert destroy:  buf.destroy(); buf.mapAsync(...)  │
│  7. Reorder:         swap two statements               │
│  8. Cross-ref:       use buffer from device1 on device2│
└───────────────────────────────────────────────────────┘
```

### Implementation: AST-Free Mutations

We don't parse JS into an AST. Instead, mutations operate on the **grammar derivation tree** — the sequence of rule choices that produced the test case.

```python
class Derivation:
    """Records which alternatives were chosen during generation."""
    choices: list[tuple[str, int]]  # (rule_ident, alternative_index)

class Mutator:
    def mutate(self, derivation: Derivation) -> Derivation:
        """Apply a random mutation to a derivation."""
        new = derivation.copy()
        op = random.choice([
            self._swap_choice,      # Pick different alternative for one rule
            self._swap_order,       # Reorder two output statements
            self._duplicate_stmt,   # Repeat an output statement
            self._drop_stmt,        # Remove an output statement
            self._change_literal,   # Re-expand a leaf rule
        ])
        return op(new)
```

**How it works:**

1. Generate a test case normally, but record the derivation (which alternative was chosen at each decision point)
2. Save interesting derivations to the corpus
3. To mutate: load a derivation, change one or more choices, replay the derivation to produce a new test case
4. The mutated test case is structurally similar but exercises a different code path

### Engine Changes

```python
@dataclass
class GenState:
    leaf_mode: bool = False
    leaf_trigger: int = 0
    recording: bool = False         # NEW: record choices
    choices: list[tuple] = None     # NEW: derivation log
    replaying: bool = False         # NEW: replay mode
    replay_choices: list[tuple] = None  # NEW: choices to replay
```

When `recording=True`, every `_choose()` call logs `(rule_ident, chosen_index)` to `choices`.
When `replaying=True`, `_choose()` returns the pre-selected alternative from `replay_choices` instead of random.

### Effort Estimate

- Derivation recording in `_choose`: ~30 lines
- Derivation replay: ~30 lines
- Mutation operators: ~150 lines
- Corpus storage (JSON): ~80 lines
- **Total: ~300 lines, 2-3 days**

### Known Limitation: Derivation Fragility

Derivations record `(rule_ident, alternative_index)` tuples. If the grammar is modified (alternatives added/removed/reordered), saved derivations become invalid — the indices point to wrong alternatives.

Mitigation:
1. Hash the grammar files at recording time, store hash in derivation metadata
2. On replay, compare hashes — reject stale derivations with a warning
3. Stale derivations are still useful for mutation (the mutator re-expands changed choices randomly)

---

## Phase 5: Corpus Management

**Goal**: Persist, minimize, and deduplicate interesting test cases across fuzzing sessions.

### Corpus Structure

```
dharma-corpus/
├── index.json           # Metadata for all entries
├── 0001.html            # Test case HTML
├── 0001.derivation.json # Grammar derivation (for replay/mutation)
├── 0001.coverage.bin    # Coverage bitmap (for dedup)
├── 0002.html
├── 0002.derivation.json
├── 0002.coverage.bin
└── ...
```

### Index Schema

```json
{
  "entries": [
    {
      "id": 1,
      "seed": "2147483648:3564348608:1266698288",
      "grammars": ["webgpu.dg", "common.dg"],
      "timestamp": "2026-03-08T12:00:00Z",
      "coverage_edges": 1423,
      "new_edges": 12,
      "error": null,
      "minimized": false,
      "parent": null
    }
  ]
}
```

### Operations

```python
class Corpus:
    def add(self, test_case: str, derivation: Derivation, coverage: Coverage) -> bool:
        """Add if it has new coverage. Returns True if added."""

    def pick_for_mutation(self) -> tuple[str, Derivation]:
        """Select a corpus entry for mutation (favor high new-edge entries)."""

    def minimize(self, entry_id: int) -> str:
        """Reduce test case to minimum that preserves coverage."""

    def deduplicate(self):
        """Remove entries whose coverage is a subset of another entry."""

    def merge(self, other: Corpus):
        """Import entries from another corpus, keeping only new coverage."""
```

### Minimization Strategy

1. Try removing each output statement one at a time
2. Re-run, check if coverage is preserved
3. If yes, keep the removal
4. Repeat until no more statements can be removed
5. Result: minimal test case that still hits the same code

### Effort Estimate

- Corpus class: ~200 lines
- Index management: ~100 lines
- Minimization: ~100 lines
- CLI integration (`--corpus-dir`): ~50 lines
- **Total: ~450 lines, 3-4 days**

---

## Implementation Order

```
Phase 1 ──► Phase 2 ──► Phase 4 ──► Phase 5
   │            │            │
   │            ▼            │
   │       Phase 3           │
   │       (parallel)        │
   │                         │
   ▼                         ▼
  Today                   ~3 weeks
```

| Phase | Effort | Impact | Dependencies |
|-------|--------|--------|-------------|
| 1: Smarter Generation | 1 day | Medium | None |
| 2: Coverage Feedback | 3 days | **High** | CDP mode working |
| 3: Type-Directed | 4 days | **High** | None (parallel with 2) |
| 4: Mutation-Based | 3 days | **Very High** | Phase 2 (needs corpus) |
| 5: Corpus Management | 3 days | **High** | Phase 2 |

**Total: ~2 weeks of focused work.**

---

## Design Principles

1. **Current-grammar compatible** — The 11 grammars we have today (v2.0) should work unchanged through Phase 2. Phases 3+ may require grammar updates to benefit from new features, but untagged grammars still function. We do NOT maintain backwards compatibility with the original dharma v1 codebase — that's a different project.
2. **Opt-in complexity** — Type annotations, coverage feedback, and mutations are all optional features
3. **No new dependencies** — Coverage uses CDP (already have the client), corpus uses JSON files
4. **Grammar-first** — The `.dg` format remains the primary authoring surface. Types and mutations are grammar-level features, not Python code
5. **Incremental value** — Each phase delivers standalone value. You don't need mutation to benefit from coverage feedback, and you don't need types to benefit from mutations
6. **Breaking changes are fine** — If requiring type tags on variables in Phase 3 produces a cleaner design, we do it and update all 11 grammars. We don't carry compatibility shims for old syntax.
