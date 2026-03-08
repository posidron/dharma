---
name: create-grammar-from-w3c
description: '**WORKFLOW SKILL** — Create a new Dharma fuzzer grammar (.dg file) from a W3C Web API specification. USE FOR: adding fuzzer coverage for a new browser API; generating a grammar from a W3C spec URL; creating value/variable/output sections from Web IDL interfaces. DO NOT USE FOR: modifying existing grammars (use update-grammar-from-blink instead); fixing grammar syntax errors; engine/Python code changes. INVOKES: fetch_webpage to read W3C specs, file system tools to create .dg files, terminal to validate with --dry-run.'
---

# Create Grammar from W3C Spec

Create a comprehensive Dharma fuzzer grammar (.dg) file for a Web API based on its W3C specification.

## Workflow

### 1. Gather the Spec

Fetch the W3C specification URL provided by the user. Extract:
- All **interfaces** and their **methods**, **attributes**, and **constructors**
- All **enum types** and their values
- All **dictionary types** and their members (with defaults)
- All **callback** signatures
- **Constructor** patterns and required arguments
- **Async methods** that return Promises (need `await`)
- **Event handlers** and event types

### 2. Grammar Structure

Every grammar MUST follow this exact template:

```
%%% <API Name> Grammar
%%% <Spec Status>, <Date>
%%% <Spec URL>
%%%
%%% Covers: <list of interfaces>

%require% common

%const% OUTPUT_MIN := 5
%const% OUTPUT_MAX := 20
%const% OUTPUT_TEMPLATE := "try { %s } catch(e) {}"

%%% =========================================================================
%%% Value Section — all building blocks and operations
%%% =========================================================================

%section% := value

%%% --- Enum values from spec ---
%%% (one rule per enum type, values as alternatives)

%%% --- Option/Init dictionaries from spec ---
%%% (one rule per dictionary, members as object literals)

%%% --- Operations organized by interface ---
%%% (one rule per interface, methods as alternatives)

%%% =========================================================================
%%% Variable Section — stateful object constructors
%%% =========================================================================

%section% := variable

%%% Variables use bare @ for auto-numbering:
%%%   ctx :=
%%%       @ = new AudioContext();
%%%
%%% Variables that depend on other variables use !var!:
%%%   node :=
%%%       @ = !ctx!.createOscillator();
%%%
%%% The engine topologically sorts variables by dependencies.

%%% =========================================================================
%%% Output Section — entry-point rules
%%% =========================================================================

%section% := output

main :=
    +operations+;
```

### 3. Rules for Grammar Quality

**Value rules:**
- Every enum from the spec becomes a value rule with all enum values as alternatives
- Include spec-valid values AND boundary/edge-case values (0, -1, MAX_INT, empty string, `+common:number+`)
- Dictionary options should have multiple alternatives: full options, partial options, empty `{}`
- Reference `+common:bool+`, `+common:number+`, `+common:text+` for generic fuzz values

**Variable rules:**
- One variable per major interface/object type
- Use `@` (bare) for auto-numbered names
- Use `!other_var!` to reference dependencies
- Constructor alternatives should cover: factory methods AND `new Constructor()` forms
- Async constructors need `await`
- NO `getUserMedia()` or other permission-requiring APIs — use fake sources:
  - Audio: `AudioContext.createOscillator()` → `createMediaStreamDestination().stream`
  - Video: `canvas.captureStream(30)`

**Output rules:**
- One `main` rule with all operation categories
- Each operation alternative should be a single JS statement ending with `;`
- Promise-returning methods must use `await`
- Property reads are valid (e.g., `!obj!.state`)
- Use `%unique%(!var!, !var!)` where source ≠ destination matters

**Constants:**
- `OUTPUT_MIN := 3-5` (enough operations per test case)
- `OUTPUT_MAX := 15-25` (not so many that test cases are huge)
- `OUTPUT_TEMPLATE := "try { %s } catch(e) {}"` (always wrap in try/catch)

### 4. Validation

After creating the grammar:

1. Run `dharma -grammars dharma/grammars/<name>.dg -dry-run` to validate
2. Run `dharma -grammars dharma/grammars/<name>.dg -seed 42` to see sample output
3. Run `dharma -grammars dharma/grammars/<name>.dg -seed 42 -count 100 -stats` to check coverage
4. Verify JS syntax: pipe output through Node.js module validation
5. Update README.md grammars table
6. Update ARCHITECTURE.md directory listing

### 5. Common Patterns

**Typed arrays for buffer APIs:**
```
bufferData :=
    new Float32Array(%range%(1-64))
    new Uint8Array([%repeat%(%range%(0-255), ", ")])
    new ArrayBuffer(%range%(0-1024))
```

**Canvas/media source (no permissions):**
```
source :=
    var _c = document.createElement("canvas"); _c.width = 640; _c.height = 480; @ = _c;
    var _actx = new AudioContext(); var _osc = _actx.createOscillator(); var _dest = _actx.createMediaStreamDestination(); _osc.connect(_dest); _osc.start(); @ = _dest.stream;
```

**Async method with await:**
```
asyncOp :=
    await !obj!.someAsyncMethod()
    await !obj!.anotherAsync(+options+)
```

**Event handlers:**
```
eventSetup :=
    !obj!.onfoo = function(e) { }
    !obj!.addEventListener("foo", function(e) { })
```
