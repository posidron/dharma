# Architecture

This document describes the internal design of Dharma.

## Overview

```
┌─────────────────────────────────────────────┐
│                  CLI (dharma.py)             │
│  Parses args, seeds PRNG, orchestrates runs │
│  --dry-run, --stats, --seed                 │
└──────────┬──────────┬───────────┬───────────┘
           │          │           │
     ┌─────▼─────┐ ┌──▼──────┐ ┌─▼────────────┐
     │  Generate  │ │  WS     │ │  CDP          │
     │  to file / │ │  Server │ │  (cdp.py)     │
     │  stdout    │ │         │ │  Direct Chrome │
     └─────┬─────┘ └──┬──────┘ └─┬────────────┘
           │          │           │
     ┌─────▼──────────▼───────────▼──────────────┐
     │          DharmaMachine (core/dharma.py)    │
     │  Grammar parser + random generator        │
     │  Topological sort, coverage stats,         │
     │  first-writer-wins constants               │
     └──────────────────┬────────────────────────┘
                        │
              ┌─────────▼─────────┐
              │   Extensions      │
              │  (extensions.py)  │
              │  %range% %repeat% │
              │  %choice% %block% │
              │  %optional%       │
              │  %unique%         │
              └───────────────────┘
```

## Directory Structure

```
dharma/
├── __init__.py          # Package root, exports DharmaCommandLine + __version__
├── __main__.py          # `python -m dharma` entry point
├── dharma.py            # CLI: argument parsing, seed management, run modes
├── core/
│   ├── dharma.py        # DharmaMachine: grammar parser + test case generator
│   ├── extensions.py    # Extension methods (%range%, %repeat%, %choice%, etc.)
│   ├── errors.py        # GrammarError, ParseError, XRefError
│   ├── websocket.py     # Async HTTP + WebSocket server (zero dependencies)
│   ├── cdp.py           # Chrome DevTools Protocol client (zero dependencies)
│   └── harness.html     # Browser fuzzing harness (served by the server)
└── grammars/
    ├── common.dg        # Shared standard library (numbers, strings, colors, unicode)
    ├── canvas2d.dg      # HTML Canvas 2D + OffscreenCanvas
    ├── webgl.dg         # WebGL 1.0 / 2.0
    ├── webgpu.dg        # WebGPU API (sample shaders for valid pipelines)
    ├── wgsl.dg          # WGSL shader compiler (random shaders)
    ├── webaudio.dg      # Web Audio API 1.1
    ├── webrtc.dg        # WebRTC API
    ├── webnn.dg         # Web Neural Network API
    ├── webcodecs.dg     # WebCodecs (Video/Audio Encoder/Decoder, ImageDecoder)
    ├── svg.dg           # SVG DOM
    ├── indexeddb.dg     # IndexedDB
    ├── test.dg          # Feature showcase — demonstrates every grammar feature
    └── var/templates/   # HTML templates for test case wrapping
```

## Grammar Engine (`core/dharma.py`)

### Parsing

`DharmaMachine.process_grammars()` reads `.dg` files and builds an in-memory graph:

1. **Tokenization** — Each alternative is parsed into tokens: `String`, `ValueXRef` (`+name+`), `VariableXRef` (`!name!`), `ElementXRef` (`@name@`), and extension calls.
2. **Namespacing** — Each grammar gets a namespace from its filename. Cross-file references use `namespace:rule` syntax.
3. **Rule merging** — When multiple grammars define the same rule name, their alternatives are merged. This is how `wgsl.dg` injects random shaders into `webgpu.dg`.
4. **`%require%` validation** — Declared dependencies are checked after all grammars load.
5. **Cross-reference resolution** — Xrefs are resolved into direct object references.
6. **Leaf-path analysis** — Identifies leaf alternatives to guarantee termination.

### Constants: First-Writer-Wins

When multiple grammars set the same `%const%`, the first grammar loaded owns it. Later grammars that try to change the value are silently ignored. **Grammar loading order matters** — list your primary grammar first.

### Variable Ordering

