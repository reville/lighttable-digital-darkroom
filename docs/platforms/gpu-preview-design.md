# GPU-resident preview presentation for Windows and Linux (design)

Status: design only. No spike is committed (see "Why no spike is committed"
below). This document is the concrete crate/API plan, the risks, and an
estimate for a follow-up implementation.

## Problem

On macOS, `windows-shell`'s counterpart (`app/NativePreview.swift`) owns a
Metal layer under the WKWebView and presents the resident engine's RGBA8
surface directly: no browser image codec, no WebGL texture upload, no extra
copy once the surface is on the GPU. Windows and Linux have no equivalent —
`web/app.js`'s `NATIVE_PREVIEW` gate (`window.webkit?.messageHandlers?.lightTable`)
is only true inside the WKWebView bridge, so `windows-shell` always falls
back to the WebGL presenter (`GradeRenderer` in `web/gl.js`). This change
(see `docs/performance.md`) removed the JPEG encode/decode from that
fallback by fetching the engine's packed RGBA8 surface raw and uploading it
with `texImage2D`, but the frame still crosses: engine → CPU copy → HTTP
response body → CPU copy → WebGL texture. A GPU-resident path would let the
engine's output reach the screen without leaving GPU memory (or with at most
one CPU-side copy into a shared texture), and would unlock the same 1:1
viewport tiling the Metal path already gets (see "Viewport tiling" in
`viewportRegionEnabled()`, `web/app.js`).

## Shape of the design

A child window, positioned and sized by JS (the same `nativeViewportLayout`
message `windows-shell` already relays for window chrome), holding a wgpu
surface that:

1. Reads the resident engine's output from the same shared-memory surface
   `server.py`'s `_NATIVE_SHARED`/`retain_native_shared` already produce on
   POSIX (Linux) — `native_shared` in `render_rust`'s request is
   `sys.platform == "darwin"` today; the surface *format* (packed RGBA8 with
   the `FLRA` header, see `NATIVE_SURFACE_HEADER` in server.py) is already
   platform-agnostic, only the darwin-only gate on using shared memory
   instead of a file needs relaxing for Linux. Windows has no POSIX
   `shm_open`; it would use a named `CreateFileMapping` section instead (a
   new small module parallel to `rust-engine/src/export_surface.rs`, which
   is `#[cfg(unix)]`-gated today and returns an error otherwise).
