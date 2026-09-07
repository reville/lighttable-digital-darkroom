use std::{
    collections::VecDeque,
    ffi::CString,
    fs,
    io::{self, BufRead, BufWriter, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant, UNIX_EPOCH},
};

use anyhow::{Context, Result, anyhow, bail};
use image::ImageEncoder;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use spektrafilm_core::{params::RuntimeParams, pipeline::Pipeline, profile};
use spektrafilm_gpu::ComputeBackend;
use spektrafilm_math::{image::ImageBuf, precision};

mod export;
mod export_surface;
mod region;
mod native_surface;

#[derive(Debug, Deserialize)]
struct Request {
    id: u64,
    #[serde(default = "default_command")]
    command: String,
    input: Option<PathBuf>,
    input_shm: Option<String>,
    input_shm_len: Option<usize>,
    input_cache_key: Option<String>,
    output: Option<PathBuf>,
    native_output: Option<PathBuf>,
    #[serde(default)]
    native_shared: bool,
    #[serde(default)]
    export_shared: bool,
    viewport: Option<region::Rect>,
    data_dir: Option<PathBuf>,
    film: Option<String>,
    paper: Option<String>,
    #[serde(default)]
    scan_film: bool,
    params: Option<RuntimeParams>,
    #[serde(default = "default_quality")]
    quality: u8,
    #[serde(default = "default_bit_depth")]
    bit_depth: u8,
    #[serde(default)]
    rotate_quarters_ccw: u8,
    grade: Option<serde_json::Value>,
    masks: Option<serde_json::Value>,
    crop: Option<serde_json::Value>,
    long_edge: Option<u32>,
}

fn default_command() -> String {
    "render".to_owned()
}

fn default_quality() -> u8 {
    88
}

fn default_bit_depth() -> u8 {
    8
}

#[derive(Debug, Serialize)]
struct Response {
    id: u64,
    ok: bool,
    backend: String,
    width: Option<u32>,
    height: Option<u32>,
    full_width: Option<u32>,
    full_height: Option<u32>,
    mean: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_shared: Option<native_surface::SharedSurface>,
    #[serde(skip_serializing_if = "Option::is_none")]
    export_shared: Option<export_surface::SharedExport>,
    input_cache_hit: bool,
    viewport_accelerated: bool,
    native_gpu_packed: bool,
    film_stage_cache_hit: bool,
    gpu_buffers_reused: bool,
    resident_cache_bytes: usize,
    pipeline_cache_hit: bool,
    load_ms: f64,
    pipeline_ms: f64,
    render_ms: f64,
    encode_ms: f64,
    total_ms: f64,
    error: Option<String>,
}