Variables are emitted in dependency order via topological sort (Kahn's algorithm). `adapter` → `device` → `buffer` → `texture`. Cycles are tolerated.

### Generation

`generate_content()` produces one test case:

1. **Seed capture** — RNG state embedded as `/* dharma seed: ... */` comment
2. **Variable instantiation** — In dependency order, `VARIABLE_MIN..MAX` per type
3. **Output expansion** — `OUTPUT_MIN..MAX` rules expanded recursively
4. **Leaf-mode forcing** — At depth > `LEAF_TRIGGER`, only leaf alternatives selected
5. **Template wrapping** — `OUTPUT_TEMPLATE` per line, `$testcase_content` for full document
6. **Coverage tracking** — Rule picks counted for `--stats`

### Token Types

| Token | Syntax | Purpose |
|-------|--------|---------|
| `String` | literal text | Emitted as-is |
| `ValueXRef` | `+name+` | Expands a value rule |
| `VariableXRef` | `!name!` | References a declared variable |
| `ElementXRef` | `@name@` | Variable declaration site |
| `SelfRef` | bare `@` | Shorthand in variable bodies |

### Extensions

| Extension | Syntax | Description |
|-----------|--------|-------------|
| `%range%` | `%range%(0-255)` | Random int, float, hex, or char |
| `%repeat%` | `%repeat%(expr, sep, nodups)` | Repeat with separator and dedup |
| `%choice%` | `%choice%(a, b, c)` | Uniform random pick |
| `%optional%` | `%optional%(expr, 0.5)` | Include with probability |
| `%unique%` | `%unique%(!a!, !b!)` | Guarantee distinct values |
| `%weight%` | `%weight%(N) alt` | Weighted alternative |
| `%block%` | `%block%(path)` | Embed file contents |
| `%uri%` | `%uri%(key)` | Load from URI table |

### Directives

| Directive | Purpose |
|-----------|---------|
| `%const% NAME := value` | Set constant (first-writer-wins) |
| `%require% name` | Declare grammar dependency |
| `%section% := value\|variable\|output` | Switch section |

## WebSocket Server (`core/websocket.py`)

Zero-dependency async server. Handles HTTP + WebSocket on one port.

### Endpoints

| Path | Description |
|------|-------------|
| `/` | Browser harness UI |
| `/testcase` | Single generated test case |
| `/health` | Health check |
| `/ws` | WebSocket stream |

### Protocol

- Client sends `{"status": "open"}` → server sends first test case
- Client sends `{"status": "success"}` → server sends next test case
- Client sends `{"status": "error", "message": "..."}` → logged to `dharma-errors.log`
- Large payloads (>64KB) sent as binary frames for efficiency

### Harness

- **Iframe mode** — Full sandbox permissions + feature policies for all hardware APIs
- **New Tab mode** — Concurrent tab pool (1–10), configurable TTL
- **Error reporting** — `window.onerror` → `postMessage` → WebSocket → server log file
- **Crash saving** — Each error's test case is saved to `dharma-crashes/N.html` for reproduction

## CDP Mode (`core/cdp.py`)

Direct Chrome injection. Zero dependencies.

1. Discovers targets via `/json/list` (full response, no truncation)
2. WebSocket connection with proper masking (RFC 6455 client)
3. Wraps content in `<script type="module">` when no template set
4. Cancels pending futures on close
5. Monitors `Inspector.targetCrashed` — saves crash test case and auto-reconnects

### Mode Comparison

| Mode | Speed | API Access | Setup |
|------|-------|------------|-------|
| Iframe | Fast | Full (permissive sandbox) | None |
| New Tab | Medium | Full | Allow pop-ups |
| CDP | Fast | Full | Chrome `--remote-debugging-port` |

## CLI

| Flag | Description |
|------|-------------|
| `-grammars FILE...` | Grammar files (required) |
| `-dry-run` | Validate grammars without generating |
| `-stats` | Print coverage statistics to stderr |
| `-seed N` | Reproducible generation |
| `-count N` | Number of test cases |
| `-server` | WebSocket server mode |
| `-server-mode iframe\|tab` | Harness execution mode |
| `-cdp` | Chrome DevTools Protocol mode |
| `-storage DIR` | Write test cases to directory |
| `-template FILE` | HTML template with `$testcase_content` |

## Build

```bash
poetry install              # Production
poetry install --with dev   # With ruff, pylint, mypy, pytest
```

Requires **Python ≥ 3.13**.

## Testing

```bash
poetry run pytest tests/ -v    # 57 tests, ~2s
```

Tests cover: engine constants, range/variable edge cases, all extensions, rule merging, cross-grammar refs, CLI flags (dry-run, stats, seed reproducibility), content wrapping, and parametrized load+generate for all grammars.

CI runs on push/PR via `.github/workflows/ci.yml`.

## Adding a New Grammar

1. Create `dharma/grammars/yourapi.dg`
2. Add `%require% common`
3. Set `%const% OUTPUT_MIN`, `OUTPUT_MAX`, `OUTPUT_TEMPLATE`
4. Define value, variable, and output sections
5. Validate: `dharma -grammars dharma/grammars/yourapi.dg -dry-run`
6. Test: `dharma -grammars dharma/grammars/yourapi.dg -seed 42`
7. Coverage: `dharma -grammars dharma/grammars/yourapi.dg -count 100 -stats`

See [`dharma/grammars/test.dg`](dharma/grammars/test.dg) for every feature demonstrated.
