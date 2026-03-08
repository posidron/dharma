# Dharma

**Generation-based, context-free grammar fuzzer.**

Dharma generates randomized test cases from user-defined grammars. It's designed for browser and JavaScript engine fuzzing but works for any domain where you can express structure as a grammar.

## Quick Start

```bash
poetry install
poetry run dharma -grammars dharma/grammars/canvas2d.dg
```

Or with pip:

```bash
pip install .
dharma -grammars dharma/grammars/canvas2d.dg
```

## Installation

### Poetry (recommended)

```bash
poetry install              # production
poetry install --with dev   # with dev tools (ruff, pylint, mypy, pytest)
```

### pip

```bash
pip install .               # production
pip install -e ".[dev]"     # editable / development
```

### Docker

```bash
docker build -t dharma .
docker run --rm -it dharma -grammars dharma/grammars/canvas2d.dg
```

**Requires Python ≥ 3.13.**

## Usage

### Generate to stdout

```bash
dharma -grammars dharma/grammars/canvas2d.dg
```

### Multiple grammars (order matters)

The first grammar listed owns the generation constants (`OUTPUT_MIN`, `OUTPUT_MAX`, `OUTPUT_TEMPLATE`). Later grammars cannot override them. `common.dg` auto-loads unless you provide it explicitly.

```bash
# WebGPU API fuzzing — sample shaders, exercises the full API
dharma -grammars dharma/grammars/webgpu.dg

# Shader compiler fuzzing — random WGSL fed to createShaderModule()
dharma -grammars dharma/grammars/wgsl.dg

# Combined — random WGSL merges into WebGPU's shader rules
dharma -grammars dharma/grammars/webgpu.dg dharma/grammars/wgsl.dg
```

### Save to files

```bash
dharma -grammars dharma/grammars/canvas2d.dg -storage ./output -count 100
```

### Reproducible runs

```bash
dharma -grammars dharma/grammars/canvas2d.dg -seed 42
```

### WebSocket server

Starts an HTTP + WebSocket server with a built-in browser harness:

```bash
dharma -grammars dharma/grammars/canvas2d.dg -server
```

Open the printed URL in your browser. The harness supports two execution modes:

- **Iframe mode** — fast, runs test cases in a sandboxed iframe (default)
- **New Tab mode** — opens each test case in a separate browser tab, giving full API access without iframe restrictions

Test errors from the browser are reported back to the server and logged to `dharma-errors.log`. The test case that caused each error is saved to `dharma-crashes/N.html` for reproduction.

Individual test cases are also available at `/testcase` for external tooling.

### CDP mode (Chrome DevTools Protocol)

Feed test cases directly into Chrome — no harness, no iframe restrictions:

```bash
# Launch Chrome with remote debugging
chrome --remote-debugging-port=9222

# In another terminal
dharma -grammars dharma/grammars/canvas2d.dg -cdp
```

Gives full API access at high speed, but requires control over Chrome's launch flags. Automatically detects renderer crashes via `Inspector.targetCrashed`, saves the crash-causing test case to `dharma-crashes/`, and reconnects. See [ARCHITECTURE.md](ARCHITECTURE.md) for trade-offs between the three modes.

### With a template

```bash
dharma -grammars dharma/grammars/canvas2d.dg -server \
  -template dharma/grammars/var/templates/html5/default.html
```

Without `-template`, the server wraps output in a minimal `<script type="module">` shell that supports top-level `await`.

### Benchmark

```bash
time dharma -grammars dharma/grammars/canvas2d.dg -count 10000 > /dev/null
```

## CLI Reference

```
dharma -grammars FILE [FILE ...]   Grammar files (.dg)

Generation:
  -count N                         Number of test cases (default: 1)
  -format EXT                      Output file extension (default: html)
  -seed N                          Random seed for reproducibility
  -recursion-limit N               Python recursion limit (default: 20000)

Server:
  -server                          Run the WebSocket server
  -server-host HOST                Bind address (default: 127.0.0.1)
  -server-port PORT                Bind port (default: 9090)
  -server-mode MODE                iframe or tab (default: iframe)

CDP:
  -cdp                             Feed test cases via Chrome DevTools Protocol
  -cdp-host HOST                   Chrome debug host (default: 127.0.0.1)
  -cdp-port PORT                   Chrome debug port (default: 9222)
  -cdp-ttl SEC                     Seconds per test case (default: 2.0)

Files:
  -storage DIR                     Output directory for test cases
  -prefix FILE                     Content prepended to every test case
  -suffix FILE                     Content appended to every test case
  -template FILE                   Template with $testcase_content placeholder

Debug:
  -logging LVL                     10=DEBUG, 20=INFO, 30=WARNING (default: 20)
  -dry-run                         Validate grammars without generating output
  -stats                           Print rule coverage statistics after generation
  -version                         Show version and exit
```