impl Response {
    fn error(id: u64, backend: &str, started: Instant, error: anyhow::Error) -> Self {
        Self {
            id,
            ok: false,
            backend: backend.to_owned(),
            width: None,
            height: None,
            full_width: None,
            full_height: None,
            mean: None,
            native_shared: None,
            export_shared: None,
            input_cache_hit: false,
            viewport_accelerated: false,
            native_gpu_packed: false,
            film_stage_cache_hit: false,
            gpu_buffers_reused: false,
            resident_cache_bytes: 0,
            pipeline_cache_hit: false,
            load_ms: 0.0,
            pipeline_ms: 0.0,
            render_ms: 0.0,
            encode_ms: 0.0,
            total_ms: millis(started.elapsed()),
            error: Some(format!("{error:#}")),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum InputIdentity {
    File {
        path: PathBuf,
        len: u64,
        modified: Duration,
    },
    Shared(String),
}

struct CachedInput {
    identity: InputIdentity,
    image: Arc<ImageBuf>,
    bytes: usize,
    generation: u64,
    metering: VecDeque<(String, f32)>,
}

struct Engine {
    backend: Box<dyn ComputeBackend>,
    inputs: VecDeque<CachedInput>,
    input_cache_max_bytes: usize,
    pipelines: VecDeque<(String, Pipeline, u64)>,
    print_profiles: VecDeque<(String, profile::Profile)>,
    pipeline_cache_max_entries: usize,
    next_input_generation: u64,
    next_pipeline_generation: u64,
    reuse_film_stages: bool,
}

impl Engine {
    fn new() -> Self {
        Self {
            next_input_generation: 0,
            next_pipeline_generation: 0,
            reuse_film_stages: std::env::var("LIGHTTABLE_RESIDENT_STAGE_CACHE").as_deref()
                != Ok("0"),
            backend: spektrafilm_gpu::select_backend(),
            inputs: VecDeque::new(),
            input_cache_max_bytes: std::env::var("LIGHTTABLE_RESIDENT_INPUT_CACHE_BYTES")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(256 * 1024 * 1024),
            pipelines: VecDeque::new(),
            print_profiles: VecDeque::new(),
            pipeline_cache_max_entries: std::env::var("LIGHTTABLE_RESIDENT_PIPELINE_CACHE_ENTRIES")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(4),
        }
    }

    fn handle(&mut self, request: Request) -> Result<Response> {
        let started = Instant::now();
        if request.command == "ping" || request.command == "probe_input" {
            let cached_input = if request.command == "probe_input" {
                let key = request.input_cache_key.as_ref().context("missing input_cache_key")?;
                self.inputs.iter().position(|cached| {
                    cached.identity == InputIdentity::Shared(key.clone())
                }).map(|position| {
                    let cached = self.inputs.remove(position).expect("cached input position");
                    let dimensions = (cached.image.width, cached.image.height);
                    self.inputs.push_back(cached);
                    dimensions
                })
            } else {
                None
            };
            return Ok(Response {
                id: request.id,
                ok: true,
                backend: self.backend.name().to_owned(),
                width: cached_input.map(|size| size.0),
                height: cached_input.map(|size| size.1),
                full_width: None,
                full_height: None,
                mean: None,
            native_shared: None,
                export_shared: None,
                input_cache_hit: cached_input.is_some(),
                viewport_accelerated: false,
            native_gpu_packed: false,
            film_stage_cache_hit: false,
                gpu_buffers_reused: false,
                resident_cache_bytes: 0,
                pipeline_cache_hit: false,
                load_ms: 0.0,
                pipeline_ms: 0.0,
                render_ms: 0.0,
                encode_ms: 0.0,
                total_ms: millis(started.elapsed()),
                error: None,
            });
        }
        if request.command != "render" {
            bail!("unsupported command '{}'", request.command);
        }

        let output = request.output.as_deref();
        let native_output = request.native_output.as_deref();
        if output.is_none() && native_output.is_none() && !request.export_shared {
            bail!("missing output");
        }
        let data_dir = request.data_dir.as_deref().context("missing data_dir")?;
        let film_name = request.film.as_deref().context("missing film")?;
        let mut params = request.params.context("missing params")?;
        params.io.scan_film = request.scan_film;

        let load_started = Instant::now();
        let identity = if let Some(key) = request.input_cache_key.as_deref() {
            InputIdentity::Shared(key.to_owned())
        } else {
            input_identity(request.input.as_deref().context("missing input")?)?
        };
        let cached_position = self
            .inputs
            .iter()
            .position(|cached| cached.identity == identity);
        let input_cache_hit = cached_position.is_some();
        let image = if let Some(position) = cached_position {
            let cached = self.inputs.remove(position).expect("cached input position");
            let image = Arc::clone(&cached.image);
            self.inputs.push_back(cached);
            image
        } else {
            let loaded = if let Some(name) = request.input_shm.as_deref() {
                load_shared_input(
                    name,
                    request.input_shm_len.context("missing input_shm_len")?,
                )?
            } else {
                load_tiff(request.input.as_deref().context("missing input")?)?
            };
            let image = Arc::new(loaded);
            let bytes = std::mem::size_of_val(image.data.as_slice());
            self.next_input_generation += 1;
            self.inputs.push_back(CachedInput {
                generation: self.next_input_generation,
                metering: VecDeque::new(),
                identity,
                image: Arc::clone(&image),
                bytes,
            });
            while self.inputs.len() > 1
                && self.inputs.iter().map(|cached| cached.bytes).sum::<usize>()
                    > self.input_cache_max_bytes
            {
                self.inputs.pop_front();
            }
            image
        };
        let load_ms = millis(load_started.elapsed());

        let print_name = if request.scan_film {
            film_name
        } else {
            request.paper.as_deref().context("missing paper")?
        };
        // Include the profile root: two installations may use the same stock names.
        let profile_root = fs::canonicalize(data_dir)?;
        let key = format!(
            "{}:{}",
            profile_root.display(),
            pipeline_cache_key(film_name, &params)
        );
        let active_input = self.inputs.back_mut().expect("active cached input");
        let reuse_film_stages = self.reuse_film_stages && self.backend.is_gpu();
        let film_key =
            reuse_film_stages.then(|| film_stage_key(active_input.generation, &key, &params));
        let metered_ev = if reuse_film_stages && params.camera.auto_exposure {
            let meter_key = serde_json::to_string(&(
                &params.io.input_color_space,
                &params.camera.auto_exposure_method,
            ))?;
            if let Some((_, value)) = active_input
                .metering
                .iter()
                .find(|(key, _)| key == &meter_key)
            {
                Some(*value)
            } else {
                let matrix = spektrafilm_core::stages::filming::input_colorspace_to_xyz(
                    &params.io.input_color_space,
                );
                let value = spektrafilm_core::stages::filming::measure_autoexposure_ev(
                    &image,
                    &matrix,
                    &params.camera.auto_exposure_method,
                );
                active_input.metering.push_back((meter_key, value));
                while active_input.metering.len() > 8 {
                    active_input.metering.pop_front();
                }
                Some(value)
            }
        } else {
            None
        };
        let pipeline_started = Instant::now();
        let print_key = format!("{}:{print_name}", profile_root.display());
        let print_profile = if let Some(position) = self.print_profiles.iter()
            .position(|(key, _)| key == &print_key) {
            let cached = self.print_profiles.remove(position).expect("cached print position");
            let print = cached.1.clone();
            self.print_profiles.push_back(cached);
            print
        } else {
            let print = profile::load_profile_by_name(data_dir, print_name)
                .map_err(|error| anyhow!("print profile '{print_name}': {error}"))?;
            self.print_profiles.push_back((print_key, print.clone()));
            while self.print_profiles.len() > 16 { self.print_profiles.pop_front(); }
            print
        };
        let cached_position = self
            .pipelines
            .iter()
            .position(|(cached_key, _, _)| cached_key == &key);
        let pipeline_cache_hit = cached_position.is_some();
        let (pipeline, pipeline_generation) = if let Some(position) = cached_position {
            let cached = self
                .pipelines
                .remove(position)
                .expect("cached pipeline position");
            let active = cached.1.clone().with_print_params(print_profile, params);
            let generation = cached.2;
            self.pipelines.push_back(cached);
            (active, generation)
        } else {
            let film = profile::load_profile_by_name(data_dir, film_name)
                .map_err(|error| anyhow!("film profile '{film_name}': {error}"))?;
            let built = Pipeline::new_with_spectral(film, print_profile, params, data_dir)
                .map_err(|error| anyhow!("pipeline build: {error}"))?;
            self.next_pipeline_generation += 1;
            self.pipelines
                .push_back((key, built.clone(), self.next_pipeline_generation));
            while self.pipelines.len() > self.pipeline_cache_max_entries {
                self.pipelines.pop_front();
            }
            (built, self.next_pipeline_generation)
        };
        let film_key = film_key.map(|key| format!("{pipeline_generation}:{key}"));
        let pipeline_ms = millis(pipeline_started.elapsed());

        if request.viewport.is_some() && (request.grade.is_some() || request.masks.is_some()
            || request.crop.is_some() || request.long_edge.is_some()) {
            bail!("viewport rendering requires an unbaked native preview");
        }
        let plan = request.viewport.map(|viewport| region::plan(viewport, image.width, image.height,
            request.rotate_quarters_ccw, &pipeline.params,
            self.backend.name().to_lowercase().contains("wgpu"))).transpose()?;
        let accelerated = plan.as_ref().is_some_and(|plan| plan.accelerated);
        // Meter the original before the spatial crop, even with checkpoint reuse disabled.
        let metered_ev = if accelerated && metered_ev.is_none() && pipeline.params.camera.auto_exposure {
            let matrix = spektrafilm_core::stages::filming::input_colorspace_to_xyz(
                &pipeline.params.io.input_color_space);
            Some(spektrafilm_core::stages::filming::measure_autoexposure_ev(&image, &matrix,
                &pipeline.params.camera.auto_exposure_method))
        } else { metered_ev };
        let region_image = plan.as_ref().filter(|plan| plan.accelerated)
            .map(|plan| region::crop_image(&image, plan.render));
        let render_image = region_image.as_ref().unwrap_or(image.as_ref());
        let pipeline = if let Some(plan) = plan.as_ref().filter(|plan| plan.accelerated) {
            pipeline.with_image_region(plan.render.x, plan.render.y, image.width, image.height)
        } else { pipeline };
        let film_key = film_key.map(|key| if let Some(plan) = plan.as_ref() {
            format!("{key}:region:{:?}", plan.render.array())
        } else { key });
        let render_started = Instant::now();
        let native_only = native_output.is_some() && output.is_none()
            && !request.export_shared
            && request.grade.is_none() && request.masks.is_none()
            && request.crop.is_none() && request.long_edge.is_none();
        let packed = if native_only {
            pipeline.process_resident_native(render_image, self.backend.as_ref(),
                film_key.as_deref(), metered_ev,
                spektrafilm_gpu::NativeOutputSpec {
                    quarters_ccw: request.rotate_quarters_ccw, crop: plan.as_ref().map(|plan| plan.trim.array()),
                })
        } else { None };
        let resident_rendered = if packed.is_none() {
            pipeline.process_resident_cached(render_image, self.backend.as_ref(),
                film_key.as_deref(), metered_ev)
        } else { None };
        if accelerated && packed.is_none() && resident_rendered.is_none() {
            bail!("GPU viewport path unavailable; retry a full-frame render");
        }
        let used_resident = packed.is_some() || resident_rendered.is_some();
        let rendered = if packed.is_none() {
            Some(resident_rendered.unwrap_or_else(||
                pipeline.process((*image).clone(), self.backend.as_ref())))
        } else { None };
        let mut cache_status = self.backend.resident_cache_status();
        if !used_resident {
            cache_status.0 = false;
            cache_status.1 = false;
        }
        let render_ms = millis(render_started.elapsed());

        let encode_started = Instant::now();
        let (mut width, mut height, mut samples) = if let Some(packed) = packed.as_ref() {
            (packed.width, packed.height, Vec::new())
        } else {
            let rendered = rendered.as_ref().expect("RGB render");
            let trimmed = plan.as_ref().map(|plan| region::crop_image(rendered, plan.trim));
            rotate_samples(trimmed.as_ref().unwrap_or(rendered), request.rotate_quarters_ccw)
        };
        if request.grade.is_some()
            || request.masks.is_some()
            || request.crop.is_some()
            || request.long_edge.is_some()
        {
            let processed = export::postprocess(
                width,
                height,
                samples,
                request.grade.as_ref(),
                request.masks.as_ref(),
                request.crop.as_ref(),
                request.long_edge,
            )?;
            width = processed.width;
            height = processed.height;
            samples = processed.samples;
        }
        let mean = packed.as_ref().map_or_else(|| samples.par_iter()
            .map(|&value| f64::from(value)).sum::<f64>() / samples.len() as f64,
            |packed| packed.mean);
        if let Some(output) = output {
            save_output(
                output,
                width,
                height,
                &samples,
                request.quality,
                request.bit_depth,
            )?;
        }
        let native_shared = if request.native_shared && native_output.is_some() {
            let publish = if let Some(packed) = packed.as_ref() {
                native_surface::publish_packed(width, height, packed.row_bytes, &packed.pixels)
            } else {
                native_surface::publish_rgb(width, height, &samples)
            };
            match publish {
                Ok(surface) => Some(surface),
                Err(error) => {
                    eprintln!("native shared transport unavailable: {error:#}");
                    None
                }
            }
        } else { None };
        if native_shared.is_none() {
            if let Some(native_output) = native_output {
                if let Some(packed) = packed.as_ref() {
                    save_packed_native_surface(native_output, packed)?;
                } else {
                    save_native_surface(native_output, width, height, &samples)?;
                }
            }
        }
        let export_shared = if request.export_shared {
            Some(export_surface::publish(width, height, &samples)?)
        } else { None };
        let encode_ms = millis(encode_started.elapsed());

        Ok(Response {
            id: request.id,
            ok: true,
            backend: self.backend.name().to_owned(),
            width: Some(width),
            height: Some(height),
            full_width: request.viewport.map(|_| if request.rotate_quarters_ccw % 2 == 0 { image.width } else { image.height }),
            full_height: request.viewport.map(|_| if request.rotate_quarters_ccw % 2 == 0 { image.height } else { image.width }),
            mean: Some(mean),
            native_shared,
            export_shared,
            input_cache_hit,
            viewport_accelerated: accelerated,
            native_gpu_packed: packed.is_some(),
            film_stage_cache_hit: cache_status.0,
            gpu_buffers_reused: cache_status.1,
            resident_cache_bytes: cache_status.2,
            pipeline_cache_hit,
            load_ms,
            pipeline_ms,
            render_ms,
            encode_ms,
            total_ms: millis(started.elapsed()),
            error: None,
        })
    }
}

fn input_identity(path: &Path) -> Result<InputIdentity> {
    let metadata = fs::metadata(path).with_context(|| format!("reading {}", path.display()))?;
    Ok(InputIdentity::File {
        path: path.canonicalize().unwrap_or_else(|_| path.to_owned()),
        len: metadata.len(),
        modified: metadata.modified()?.duration_since(UNIX_EPOCH)?,
    })
}

const RAW_SHARED_MAGIC: &[u8; 4] = b"LTRI";
const RAW_SHARED_HEADER_BYTES: usize = 16;

fn parse_shared_input(bytes: &[u8]) -> Result<ImageBuf> {
    if bytes.len() < RAW_SHARED_HEADER_BYTES || &bytes[0..4] != RAW_SHARED_MAGIC {
        bail!("invalid shared RAW header");
    }
    let width = u32::from_le_bytes(bytes[4..8].try_into()?);
    let height = u32::from_le_bytes(bytes[8..12].try_into()?);
    let row_bytes = u32::from_le_bytes(bytes[12..16].try_into()?);
    if width == 0 || height == 0 || row_bytes != width.checked_mul(6).context("row overflow")? {
        bail!("invalid shared RAW dimensions");
    }
    let sample_count = usize::try_from(width)?
        .checked_mul(usize::try_from(height)?)
        .and_then(|pixels| pixels.checked_mul(3))
        .context("shared RAW sample overflow")?;
    let payload_bytes = sample_count
        .checked_mul(2)
        .context("shared RAW byte overflow")?;
    let required = RAW_SHARED_HEADER_BYTES
        .checked_add(payload_bytes)
        .context("shared RAW length overflow")?;
    if bytes.len() != required {
        bail!(
            "shared RAW length mismatch: got {}, expected {required}",
            bytes.len()
        );
    }
    let data = bytes[RAW_SHARED_HEADER_BYTES..]
        .par_chunks_exact(2)
        .map(|pair| {
            precision::from_f32(f32::from(u16::from_le_bytes([pair[0], pair[1]])) / 65535.0)
        })
        .collect();
    Ok(ImageBuf::from_data(width, height, data))
}

#[cfg(unix)]
fn load_shared_input(name: &str, length: usize) -> Result<ImageBuf> {
    if !(RAW_SHARED_HEADER_BYTES..=2 * 1024 * 1024 * 1024).contains(&length) {
        bail!("invalid shared RAW length {length}");
    }
    let bare = name.strip_prefix('/').unwrap_or(name);
    if bare.is_empty()
        || bare.len() > 128
        || !bare
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        bail!("invalid shared-memory name");
    }
    let c_name = CString::new(format!("/{bare}"))?;
    // The Python owner retains and unlinks the object only after this
    // synchronous request returns.
    let fd = unsafe { libc::shm_open(c_name.as_ptr(), libc::O_RDONLY, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error())
            .with_context(|| format!("opening shared RAW {bare}"));
    }
    let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, metadata.as_mut_ptr()) } != 0 {
        let error = io::Error::last_os_error();
        unsafe {
            libc::close(fd);
        }
        return Err(error).with_context(|| format!("sizing shared RAW {bare}"));
    }
    let actual_length = unsafe { metadata.assume_init() }.st_size;
    // Darwin rounds POSIX shared-memory objects to a VM page even though
    // Python exposes only the requested byte span. Reject truncation, but map
    // exactly the protocol length and ignore the harmless page tail.
    if actual_length < 0 || (actual_length as usize) < length {
        unsafe {
            libc::close(fd);
        }
        bail!("shared RAW is truncated: request {length}, object {actual_length}");
    }
    let address = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            length,
            libc::PROT_READ,
            libc::MAP_SHARED,
            fd,
            0,
        )
    };
    unsafe {
        libc::close(fd);
    }
    if address == libc::MAP_FAILED {
        return Err(io::Error::last_os_error())
            .with_context(|| format!("mapping shared RAW {bare}"));
    }
    let bytes = unsafe { std::slice::from_raw_parts(address.cast::<u8>(), length) };
    let result = parse_shared_input(bytes);
    unsafe {
        libc::munmap(address, length);
    }
    result
}