2. Uploads that surface into a wgpu texture (`Queue::write_texture`, one CPU
   copy — the same cost the WebGL path already pays for `texImage2D`; going
   fully zero-copy needs a shared GPU handle, e.g. a Vulkan external memory
   import or a `wgpu::hal` escape hatch keyed to the resident engine's own
   `wgpu::Device`, which is a materially bigger project, see "Full
   zero-copy" below).
3. Runs the grade tier in `rust-engine/src/grade_gpu.wgsl` as a fragment
   shader over that texture — the interactive grade (exposure, contrast,
   curves, HSL, masks) already lives in WGSL for the resident compute
   backend, so the same shader source is reusable for a graphics pipeline
   with a different bind-group layout (compute uses a storage buffer today;
   presentation wants a sampled texture and a render target).
4. Presents to the child window's surface, replacing what the WebGL canvas
   shows today. The DOM canvas would become a click/hover hit-target only
   (`pointer-events` still route through it) while the actual pixels are the
   native child window's.

### Crates

- `wgpu = "30.0.1"` — already a workspace dependency
  (`rust-engine/vendor/spektrafilm/Cargo.toml`), used today only through
  `spektrafilm_gpu::ComputeBackend` for compute passes. This design adds a
  `Surface`/`Instance`/graphics `RenderPipeline` use of the same crate
  version, in a new module (not in the resident engine process — see
  "Which process hosts the surface" below).
- `raw-window-handle = "0.6"` (wgpu 30's expected version) to hand wgpu a
  window/display handle from whichever windowing crate owns the child
  window.
- Windowing: `tao = "0.37"` is already a `windows-shell` dependency for the
  main window. A child surface needs either (a) a second `tao::window::Window`
  parented to the main one, or (b) a raw child `HWND`/`X11 Window` created
  directly with `windows-sys`/`x11rb` and driven by `windows-shell`'s
  existing `tao` event loop via `raw_window_handle` alone (no second event
  loop). (b) is very likely the right call: `tao` 0.37 does not expose a
  documented cross-platform "child window with no decorations, embedded in
  a parent's client area" builder, and inventing one on top of `tao` is a
  bigger and more fragile surface than reading the parent HWND with
  `wry::WebView::window()` equivalents and creating a plain child window
  next to it.

### Which process hosts the surface

Two options:
- **In `windows-shell` itself.** The shell already receives
  `nativeViewportLayout` postMessages and already talks to `server.py` over
  HTTP for everything else. It would additionally read the shared preview
  surface directly (bypassing HTTP for the pixel data, keeping HTTP for the
  JSON metadata that names the surface) and own the wgpu device/surface.
  This matches the Metal design (`app/NativePreview.swift` is part of the
  native shell, not the web layer) and is the recommended shape.
- **A separate helper process.** Not recommended: it would duplicate the
  IPC `windows-shell` already has with `server.py` for no benefit, since
  `windows-shell` already exists as the natural home for platform-native
  presentation.

### Grade parity

`grade_gpu.wgsl`'s compute entry point takes a flat `params: array<f32, 1205>`
buffer (see `rust-engine/src/grade_gpu.rs::parameters`) and a storage-buffer
image. A presentation pipeline needs:
- The same parameter buffer, uploaded on every grade change (already cheap:
  it is what `nativePreview`'s `grade` payload carries today for Metal).
- A second WGSL entry point (`@fragment`) that samples the source texture
  instead of indexing a storage buffer, sharing the actual tone/color math
  with the existing `@compute` entry via a common `fn grade_pixel(...)`
  function already factored out in the file (or factored out as part of
  this work if it is not already share-shaped) so the two entry points
  cannot drift.
- Masks (`nativeMasks` payload) as a second sampled texture, exactly as
  Metal's shader already composites them.

## Risks

- **WebView2 airspace (Windows).** WebView2 (Chromium) child windows are
  known to have compositing/airspace conflicts with sibling HWNDs
  overlapping their client area — floating toolbars, dropdowns, and
  drag-and-drop overlays historically fight z-order with embedded native
  child windows in Chromium-hosted apps. This is the single biggest open
  risk: it needs to be spiked on real Windows hardware (not available on
  this Mac) before committing to the approach, specifically with
  `WebView2Controller::put_IsVisible` sequencing and whatever overlay UI
  LightTable draws above the preview (crop guides, mask overlays, the
  reference-match ghost image) — those currently draw in the same WebGL
  canvas or a DOM layer above it, and would need to stay above a native
  child window too, which either means keeping them as transparent
  DOM/canvas layers z-ordered above the WebView2 host window's z-order
  relative to the child HWND (fragile) or moving overlay drawing into the
  same wgpu surface (a much larger change).
- **WebKitGTK compositing (Linux).** WebKitGTK supports windowed and
  windowless (offscreen, composited into the GTK widget tree) modes; `wry`
  on Linux uses GTK's WebKitGTK binding. A child `GtkWidget`/raw X11 window
  embedded via `gtk_socket`/XEmbed or a raw override-redirect window faces
  similar z-order and input-routing fragility, worse under Wayland (no
  stable cross-compositor subsurface-embedding story equivalent to X11's
  XEmbed; GNOME/KDE Wayland compositors differ). This likely means an
  X11-only initial implementation with Wayland staying on the WebGL/raw
  path (via `WAYLAND_DISPLAY` detection at startup), which is an acceptable
  fallback since the raw-transport WebGL path already ships from this work.
- **DPI.** `windows-shell` already resizes/repositions in logical points and
  the resident engine renders at a pixel width chosen by
  `automaticPreviewWidth()` (device-scale aware, see `web/view-performance.js`).
  A child window's backing scale factor must track the same device-pixel
  ratio changes tao's `WindowEvent::ScaleFactorChanged` reports for the
  parent, including a monitor change mid-session (dragging the window
  between a HiDPI and a standard display) — this is exactly the class of
  bug the existing native viewport layout code
  (`scheduleNativeViewportLayout`, `nativeViewportPayload`) was written to
  get right for Metal, so the message contract can likely be reused
  verbatim; the risk is purely in wiring a second consumer of it correctly.
- **Input routing.** Pointer events for pan/zoom/crop/mask editing currently
  land on the DOM canvas element and are handled by `web/app.js`. A
  transparent or input-transparent child window must forward pointer/wheel/
  keyboard events it receives back through the same JS handlers (or the
  child window must be deliberately excluded from hit-testing so events
  always fall through to the DOM canvas beneath it) — getting this wrong
  either breaks editing gestures or breaks the point of a native surface
  (event round-trip latency). The Metal design solves this by having AppKit
  route input through the WKWebView's normal responder chain since
  `NativePreview` is a subview, not a separate window; a Windows/Linux child
  *window* (rather than an embedded view) does not get this for free and
  needs its own explicit event-forwarding path.
- **Process/lifecycle.** The wgpu device and surface need to survive photo
  navigation (recreated texture, same device/surface) and be torn down
  cleanly on window close/minimize (Windows) or workspace switch (Linux)
  without leaking GPU memory — `windows-shell`'s existing `CloseAttempts`
  lifecycle would need a matching teardown hook.

## Estimate

- Windows child-window + wgpu surface + raw base-frame presentation
  (no grade, no masks, no viewport tiling): **3-5 days**, contingent on the
  WebView2 airspace risk above not requiring a fundamentally different
  approach (e.g., abandoning a native child window for a DXGI swap-chain
  composited via WebView2's own compositor, if Microsoft's APIs allow
  handing it a shared texture — worth investigating before writing HWND
  code, since it would sidestep the airspace risk entirely at the cost of
  depending on WebView2-specific composition APIs).
- Linux (X11) equivalent: **2-3 days** once the Windows plumbing establishes
  the `windows-shell`-side shared-memory reader and wgpu setup pattern to
  copy from. Wayland: not planned; falls back to the WebGL/raw-transport
  path.
- Grade parity (WGSL fragment entry point, masks, live parameter updates):
  **3-4 days**, plus visual-parity testing against the Metal shader's output
  (the resident engine's compute shader already has to match Metal, so this
  is "match a shader that already has to match another shader again",
  which has historically been where bugs hide — see `bench/` and
  `tests/native_processing_preview.py` for the existing numeric-parity
  approach that a WGSL fragment path would need an equivalent of on
  Windows/Linux, which cannot be run on this Mac).
- Viewport tiling (matching `S.nativeViewport`'s partial-frame compositing,
  see the note in `viewportRegionEnabled()`): **2-3 days** on top of the
  above, since it is a virtual-canvas/offset-texture problem independent of
  the GPU-resident work.
- **Total: roughly 3-4 weeks** of focused work plus real Windows and Linux
  hardware for the airspace/compositing risks, which is the reason this
  lands as a design rather than a spike from a Mac-only environment.

## Why no spike is committed

A "minimal spike" that only proves "an engine-fed wgpu surface renders the
base frame in a child window, no grade" still needs a real child
HWND/X11-window under a real WebView2/WebKitGTK host to say anything
meaningful about the biggest risk (airspace/compositing) — a spike that
only proves wgpu can render into *some* window proves very little, since
that much already works in every wgpu example in the ecosystem. Writing
that spike on this Mac, without Windows or Linux hardware to run it on,
would produce code nobody has run, which is worse than no code: it would
look validated without being validated. This is left as a design
(concrete crate choices, the shared-surface format that already exists and
is reusable, and the specific risks to spike first on real hardware) rather
than an unverified spike behind a feature flag. **NOT DONE**, reason:
requires Windows and Linux hardware this environment does not have.

## Viewport tiling (a smaller, related gap)

Independent of the GPU-resident work above: this change's server-side
plumbing (`server.py`'s `want_surface = native or raw` gate on
`render_preview`) already lets a raw-transport WebGL client ask for the same
1:1 viewport tiles the Metal path uses (`viewport` in the request), but
`web/app.js`'s `viewportRegionEnabled()` still requires `nativePreviewActive()`
because presenting a tile also needs the Metal presenter's virtual-canvas
compositing: positioning a partial-frame texture at an offset while panning
(`S.nativeViewport`, `nativeViewportPayload()`, `scheduleNativeViewportLayout()`).
Building that for the WebGL/raw path (an oversized virtual canvas, a
texture atlas or offset `texSubImage2D` writes, and coordinate-mapped shader
sampling) is a self-contained follow-up, decoupled from whether the
GPU-resident presenter above ever ships — **NOT DONE** here, left for a
dedicated change once the base-frame raw transport has been used in
production for a while.