## Grammar Syntax

Dharma grammars use a simple declarative format with three sections: **value** (named productions), **variable** (stateful constructors), and **output** (entry points).

See [`dharma/grammars/test.dg`](dharma/grammars/test.dg) for a fully annotated example that demonstrates every feature.

Quick reference:

| Syntax | Purpose |
|--------|---------|
| `%%% comment` | Comment line |
| `%const% NAME := value` | Set a generation constant (first grammar wins) |
| `%require% name` | Declare a grammar dependency |
| `%section% := value\|variable\|output` | Switch section |
| `+rule+` | Reference a value rule |
| `+ns:rule+` | Cross-grammar reference |
| `!variable!` | Reference/create a variable |
| `@` | Auto-numbered variable name (in variable section) |
| `%weight%(N) alt` | Weighted alternative |
| `%range%(a-b)` | Random int, float, hex, or char in range |
| `%choice%(a, b, c)` | Uniform random pick |
| `%repeat%(expr, sep)` | Repeat with separator |
| `%repeat%(expr, sep, nodups)` | Repeat with dedup |
| `%optional%(expr, 0.5)` | Include with probability |
| `%unique%(!a!, !b!)` | Guarantee distinct values |
| `%block%(path)` | Embed file contents |
| `%uri%(path)` | Random file URI |

### Grammar loading order

- **First-writer-wins for constants**: the first grammar to set `OUTPUT_MIN`, `OUTPUT_TEMPLATE`, etc. owns those values. Later grammars that try to set the same constant to a different value are silently ignored.
- **Rule merging**: when multiple grammars define the same rule name, their alternatives are merged. This is how `wgsl.dg` injects random shaders into `webgpu.dg`'s `fullShaderCode` rule.
- **`%require%`**: grammars can declare dependencies. If the required grammar isn't loaded, you get a clear error at startup.
- **`common.dg` auto-loads** unless you supply it explicitly.
- **Variables are topologically sorted** by their `!variable!` dependencies, so `adapter` is always declared before `device`, `device` before `buffer`, etc.

## Included Grammars

| Grammar | Target |
|---------|--------|
| `canvas2d.dg` | HTML Canvas 2D API |
| `webgl.dg` | WebGL 1.0 / 2.0 |
| `webgpu.dg` | WebGPU API |
| `wgsl.dg` | WGSL shader compiler |
| `webaudio.dg` | Web Audio API |
| `webrtc.dg` | WebRTC API |
| `webnn.dg` | Web Neural Network API |
| `webcodecs.dg` | WebCodecs (Video/Audio Decoder/Encoder, ImageDecoder) |
| `svg.dg` | SVG DOM |
| `indexeddb.dg` | IndexedDB |
| `common.dg` | Shared values — numbers, strings, colors, unicode, booleans |
| `test.dg` | Feature showcase — demonstrates every grammar feature |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for details on internals, the grammar engine, and the server design.

## Development

```bash
poetry install --with dev
poetry run pytest tests/ -v       # 57 tests
poetry run ruff format --check dharma/
poetry run pylint dharma/
```

CI runs automatically on push/PR via [GitHub Actions](.github/workflows/ci.yml).

## Reproducibility

- Every run logs its random seed. Pass `-seed N` to reproduce.
- Each generated test case includes a `/* dharma seed: ... */` comment for tracing back to the exact RNG state.
- No `Math.random()` is used in generated test cases — all randomization happens server-side via Python's seeded PRNG.

## License

[MPL 2.0](https://www.mozilla.org/en-US/MPL/2.0/)

## Links

- [Dharma: A Grammar-Based Fuzzer](https://blog.mozilla.org/security/2015/06/29/dharma/) — Mozilla Security Blog
- [Fuzzing Wasm/JavaScript with Dharma](https://webassembly-security.com/fuzzing-wasm-javascript-dharma-chrome-v8/)
- [Implementing Fuzz Logics with Dharma](https://www.zerodayinitiative.com/blog/2019/1/31/implementing-fuzz-logics-with-dharma) — Zero Day Initiative
- [Pwn2Own 2018 Vulnerability Discovery](http://blog.ret2.io/2018/06/13/pwn2own-2018-vulnerability-discovery/) — Ret2 Systems
