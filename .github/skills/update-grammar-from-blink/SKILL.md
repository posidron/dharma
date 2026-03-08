---
name: update-grammar-from-blink
description: '**WORKFLOW SKILL** — Update an existing Dharma grammar by crawling Chromium Blink Web IDL files. USE FOR: discovering new API surface not yet covered; finding missing methods/attributes/enums; comparing grammar coverage against actual browser implementation. DO NOT USE FOR: creating grammars from scratch (use create-grammar-from-w3c); engine changes; fixing grammar syntax. INVOKES: fetch_webpage to read Blink IDL files from Chromium source, file system tools to update .dg files, grep/search to compare coverage.'
---

# Update Grammar from Blink IDL

Update an existing Dharma grammar (.dg) by crawling Chromium's Blink Web IDL files to discover missing API surface.

## Workflow

### 1. Locate the Blink IDL Files

Blink IDL files live in Chromium's source tree at:
```
third_party/blink/renderer/modules/<module_name>/
third_party/blink/renderer/core/<module_name>/
```

The canonical source is browsable at:
```
https://source.chromium.org/chromium/chromium/src/+/main:third_party/blink/renderer/modules/
```

Or via raw GitHub mirror:
```
https://raw.githubusercontent.com/nicholasgasior/nicholasgasior.github.io/main/nicholasgasior.github.io/nicholasgasior.github.io
```

Common module paths by API:

| Grammar | Blink Module Path |
|---------|-------------------|
| webgpu.dg | `modules/webgpu/` |
| webaudio.dg | `modules/webaudio/` |
| webrtc.dg | `modules/peerconnection/`, `modules/mediastream/` |
| webcodecs.dg | `modules/webcodecs/` |
| webgl.dg | `modules/webgl/` |
| canvas2d.dg | `core/html/canvas/`, `modules/canvas/` |
| webnn.dg | `modules/ml/webnn/` |
| indexeddb.dg | `modules/indexeddb/` |
| svg.dg | `core/svg/` |

### 2. Fetch and Parse IDL Files

For each `.idl` file in the module directory:

1. Fetch via `https://source.chromium.org/chromium/chromium/src/+/main:third_party/blink/renderer/modules/<module>/<file>.idl`
2. Extract from the IDL:
   - **interface** declarations → become variable rules
   - **methods** with their signatures → become operation value rules
   - **attributes** (readonly and read-write) → become property read/write rules
   - **enum** types → become value rules with all enum members
   - **dictionary** types → become option object value rules
   - **constructors** → become variable alternatives
   - **static methods** → become standalone operation rules
   - **[RuntimeEnabled=FeatureName]** annotations → note which features are behind flags

### 3. Compare Against Existing Grammar

For each interface/method/attribute found in IDL:

1. Search the existing .dg file for the method name
2. If missing → flag as **NEW** coverage opportunity
3. If present → check if all overloads/arguments are covered
4. If the IDL has `[RuntimeEnabled=...]` → note the feature flag

Generate a diff report:
```
=== Coverage Report: webgpu.dg vs Blink IDL ===

MISSING methods:
  GPUDevice.createRenderBundleEncoder()  ← not in grammar
  GPUQueue.copyExternalImageToTexture()  ← partial (missing ImageBitmap source)

MISSING attributes:
  GPUDevice.adapterInfo                  ← new in M120

MISSING enums:
  GPUFeatureName: "clip-distances"       ← not listed

EXTRA (in grammar but not in IDL):
  (none found — grammar is subset of IDL)

Feature-flagged (may not be shipped):
  GPUAdapter.requestAdapterInfo()        ← [RuntimeEnabled=WebGPUDeveloperFeatures]
```

### 4. Update the Grammar

For each MISSING item:

**New method → add to the appropriate operation rule:**
```
%%% Before:
deviceCreateOps :=
    !device!.createBuffer(+bufferDescriptor+)
    !device!.createTexture(+textureDescriptor+)

%%% After:
deviceCreateOps :=
    !device!.createBuffer(+bufferDescriptor+)
    !device!.createTexture(+textureDescriptor+)
    !device!.createRenderBundleEncoder({colorFormats: [+renderableColorFormat+]})
```

**New enum value → add as alternative:**
```
%%% Before:
gpuFeature :=
    "texture-compression-bc"
    "depth-clip-control"

%%% After:
gpuFeature :=
    "texture-compression-bc"
    "depth-clip-control"
    "clip-distances"
```

**New attribute → add to property read rule:**
```
deviceAttrOps :=
    !device!.features
    !device!.limits
    !device!.adapterInfo
```

**New constructor overload → add to variable:**
```
device :=
    @ = await !adapter!.requestDevice();
    @ = await !adapter!.requestDevice({requiredFeatures: [+gpuFeature+]});
```

### 5. Validation

After updating:

1. `dharma -grammars dharma/grammars/<name>.dg -dry-run` — must pass
2. `dharma -grammars dharma/grammars/<name>.dg -seed 42 -count 10` — verify new methods appear
3. `dharma -grammars dharma/grammars/<name>.dg -count 100 -stats` — check no unused rules
4. JS syntax validation on generated output

### 6. IDL Parsing Cheat Sheet

**Interface:**
```idl
[Exposed=Window]
interface GPUDevice : EventTarget {
    readonly attribute GPUSupportedFeatures features;
    GPUBuffer createBuffer(GPUBufferDescriptor descriptor);
};
```
→ Variable: `@ = await !adapter!.requestDevice();`
→ Attribute: `!device!.features`
→ Method: `!device!.createBuffer(+bufferDescriptor+)`

**Enum:**
```idl
enum GPUPowerPreference { "low-power", "high-performance" };
```
→ Value rule with all strings as alternatives

**Dictionary:**
```idl
dictionary GPUBufferDescriptor {
    required GPUSize64 size;
    required GPUBufferUsageFlags usage;
    boolean mappedAtCreation = false;
};
```
→ Value rule: `{size: +bufferSize+, usage: +bufferUsage+, mappedAtCreation: +common:bool+}`

**[RuntimeEnabled]:**
```idl
[RuntimeEnabled=WebGPUSubgroups]
readonly attribute boolean subgroupsSupported;
```
→ Add with a comment noting the feature flag:
```
%%% [RuntimeEnabled=WebGPUSubgroups]
!device!.subgroupsSupported
```

### 7. Tips

- Focus on **modules/** not **core/** — modules contain the high-level APIs
- `.idl` files with `Partial interface` extend existing interfaces — check those too
- `includes` mixins pull in additional methods
- `[SecureContext]` APIs only work on HTTPS — the dharma server is HTTP, so these won't work in iframe/tab mode (use CDP with `--unsafely-treat-insecure-origin-as-secure`)
- Some IDL files have `[ImplementedAs=...]` which maps to a different C++ class — useful for finding the implementation but doesn't affect the grammar