#[cfg(windows)]
fn load_shared_input(name: &str, length: usize) -> Result<ImageBuf> {
    use std::os::windows::ffi::OsStrExt;

    if !(RAW_SHARED_HEADER_BYTES..=2 * 1024 * 1024 * 1024).contains(&length) {
        bail!("invalid shared RAW length {length}");
    }
    // Python's multiprocessing.shared_memory names Windows segments
    // `wnsm_<hex>`; anything outside that alphabet is not a segment the
    // server created for this request.
    if name.is_empty()
        || name.len() > 128
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        bail!("invalid shared-memory name");
    }
    let wide: Vec<u16> = std::ffi::OsStr::new(name)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    // The Python owner keeps its own mapping handle open until this
    // synchronous request returns, so the pagefile-backed object outlives
    // the copy below even though Windows has no unlink step.
    let mapping = unsafe {
        windows_shared::OpenFileMappingW(windows_shared::FILE_MAP_READ, 0, wide.as_ptr())
    };
    if mapping.is_null() {
        return Err(io::Error::last_os_error())
            .with_context(|| format!("opening shared RAW {name}"));
    }
    // Asking for exactly the protocol length makes the view fail rather
    // than silently shorten when the object is smaller than advertised.
    let address = unsafe {
        windows_shared::MapViewOfFile(mapping, windows_shared::FILE_MAP_READ, 0, 0, length)
    };
    let map_error = io::Error::last_os_error();
    unsafe {
        windows_shared::CloseHandle(mapping);
    }
    if address.is_null() {
        return Err(map_error).with_context(|| format!("mapping shared RAW {name}"));
    }
    let result = windows_shared::view_length(address).and_then(|available| {
        if available < length {
            bail!("shared RAW is truncated: request {length}, view {available}");
        }
        let bytes = unsafe { std::slice::from_raw_parts(address.cast::<u8>(), length) };
        parse_shared_input(bytes)
    });
    unsafe {
        windows_shared::UnmapViewOfFile(address);
    }
    result
}

