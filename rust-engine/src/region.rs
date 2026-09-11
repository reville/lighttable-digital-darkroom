// SPDX-License-Identifier: GPL-3.0-only
//! Exact viewport planning for the finite-support WGPU film chain.
//!
//! Input and metering remain full frame. Only the image sent through the pixel
//! stages is cropped, with the sum of the sequential kernels' support. Noise
//! uses absolute source coordinates. Diffusion crops align to the full-frame
//! downsample lattice and retain its absolute interpolation coordinates.
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use spektrafilm_core::params::RuntimeParams;
use spektrafilm_math::image::ImageBuf;

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Rect {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

impl Rect {
    pub fn array(self) -> [u32; 4] {
        [self.x, self.y, self.width, self.height]
    }
}

#[derive(Debug, Clone, Copy)]
pub struct RegionPlan {
    /// Expanded input-axis rectangle sent to the pixel stages.
    pub render: Rect,
    /// Input-axis final crop relative to the expanded render, before rotation.
    pub trim: Rect,
    pub accelerated: bool,
}

pub fn plan(
    viewport: Rect,
    width: u32,
    height: u32,
    quarters_ccw: u8,
    params: &RuntimeParams,
    wgpu: bool,
) -> Result<RegionPlan> {
    let (out_width, out_height) = if quarters_ccw % 2 == 0 {
        (width, height)
    } else {
        (height, width)
    };
    if viewport.width == 0
        || viewport.height == 0
        || viewport
            .x
            .checked_add(viewport.width)
            .is_none_or(|end| end > out_width)
        || viewport
            .y
            .checked_add(viewport.height)
            .is_none_or(|end| end > out_height)
    {
        bail!("viewport must be a nonempty rectangle inside {out_width}x{out_height}");
    }
    let v = viewport;
    let source = match quarters_ccw % 4 {
        0 => v,
        1 => Rect {
            x: width - v.y - v.height,
            y: v.x,
            width: v.height,
            height: v.width,
        },
        2 => Rect {
            x: width - v.x - v.width,
            y: height - v.y - v.height,
            width: v.width,
            height: v.height,
        },
        _ => Rect {
            x: v.y,
            y: height - v.x - v.width,
            width: v.height,
            height: v.width,
        },
    };
    let padding = wgpu
        .then(|| kernel_support(params, width, height))
        .flatten();
    let render = if let Some((pad, alignment)) = padding {
        let align_down = |v: u32| v / alignment * alignment;
        let align_up = |v: u32| v.div_ceil(alignment).saturating_mul(alignment);
        let x = align_down(source.x.saturating_sub(pad));
        let y = align_down(source.y.saturating_sub(pad));
        let right = (source.x + source.width).saturating_add(pad).min(width);
        let bottom = (source.y + source.height).saturating_add(pad).min(height);
        Rect {
            x,
            y,
            width: align_up(right).min(width) - x,
            height: align_up(bottom).min(height) - y,
        }
    } else {
        Rect {
            x: 0,
            y: 0,
            width,
            height,
        }
    };
    Ok(RegionPlan {
        render,
        trim: Rect {
            x: source.x - render.x,
            y: source.y - render.y,
            width: source.width,
            height: source.height,
        },
        accelerated: render.width < width || render.height < height,
    })
}

/// Match the WGPU separable FIR support exactly (including its safety cap).
fn blur_radius(sigma: f32) -> u32 {
    (3.0 * sigma.max(0.01)).ceil().min(256.0) as u32
}

fn kernel_support(p: &RuntimeParams, width: u32, height: u32) -> Option<(u32, u32)> {
    if (p.io.upscale_factor - 1.0).abs() > 1e-6 || p.io.crop {
        return None;
    }
    // Exact diffusion uses the full-frame sampled PSF rather than the GPU
    // Gaussian/downsample lattice. Preserve the viewport trim but render the
    // full frame so the preview has the same halos as export.
    if [&p.camera.diffusion_filter, &p.enlarger.diffusion_filter]
        .into_iter()
        .take(if p.io.scan_film { 1 } else { 2 })
        .any(|filter| filter.active && filter.strength > 0.0 && filter.spatial_scale > 0.0)
    {
        return None;
    }
    let pixel_um =
        spektrafilm_core::stages::filming::pixel_size_um(p.camera.film_format_mm, width, height);
    let mut margin = 0;
    let mut alignment = 1;
    for filter in [&p.camera.diffusion_filter, &p.enlarger.diffusion_filter]
        .into_iter()
        .take(if p.io.scan_film { 1 } else { 2 })
    {
        if let Some(plan) = filter.gpu_plan(pixel_um as f64, width, height) {
            // Bilinear interpolation reads two small pixels. Each small pixel
            // reads a radius of small blocks, and each block reads d source
            // pixels. Two extra blocks safely include both resampling stages.
            let radius = plan
                .sigmas
                .iter()
                .map(|&s| blur_radius(s))
                .max()
                .unwrap_or(0);
            margin = u32::checked_add(margin, (radius + 2).checked_mul(plan.d)?)?;
            let mut a = alignment;
            let mut b = plan.d;
            while b != 0 {
                (a, b) = (b, a % b);
            }
            alignment = (alignment / a).checked_mul(plan.d)?;
        }
    }
    if p.camera.lens_blur_um > 0.0 {
        margin += blur_radius(p.camera.lens_blur_um / pixel_um);
    }
    let h = &p.film_render.halation;
    if h.active {
        let avg = |a: [f64; 3]| (a[0] + a[1] + a[2]) / 3.0;
        // Scatter branches are parallel; the bounce blurs read their combined
        // result in parallel. Add each stage's largest radius, not every branch.
        let scatter = (avg(h.scatter_core_um).max(avg(h.scatter_tail_um)) * h.scatter_spatial_scale
            / pixel_um as f64) as f32;
        margin += blur_radius(scatter);
        if h.halation_n_bounces > 0 {
            let first = (avg(h.halation_first_sigma_um) * h.halation_spatial_scale
                / pixel_um as f64) as f32;
            margin += blur_radius(first * (h.halation_n_bounces as f32).sqrt());
        }
    }
    let dir = &p.film_render.dir_couplers;
    if dir.active {
        margin += blur_radius(
            (dir.diffusion_size_um.max(dir.diffusion_tail_um) / pixel_um as f64) as f32,
        );
    }
    let grain = &p.film_render.grain;
    if grain.active && grain.blur > 0.0 {
        margin += blur_radius(grain.blur);
    }
    let glare = if p.io.scan_film {
        &p.film_render.glare
    } else {
        &p.print_render.glare
    };
    // Glare noise is independent of image pixels, but its own blur needs the
    // same absolute-coordinate halo. Adding it is conservative and bounded.
    if glare.active && glare.percent > 0.0 && glare.blur > 0.0 {
        margin += blur_radius(glare.blur);
    }
    if p.scanner.lens_blur > 0.0 {
        margin += blur_radius(p.scanner.lens_blur);
    }
    let [sigma, amount] = p.scanner.unsharp_mask;
    if sigma > 0.0 && amount > 0.0 {
        margin += blur_radius(sigma);
    }
    Some((margin, alignment))
}

pub fn crop_image(image: &ImageBuf, rect: Rect) -> ImageBuf {
    assert!(rect.x + rect.width <= image.width && rect.y + rect.height <= image.height);
    let mut data = Vec::with_capacity(rect.width as usize * rect.height as usize * 3);
    for y in rect.y..rect.y + rect.height {
        let start = (y as usize * image.width as usize + rect.x as usize) * 3;
        data.extend_from_slice(&image.data[start..start + rect.width as usize * 3]);
    }
    ImageBuf::from_data(rect.width, rect.height, data)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn pointwise() -> RuntimeParams {
        let mut p = RuntimeParams::default();
        p.film_render.halation.active = false;
        p.film_render.dir_couplers.active = false;
        p.film_render.grain.active = false;
        p.print_render.glare.active = false;
        p.film_render.glare.active = false;
        p.scanner.unsharp_mask = [0.0, 0.0];
        p
    }
    #[test]
    fn inverse_rotated_viewports_select_the_same_source_pixels() {
        let image = ImageBuf::from_data(6, 4, (0..72).map(|v| v as f32 / 72.0).collect());
        for q in 0..4 {
            let viewport = Rect {
                x: 1,
                y: 1,
                width: 2,
                height: 3,
            };
            let region = plan(viewport, 6, 4, q, &pointwise(), true).unwrap();
            let cropped = crop_image(&image, region.render);
            let (w, _, actual) = crate::rotate_samples(&cropped, q);
            let (full_w, _, full) = crate::rotate_samples(&image, q);
            assert_eq!(w, viewport.width);
            let mut expected = Vec::new();
            for y in viewport.y..viewport.y + viewport.height {
                let start = ((y * full_w + viewport.x) * 3) as usize;
                expected.extend_from_slice(&full[start..start + (viewport.width * 3) as usize]);
            }
            assert_eq!(actual, expected, "rotation {q}");
        }
    }
    #[test]
    fn margin_sums_sequential_kernels_and_clamps_at_frame_edges() {
        let mut p = pointwise();
        p.scanner.lens_blur = 2.0;
        p.scanner.unsharp_mask = [3.0, 1.0];
        let region = plan(
            Rect {
                x: 10,
                y: 20,
                width: 50,
                height: 60,
            },
            200,
            150,
            0,
            &p,
            true,
        )
        .unwrap();
        assert_eq!(
            region.render,
            Rect {
                x: 0,
                y: 5,
                width: 75,
                height: 90
            }
        );
        assert_eq!(
            region.trim,
            Rect {
                x: 10,
                y: 15,
                width: 50,
                height: 60
            }
        );
        assert!(region.accelerated);
    }
    #[test]
    fn unsupported_resampling_and_cpu_use_full_frame_without_changing_crop() {
        let mut p = pointwise();
        let viewport = Rect {
            x: 10,
            y: 20,
            width: 50,
            height: 60,
        };
        for gpu in [false, true] {
            p.io.crop = gpu;
            let region = plan(viewport, 200, 150, 0, &p, gpu).unwrap();
            assert!(!region.accelerated);
            assert_eq!(
                region.render,
                Rect {
                    x: 0,
                    y: 0,
                    width: 200,
                    height: 150
                }
            );
            assert_eq!(region.trim, viewport);
        }
    }
    #[test]
    fn exact_diffusion_keeps_full_frame_and_preserves_requested_trim() {
        let mut p = pointwise();
        p.camera.diffusion_filter.active = true;
        p.camera.diffusion_filter.spatial_scale = 0.15;
        p.enlarger.diffusion_filter.active = true;
        p.enlarger.diffusion_filter.spatial_scale = 0.25;
        let region = plan(
            Rect {
                x: 2711,
                y: 1987,
                width: 200,
                height: 150,
            },
            6000,
            4000,
            0,
            &p,
            true,
        )
        .unwrap();
        assert!(!region.accelerated);
        assert_eq!(region.render, Rect { x: 0, y: 0, width: 6000, height: 4000 });
        assert_eq!(region.trim, Rect { x: 2711, y: 1987, width: 200, height: 150 });
    }
    #[test]
    fn diffusion_with_frame_sized_support_keeps_full_frame() {
        let mut p = pointwise();
        p.camera.diffusion_filter.active = true;
        p.camera.diffusion_filter.spatial_scale = 50.0;
        let viewport = Rect {
            x: 50,
            y: 50,
            width: 100,
            height: 100,
        };
        let region = plan(viewport, 1100, 733, 0, &p, true).unwrap();
        assert!(!region.accelerated);
        assert_eq!(
            region.render,
            Rect {
                x: 0,
                y: 0,
                width: 1100,
                height: 733
            }
        );
        assert_eq!(region.trim, viewport);
    }
    #[test]
    fn invalid_and_overflowing_viewports_fail_before_allocating() {
        for viewport in [
            Rect {
                x: 0,
                y: 0,
                width: 0,
                height: 10,
            },
            Rect {
                x: u32::MAX,
                y: 0,
                width: 2,
                height: 1,
            },
            Rect {
                x: 10,
                y: 10,
                width: 191,
                height: 2,
            },
        ] {
            assert!(plan(viewport, 200, 150, 0, &pointwise(), true).is_err());
        }
    }
}
