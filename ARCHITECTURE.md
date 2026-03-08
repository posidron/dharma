# Architecture

This document describes the internal design of Dharma.

## Overview

```
┌─────────────────────────────────────────────┐
│                  CLI (dharma.py)             │
│  Parses args, seeds PRNG, orchestrates runs │
└──────────┬──────────────────┬───────────────┘
           │                  │
     ┌─────▼─────┐    ┌──────▼──────────────┐
     │  Generate  │    │  WebSocket Server   │
     │  to file / │    │  (websocket.py)     │
     │  stdout    │    │                     │
     └─────┬─────┘    │  HTTP: / /testcase   │
           │          │  WS:   /ws           │
           │          └──────┬──────────────┘
           │                 │
     ┌─────▼─────────────────▼───────────────┐
     │          DharmaMachine (core/dharma.py)│
     │  Grammar parser + random generator    │
     └──────────────────┬────────────────────┘
                        │
              ┌─────────▼─────────┐
              │   Extensions      │
              │  (extensions.py)  │
              │  %range% %repeat% │
              │  %choice% %block% │
              │  %optional% etc.  │
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
│   └── harness.html     # Browser fuzzing harness (served by the server)
└── grammars/
    ├── common.dg        # Shared standard library (numbers, strings, colors, unicode)
    ├── canvas2d.dg      # HTML Canvas 2D API
    ├── webgl.dg         # WebGL 1.0 / 2.0
    ├── webgpu.dg        # WebGPU
    ├── webnn.dg         # Web Neural Network API
    ├── svg.dg           # SVG DOM
    ├── indexeddb.dg     # IndexedDB
    ├── url.dg           # URL / URLSearchParams
    ├── test.dg          # Test grammar exercising core features
    └── var/templates/   # HTML templates for test case wrapping
```

## Grammar Engine (`core/dharma.py`)

### Parsing

`DharmaMachine.process_grammars()` reads `.dg` files and builds an in-memory graph:

1. **Tokenization** — Each line in an alternative is parsed into a list of tokens: `String`, `ValueXRef` (`+name+`), `VariableXRef` (`!name!`), `ElementXRef` (`@name@`), and extension calls.
2. **Namespacing** — Each grammar file gets a namespace from its filename (e.g., `common.dg` → `common`). Cross-file references use `namespace:rule` syntax.
3. **Cross-reference resolution** — After all files are parsed, xrefs are resolved into direct object references. Unresolved xrefs raise `XRefError`.
4. **Leaf-path analysis** — For each `DharmaValue`, the engine identifies which alternatives are _leaf_ (contain no recursive xrefs) to guarantee termination when recursion grows deep.

### Generation

`DharmaMachine.generate_content()` produces one test case:

1. **Variable instantiation** — `VARIABLE_MIN..VARIABLE_MAX` variable definitions are generated, each auto-numbered (e.g., `var1`, `var2`).
2. **Output expansion** — `OUTPUT_MIN..OUTPUT_MAX` output rules are expanded by recursively resolving xrefs.
3. **Leaf-mode forcing** — When recursion depth exceeds `LEAF_TRIGGER` (default 256), the engine switches to leaf-only alternatives to guarantee termination.
4. **Template wrapping** — If a template is provided, `$testcase_content` is replaced with the generated code.

### Token Types

| Token | Syntax | Purpose |
|-------|--------|---------|
| `String` | literal text | Emitted as-is |
| `ValueXRef` | `+name+` | Expands a value rule |
| `VariableXRef` | `!name!` | References a declared variable |
| `ElementXRef` | `@name@` | Variable declaration site |
| `SelfRef` | bare `@` | Shorthand for self-declaring @ in variable bodies |

### Extensions

Extensions are inline function calls within grammar alternatives:

| Extension | Syntax | Example |
|-----------|--------|---------|
| `MetaRange` | `%range%(start-end)` | `%range%(0-255)`, `%range%(a-z)`, `%range%(0x00-0xFF)` |
| `MetaRepeat` | `%repeat%(tokens)` | `%repeat%(+digit+)`, `%repeat%(+item+, ", ")` |
| `MetaChoice` | `%choice%(a, b, c)` | Pick one at random |
| `MetaBlock` | `%block%(path)` | Embed file contents |
| `MetaURI` | `%uri%(key)` | Load from URI table |
| `MetaOptional` | `%optional%(expr, p)` | Include with probability `p` |

### Configuration Constants

Set via `%const%` directives in grammar files:

| Constant | Default | Description |
|----------|---------|-------------|
| `OUTPUT_MIN` | 1 | Minimum output lines per test case |
| `OUTPUT_MAX` | 8 | Maximum output lines per test case |
| `VARIABLE_MIN` | 1 | Minimum variable declarations |
| `VARIABLE_MAX` | 4 | Maximum variable declarations |
| `LEAF_TRIGGER` | 256 | Recursion depth before forcing leaf alternatives |
| `MAX_REPEAT_POWER` | 12 | Maximum repeat count (2^N) |
| `OUTPUT_TEMPLATE` | `%s` | Wrapper for each output line (e.g., `try { %s } catch(e) {}`) |

## WebSocket Server (`core/websocket.py`)

Zero-dependency async server built on `asyncio`. No external packages required.

### Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/` | GET | Browser harness UI |
| `/testcase` | GET | Single generated test case as HTML |
| `/health` | GET | Health check (`{"status":"ok"}`) |
| `/ws` | WS | WebSocket stream for continuous fuzzing |

### WebSocket Protocol

The client and server exchange JSON messages:

**Client → Server:**
- `{"status": "open"}` — Client ready, requesting first test case
- `{"status": "success"}` — Previous test case executed, requesting next

**Server → Client:**
- Raw HTML string — The generated test case content

### Browser Harness

The harness (`core/harness.html`) is served at `/` and provides:

- **Iframe mode** — Runs test cases in a sandboxed `<iframe>` for speed. Some APIs may be restricted in iframe context.
- **New Tab mode** — Opens each test case in a separate browser tab via `window.open()`, giving full API access. The tab is closed after a configurable TTL before the next test is requested. Slower but avoids iframe sandbox limitations.
- **Controls** — Pause/resume, adjustable delay between tests, tab TTL slider.
- **Stats** — Live test count, error count, generation rate.
- **Auto-reconnect** — Reconnects to the server on disconnect.

## CDP Mode (`core/cdp.py`)

Direct injection of test cases into Chrome via the DevTools Protocol — no browser harness, no UI, no iframe restrictions.

### Requirements

Chrome must be launched with remote debugging enabled:

```bash
chrome --remote-debugging-port=9222
# or on macOS:
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

### How It Works

1. Dharma connects to Chrome's debug HTTP endpoint (`/json/list`) to discover page targets
2. Opens a CDP WebSocket connection to the target's `webSocketDebuggerUrl`
3. Enables the `Page` domain and retrieves the main frame ID
4. Loops:
   - `Page.navigate({url: "about:blank"})` — reset page state
   - `Page.setDocumentContent({frameId, html})` — inject the generated test case
   - Wait for the configured TTL (default 2s) for scripts to execute
   - Repeat

### Architecture

```
┌────────────────┐         CDP (WebSocket)         ┌──────────────────┐
│  Dharma CLI    │◄──────────────────────────────►  │  Chrome/Chromium │
│  -cdp mode     │  Page.navigate, setDocContent    │  --remote-debug  │
└────────────────┘                                  └──────────────────┘
```

The CDP client (`cdp.py`) is zero-dependency — it implements the WebSocket client handshake and frame protocol from scratch using `asyncio`, matching the server's zero-dependency approach.

### Trade-offs

| Mode | Speed | API Access | Setup | Works on Locked Devices |
|------|-------|------------|-------|-----------------------|
| Iframe (WebSocket) | Fast | Limited (sandbox) | None | Yes |
| New Tab (WebSocket) | Medium | Full | Allow pop-ups | Yes |
| CDP | Fast | Full | Launch Chrome with `--remote-debugging-port` | No |

CDP gives the best combination of speed and API access, but requires control over how Chrome is launched. For targets where you can't enable remote debugging (smart TVs, embedded browsers, closed-source products), use the WebSocket harness modes instead.

## Reproducibility

- All randomization uses Python's `random` module, seeded at startup.
- The seed is logged on every run and can be replayed with `-seed N`.
- Generated test cases contain no `Math.random()` — all choices are made server-side.

## Build

The project uses a single `pyproject.toml` with setuptools. No `setup.py`, no `Makefile`, no `__version__.py` — the version is declared once in `pyproject.toml` and read at runtime via `importlib.metadata`.

```bash
pip install .           # Install
pip install -e ".[dev]" # Editable install with dev tools (pytest, ruff, pylint, mypy)
```

## Adding a New Grammar

1. Create `dharma/grammars/yourapi.dg`
2. Define `%section% := value` rules for the API's types and methods
3. Define `%section% := variable` for any stateful objects
4. Define `%section% := output` for the entry-point operations
5. Reference shared values with `+common:number+`, `+common:bool+`, etc.
6. Test: `dharma -grammars dharma/grammars/yourapi.dg dharma/grammars/common.dg`