/// The handful of kernel32 entry points needed to read a named file mapping.
/// Declared by hand so the engine gains no Windows-only crate dependency.
#[cfg(windows)]
mod windows_shared {
    use std::ffi::c_void;

    use anyhow::{Context, Result};

    pub const FILE_MAP_READ: u32 = 0x0004;

    /// `MEMORY_BASIC_INFORMATION` from the Windows SDK, including the
    /// `PartitionId` field that pads `RegionSize` to pointer alignment.
    #[repr(C)]
    pub struct MemoryBasicInformation {
        pub base_address: *mut c_void,
        pub allocation_base: *mut c_void,
        pub allocation_protect: u32,
        pub partition_id: u16,
        pub region_size: usize,
        pub state: u32,
        pub protect: u32,
        pub kind: u32,
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        pub fn OpenFileMappingW(desired_access: u32, inherit_handle: i32, name: *const u16)
        -> *mut c_void;
        pub fn MapViewOfFile(
            mapping: *mut c_void,
            desired_access: u32,
            offset_high: u32,
            offset_low: u32,
            bytes: usize,
        ) -> *mut c_void;
        pub fn UnmapViewOfFile(address: *const c_void) -> i32;
        pub fn CloseHandle(handle: *mut c_void) -> i32;
        pub fn VirtualQuery(
            address: *const c_void,
            buffer: *mut MemoryBasicInformation,
            length: usize,
        ) -> usize;
    }

    /// Bytes readable from `address` to the end of its mapped region.
    pub fn view_length(address: *const c_void) -> Result<usize> {
        let mut information = std::mem::MaybeUninit::<MemoryBasicInformation>::uninit();
        let written = unsafe {
            VirtualQuery(
                address,
                information.as_mut_ptr(),
                std::mem::size_of::<MemoryBasicInformation>(),
            )
        };
        if written == 0 {
            return Err(std::io::Error::last_os_error()).context("sizing shared RAW view");
        }
        let information = unsafe { information.assume_init() };
        let offset = address as usize - information.base_address as usize;
        Ok(information.region_size.saturating_sub(offset))
    }
}

#[cfg(not(any(unix, windows)))]
fn load_shared_input(_name: &str, _length: usize) -> Result<ImageBuf> {
    bail!("shared RAW input is unavailable on this platform")
}

fn load_tiff(path: &Path) -> Result<ImageBuf> {
    let mut reader = image::ImageReader::open(path)
        .with_context(|| format!("opening {}", path.display()))?
        .with_guessed_format()?;
    reader.no_limits();
    let rgb = reader.decode()?.to_rgb32f();
    let (width, height) = (rgb.width(), rgb.height());
    let data = rgb
        .into_raw()
        .into_par_iter()
        .map(precision::from_f32)
        .collect();
    Ok(ImageBuf::from_data(width, height, data))
}

/// Key the developed-film checkpoint on physical filming dependencies only.
/// Scanner reference settings are downstream here; Pipeline appends the actual
/// filming exposure correction for positive scans before handing the GPU its key.
/// Input and spectral pipeline generations prevent aliasing after cache eviction.
fn film_stage_key(input_generation: u64, pipeline_key: &str, params: &RuntimeParams) -> String {
    let mut dependencies = params.clone();
    let defaults = RuntimeParams::default();
    dependencies.enlarger = defaults.enlarger;
    dependencies.print_render = defaults.print_render;
    dependencies.film_render.glare = defaults.film_render.glare;
    dependencies.scanner = defaults.scanner;
    dependencies.io.scan_film = defaults.io.scan_film;
    dependencies.io.output_color_space = defaults.io.output_color_space;
    dependencies.io.output_cctf_encoding = defaults.io.output_cctf_encoding;
    dependencies.io.output_gamut_compress = defaults.io.output_gamut_compress;
    dependencies.settings.neutral_print_filters_from_database = defaults.settings.neutral_print_filters_from_database;
    dependencies.settings.use_enlarger_lut = defaults.settings.use_enlarger_lut;
    dependencies.settings.use_scanner_lut = defaults.settings.use_scanner_lut;
    serde_json::to_string(&(input_generation, pipeline_key, dependencies))
        .expect("finite runtime parameters")
}

/// Only dependencies baked into the expensive film spectral calibration.
/// Print profiles, enlarger calibration and output conversion are refreshed
/// separately, without rebuilding or copying the shared film TC LUT.
fn pipeline_cache_key(film: &str, params: &RuntimeParams) -> String {
    serde_json::json!({
        "film": film,
        "film_dev": params.film_render.development_time,
        "input_gamut": params.io.input_gamut_compress,
        "rgb_to_raw_method": params.settings.rgb_to_raw_method,
        "apply_hanatos2025_adaptation_window": params.settings.apply_hanatos2025_adaptation_window,
        "apply_hanatos2025_adaptation_surface": params.settings.apply_hanatos2025_adaptation_surface,
        "spectral_gaussian_blur": params.settings.spectral_gaussian_blur,
        "lut_resolution": params.settings.lut_resolution,
    }).to_string()
}

fn rotate_samples(image: &ImageBuf, quarters_ccw: u8) -> (u32, u32, Vec<f32>) {
    let source: Vec<f32> = image
        .data
        .par_iter()
        .map(|&value| precision::to_f32(value).clamp(0.0, 1.0))
        .collect();
    match quarters_ccw % 4 {
        0 => (image.width, image.height, source),
        2 => {
            let mut output = vec![0.0_f32; source.len()];
            for y in 0..image.height {
                for x in 0..image.width {
                    copy_pixel(
                        &source,
                        image.width,
                        x,
                        y,
                        &mut output,
                        image.width,
                        image.width - 1 - x,
                        image.height - 1 - y,
                    );
                }
            }
            (image.width, image.height, output)
        }
        quarter => {
            let mut output = vec![0.0_f32; source.len()];
            for y in 0..image.height {
                for x in 0..image.width {
                    let (destination_x, destination_y) = if quarter == 1 {
                        (y, image.width - 1 - x)
                    } else {
                        (image.height - 1 - y, x)
                    };
                    copy_pixel(
                        &source,
                        image.width,
                        x,
                        y,
                        &mut output,
                        image.height,
                        destination_x,
                        destination_y,
                    );
                }
            }
            (image.height, image.width, output)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn copy_pixel<T: Copy>(
    source: &[T],
    source_width: u32,
    source_x: u32,
    source_y: u32,
    destination: &mut [T],
    destination_width: u32,
    destination_x: u32,
    destination_y: u32,
) {
    let source_index = ((source_y * source_width + source_x) * 3) as usize;
    let destination_index = ((destination_y * destination_width + destination_x) * 3) as usize;
    destination[destination_index..destination_index + 3]
        .copy_from_slice(&source[source_index..source_index + 3]);
}

fn save_output(
    path: &Path,
    width: u32,
    height: u32,
    samples: &[f32],
    quality: u8,
    bit_depth: u8,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let file = fs::File::create(path).with_context(|| format!("creating {}", path.display()))?;
    let mut writer = BufWriter::new(file);
    match extension.as_str() {
        "jpg" | "jpeg" => {
            let data = quantize_u8(samples);
            image::codecs::jpeg::JpegEncoder::new_with_quality(&mut writer, quality).write_image(
                &data,
                width,
                height,
                image::ExtendedColorType::Rgb8,
            )?
        }
        "png" => {
            let data = quantize_u8(samples);
            image::codecs::png::PngEncoder::new(&mut writer).write_image(
                &data,
                width,
                height,
                image::ExtendedColorType::Rgb8,
            )?
        }
        "tif" | "tiff" if bit_depth >= 32 => {
            let data: Vec<u8> = samples
                .iter()
                .flat_map(|value| value.to_ne_bytes())
                .collect();
            image::codecs::tiff::TiffEncoder::new(&mut writer).write_image(
                &data,
                width,
                height,
                image::ExtendedColorType::Rgb32F,
            )?
        }
        "tif" | "tiff" => {
            let data: Vec<u8> = samples
                .iter()
                .flat_map(|value| ((*value * 65535.0).round_ties_even() as u16).to_ne_bytes())
                .collect();
            image::codecs::tiff::TiffEncoder::new(&mut writer).write_image(
                &data,
                width,
                height,
                image::ExtendedColorType::Rgb16,
            )?
        }
        _ => bail!("unsupported output extension '.{extension}'"),
    }
    writer.flush()?;
    Ok(())
}

fn quantize_u8(samples: &[f32]) -> Vec<u8> {
    samples
        .par_iter()
        .map(|&value| ((value * 255.0).round_ties_even()) as u8)
        .collect()
}

/// Write the display-referred render as a memory-mappable RGBA8 surface.
///
/// Layout (all integers little-endian): `FLRA`, width, height, bytes-per-row,
/// followed by tightly packed RGBA pixels. The native AppKit shell uploads
/// this directly into a Metal texture, avoiding JPEG encoding/decoding and a
/// second high-resolution WebGL texture upload.
fn save_packed_native_surface(path: &Path, packed: &spektrafilm_gpu::NativePackedSurface) -> Result<()> {
    if let Some(parent) = path.parent() { fs::create_dir_all(parent)?; }
    let mut writer = BufWriter::new(fs::File::create(path)?);
    writer.write_all(b"FLRA")?;
    writer.write_all(&packed.width.to_le_bytes())?;
    writer.write_all(&packed.height.to_le_bytes())?;
    writer.write_all(&(packed.width * 4).to_le_bytes())?;
    for row in packed.pixels.chunks(packed.row_bytes) {
        writer.write_all(&row[..packed.width as usize * 4])?;
    }
    writer.flush()?;
    Ok(())
}

fn save_native_surface(path: &Path, width: u32, height: u32, samples: &[f32]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let row_bytes = width
        .checked_mul(4)
        .context("native surface row overflow")?;
    let pixel_count = usize::try_from(width)?
        .checked_mul(usize::try_from(height)?)
        .context("native surface size overflow")?;
    if samples.len() != pixel_count * 3 {
        bail!("native surface sample count mismatch");
    }
    let mut rgba = vec![255_u8; pixel_count * 4];
    rgba.par_chunks_mut(4)
        .zip(samples.par_chunks(3))
        .for_each(|(pixel, source)| {
            pixel[0] = (source[0].clamp(0.0, 1.0) * 255.0).round_ties_even() as u8;
            pixel[1] = (source[1].clamp(0.0, 1.0) * 255.0).round_ties_even() as u8;
            pixel[2] = (source[2].clamp(0.0, 1.0) * 255.0).round_ties_even() as u8;
        });

    let file = fs::File::create(path).with_context(|| format!("creating {}", path.display()))?;
    let mut writer = BufWriter::new(file);
    writer.write_all(b"FLRA")?;
    writer.write_all(&width.to_le_bytes())?;
    writer.write_all(&height.to_le_bytes())?;
    writer.write_all(&row_bytes.to_le_bytes())?;
    writer.write_all(&rgba)?;
    writer.flush()?;
    Ok(())
}

fn millis(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

fn main() -> Result<()> {
    let mut engine = Engine::new();
    let backend_name = engine.backend.name().to_owned();
    let stdin = io::stdin();
    let mut stdout = BufWriter::new(io::stdout().lock());

    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let started = Instant::now();
        let response = match serde_json::from_str::<Request>(&line) {
            Ok(request) => {
                let id = request.id;
                engine
                    .handle(request)
                    .unwrap_or_else(|error| Response::error(id, &backend_name, started, error))
            }
            Err(error) => Response::error(0, &backend_name, started, error.into()),
        };
        serde_json::to_writer(&mut stdout, &response)?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotates_one_quarter_counter_clockwise() {
        let image = ImageBuf::from_data(
            2,
            1,
            vec![
                precision::from_f32(10.0 / 255.0),
                precision::from_f32(20.0 / 255.0),
                precision::from_f32(30.0 / 255.0),
                precision::from_f32(40.0 / 255.0),
                precision::from_f32(50.0 / 255.0),
                precision::from_f32(60.0 / 255.0),
            ],
        );
        let (width, height, samples) = rotate_samples(&image, 1);
        let data = quantize_u8(&samples);
        assert_eq!((width, height), (1, 2));
        assert_eq!(data, vec![40, 50, 60, 10, 20, 30]);
    }

    #[test]
    fn float_rotation_preserves_more_than_eight_bits() {
        let value = 0.500_123_f32;
        let image = ImageBuf::from_data(
            1,
            1,
            vec![
                precision::from_f32(value),
                precision::from_f32(value),
                precision::from_f32(value),
            ],
        );
        let (_, _, samples) = rotate_samples(&image, 0);
        assert!((samples[0] - value).abs() < 1e-6);
        assert_ne!(samples[0], f32::from(quantize_u8(&samples)[0]) / 255.0);
    }

    #[test]
    fn input_identity_changes_with_file_metadata() {
        let path =
            std::env::temp_dir().join(format!("lighttable-engine-identity-{}", std::process::id()));
        fs::write(&path, b"first").unwrap();
        let first = input_identity(&path).unwrap();
        fs::write(&path, b"second-longer").unwrap();
        let second = input_identity(&path).unwrap();
        fs::remove_file(path).unwrap();
        assert_ne!(first, second);
    }

    #[test]
    fn shared_raw_surface_decodes_little_endian_rgb16() {
        let mut bytes = Vec::from(*RAW_SHARED_MAGIC);
        bytes.extend_from_slice(&2_u32.to_le_bytes());
        bytes.extend_from_slice(&1_u32.to_le_bytes());
        bytes.extend_from_slice(&12_u32.to_le_bytes());
        for value in [0_u16, 32768, 65535, 16384, 8192, 4096] {
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        let image = parse_shared_input(&bytes).unwrap();
        assert_eq!((image.width, image.height), (2, 1));
        let values: Vec<f32> = image
            .data
            .iter()
            .map(|&value| precision::to_f32(value))
            .collect();
        assert_eq!(values[0], 0.0);
        assert!((values[1] - 32768.0 / 65535.0).abs() < 1e-6);
        assert_eq!(values[2], 1.0);
    }

    #[test]
    fn native_surface_has_stable_header_and_rgba_pixels() {
        let path = std::env::temp_dir().join(format!(
            "filmlab-native-surface-{}.rgba",
            std::process::id()
        ));
        save_native_surface(&path, 2, 1, &[0.0, 0.5, 1.0, 1.0, 0.25, 0.0]).unwrap();
        let bytes = fs::read(&path).unwrap();
        fs::remove_file(path).unwrap();
        assert_eq!(&bytes[0..4], b"FLRA");
        assert_eq!(u32::from_le_bytes(bytes[4..8].try_into().unwrap()), 2);
        assert_eq!(u32::from_le_bytes(bytes[8..12].try_into().unwrap()), 1);
        assert_eq!(u32::from_le_bytes(bytes[12..16].try_into().unwrap()), 8);
        assert_eq!(&bytes[16..], &[0, 128, 255, 255, 255, 64, 0, 255]);
    }
}

#[cfg(all(test, windows))]
mod windows_shared_input_tests {
    use super::*;
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateFileMappingW(
            file: *mut std::ffi::c_void,
            attributes: *mut std::ffi::c_void,
            protect: u32,
            maximum_size_high: u32,
            maximum_size_low: u32,
            name: *const u16,
        ) -> *mut std::ffi::c_void;
    }

    const PAGE_READWRITE: u32 = 0x04;
    const FILE_MAP_WRITE: u32 = 0x0002;

    struct Segment {
        name: String,
        mapping: *mut std::ffi::c_void,
        view: *mut std::ffi::c_void,
        length: usize,
    }

    impl Segment {
        /// Mirror `multiprocessing.shared_memory.SharedMemory(create=True)`:
        /// a pagefile-backed mapping whose handle stays open for the test.
        fn create(length: usize) -> Self {
            let name = format!("wnsm_lighttable_test_{}_{length}", std::process::id());
            let wide: Vec<u16> = std::ffi::OsStr::new(&name)
                .encode_wide()
                .chain(std::iter::once(0))
                .collect();
            let mapping = unsafe {
                CreateFileMappingW(
                    usize::MAX as *mut std::ffi::c_void,
                    std::ptr::null_mut(),
                    PAGE_READWRITE,
                    0,
                    length as u32,
                    wide.as_ptr(),
                )
            };
            assert!(!mapping.is_null(), "{}", io::Error::last_os_error());
            let view = unsafe { windows_shared::MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, 0) };
            assert!(!view.is_null(), "{}", io::Error::last_os_error());
            Self {
                name,
                mapping,
                view,
                length,
            }
        }

        fn write(&self, bytes: &[u8]) {
            assert!(bytes.len() <= self.length);
            unsafe {
                std::ptr::copy_nonoverlapping(bytes.as_ptr(), self.view.cast::<u8>(), bytes.len());
            }
        }
    }

    impl Drop for Segment {
        fn drop(&mut self) {
            unsafe {
                windows_shared::UnmapViewOfFile(self.view);
                windows_shared::CloseHandle(self.mapping);
            }
        }
    }

    fn packed_rgb16(width: u32, height: u32, samples: &[u16]) -> Vec<u8> {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(RAW_SHARED_MAGIC);
        bytes.extend_from_slice(&width.to_le_bytes());
        bytes.extend_from_slice(&height.to_le_bytes());
        bytes.extend_from_slice(&(width * 6).to_le_bytes());
        for sample in samples {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        bytes
    }

    #[test]
    fn named_file_mapping_delivers_packed_rgb16_pixels() {
        let payload = packed_rgb16(2, 1, &[0, 32768, 65535, 7, 8, 9]);
        let segment = Segment::create(payload.len());
        segment.write(&payload);
        let image = load_shared_input(&segment.name, payload.len()).unwrap();
        assert_eq!((image.width, image.height), (2, 1));
        let values: Vec<f32> = image
            .data
            .iter()
            .map(|&value| precision::to_f32(value))
            .collect();
        assert_eq!(values[0], 0.0);
        assert!((values[1] - 32768.0 / 65535.0).abs() < 1e-6);
        assert_eq!(values[2], 1.0);
    }

    #[test]
    fn short_or_missing_mappings_are_refused_instead_of_read_past() {
        let payload = packed_rgb16(2, 1, &[1, 2, 3, 4, 5, 6]);
        let segment = Segment::create(payload.len());
        segment.write(&payload);
        // A request one page beyond the object must fail to map, never
        // return a view that reads unmapped memory.
        assert!(load_shared_input(&segment.name, payload.len() + 4096).is_err());
        assert!(load_shared_input("wnsm_lighttable_missing", payload.len()).is_err());
        assert!(load_shared_input("bad name", payload.len()).is_err());
    }
}

#[cfg(test)]
mod resident_cache_tests {
    use super::*;

    fn changed(group: &str, patch: serde_json::Value) -> RuntimeParams {
        let mut value = serde_json::to_value(RuntimeParams::default()).unwrap();
        for (field, replacement) in patch.as_object().unwrap() {
            value[group][field] = replacement.clone();
        }
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn downstream_changes_reuse_film_and_spectral_calibration() {
        let base = RuntimeParams::default();
        let key = film_stage_key(1, "profile-a", &base);
        let pipeline = pipeline_cache_key("film-a", &base);
        for (group, patch) in [
            ("enlarger", serde_json::json!({"print_exposure":1.4,"y_filter_shift":3.0,"m_filter_shift":-4.0,"preflash_exposure":0.1,"illuminant":"D50","normalize_print_exposure":false})),
            ("print_render", serde_json::json!({"density_curve_gamma":1.2,"development_time":1.3,"glare":{"active":true,"percent":4.0}})),
            ("scanner", serde_json::json!({"lens_blur":2.0,"unsharp_mask":[1.0,1.2],"white_correction":true,"black_level":0.02})),
            ("io", serde_json::json!({"output_color_space":"ProPhoto RGB","output_cctf_encoding":false,"output_gamut_compress":{"algorithm":"off"}})),
            ("film_render", serde_json::json!({"glare":{"active":true,"percent":4.0}})),
            ("settings", serde_json::json!({"neutral_print_filters_from_database":false,"use_enlarger_lut":true,"use_scanner_lut":true})),
        ] {
            let params = changed(group, patch);
            assert_eq!(key, film_stage_key(1,"profile-a",&params), "{group}");
            assert_eq!(pipeline, pipeline_cache_key("film-a",&params), "{group}");
        }
    }

    #[test]
    fn upstream_changes_and_generations_invalidate_film() {
        let base = RuntimeParams::default();
        let key = film_stage_key(1, "profile-a", &base);
        for (group, patch) in [
            ("camera", serde_json::json!({"exposure_compensation_ev":0.2})),
            ("camera", serde_json::json!({"lens_blur_um":2.0})),
            ("film_render", serde_json::json!({"development_time":1.1})),
            ("film_render", serde_json::json!({"grain":{"active":false}})),
            ("io", serde_json::json!({"input_color_space":"sRGB"})),
        ] {
            assert_ne!(key, film_stage_key(1,"profile-a",&changed(group,patch)), "{group}");
        }
        assert_ne!(key, film_stage_key(2,"profile-a",&base));
        assert_ne!(key, film_stage_key(1,"profile-b",&base));
        let mut scan = base.clone();
        scan.io.scan_film = true;
        let scan_key = film_stage_key(1,"profile-a",&scan);
        scan.scanner.lens_blur = 2.0;
        assert_eq!(scan_key, film_stage_key(1,"profile-a",&scan));
        scan.scanner.white_correction = true;
        assert_eq!(scan_key, film_stage_key(1,"profile-a",&scan));
    }

    #[test]
    fn input_probe_reports_miss_hit_and_refreshes_lru_without_loading() {
        let mut engine = Engine {
            backend: Box::new(spektrafilm_gpu::cpu_backend::CpuBackend),
            inputs: VecDeque::new(), input_cache_max_bytes: 1024,
            pipelines: VecDeque::new(), print_profiles: VecDeque::new(),
            pipeline_cache_max_entries: 4, next_input_generation: 0,
            next_pipeline_generation: 0, reuse_film_stages: true,
        };
        let request = || serde_json::from_value(serde_json::json!({
            "id":1,"command":"probe_input","input_cache_key":"input-a"
        })).unwrap();
        let missing = engine.handle(request()).unwrap();
        assert!(missing.ok);
        assert!(!missing.input_cache_hit);
        assert_eq!(missing.width, None);
        for key in ["input-a", "input-b"] {
            engine.inputs.push_back(CachedInput {
                identity: InputIdentity::Shared(key.into()),
                image: Arc::new(ImageBuf::from_data(2,1,vec![precision::from_f32(0.5);6])),
                bytes: 24, generation: 1, metering: VecDeque::new(),
            });
        }
        let found = engine.handle(request()).unwrap();
        assert!(found.input_cache_hit);
        assert_eq!((found.width, found.height), (Some(2),Some(1)));
        assert_eq!(engine.inputs.back().unwrap().identity, InputIdentity::Shared("input-a".into()));
        engine.inputs.clear();
        assert!(!engine.handle(request()).unwrap().input_cache_hit);
    }
}
