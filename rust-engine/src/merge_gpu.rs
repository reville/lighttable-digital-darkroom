// SPDX-License-Identifier: GPL-3.0-only
//! Float32 merge stages. Alignment stays on the established bounded CPU path.
use anyhow::{Context, Result, bail};
use spektrafilm_gpu::ComputeBackend;

const HDR: &str = include_str!("shaders/merge_hdr.wgsl");
const WARP: &str = include_str!("shaders/merge_warp.wgsl");
const FILTER: &str = include_str!("shaders/merge_filter.wgsl");

fn dimension(p: &[f32], index: usize) -> Result<usize> {
    let value = *p.get(index).context("missing merge dimension")?;
    if !value.is_finite() || value < 1.0 || value > 120_000_000.0 || value.fract() != 0.0 {
        bail!("invalid merge dimension");
    }
    Ok(value as usize)
}
fn groups(width: usize, height: usize) -> [u32; 3] {
    [width.div_ceil(16) as u32, height.div_ceil(16) as u32, 1]
}
fn stage(
    op: f32,
    w: usize,
    h: usize,
    channels: usize,
    sigma: f32,
    stride: usize,
    gray: bool,
) -> (Vec<f32>, usize, [u32; 3]) {
    let ow = w.div_ceil(stride);
    let oh = h.div_ceil(stride);
    let oc = if gray { 1 } else { channels };
    (
        vec![
            op,
            w as f32,
            h as f32,
            channels as f32,
            sigma,
            stride as f32,
            gray as u8 as f32,
            ow as f32,
            oh as f32,
        ],
        ow * oh * oc,
        groups(ow, oh),
    )
}

pub fn compute(
    backend: &dyn ComputeBackend,
    operation: &str,
    input: &[f32],
    p: &[f32],
) -> Result<Vec<f32>> {
    if p.iter().any(|v| !v.is_finite()) {
        bail!("non-finite merge parameters");
    }
    let w = dimension(p, 0)?;
    let h = dimension(p, 1)?;
    let pixels = w.checked_mul(h).context("merge dimensions overflow")?;
    if pixels > 120_000_000 {
        bail!("merge tile is too large");
    }
    let result = match operation {
        "merge_hdr_fuse" => {
            let frames = dimension(p, 2)?;
            if !(2..=9).contains(&frames) || input.len() != pixels * frames * 4 {
                bail!("invalid HDR stack");
            }
            backend.try_compute_f32(HDR, input, p, pixels * 3, groups(w, h))
        }
        "merge_panorama_warp" | "merge_hdr_shift" => {
            let ow = dimension(p, 2)?;
            let oh = dimension(p, 3)?;
            if input.len() != pixels * 3
                || p.len()
                    != (if operation == "merge_hdr_shift" {
                        13
                    } else {
                        17
                    })
                || ow * oh > 120_000_000
            {
                bail!("invalid merge warp");
            }
            let mut params = p.to_vec();
            if operation == "merge_hdr_shift" {
                params.extend_from_slice(&[w as f32, h as f32, 0.0, 0.0]);
            }
            params.push(if operation == "merge_hdr_shift" {
                1.0
            } else {
                0.0
            });
            backend.try_compute_f32(WARP, input, &params, ow * oh * 4, groups(ow, oh))
        }
        "merge_focus_sharpness" => {
            if input.len() != pixels * 3 {
                bail!("invalid focus RGB input");
            }
            let stages = [
                stage(0.0, w, h, 3, 1.0, 1, true),
                stage(1.0, w, h, 1, 1.0, 1, false),
                stage(2.0, w, h, 1, 0.0, 1, false),
                stage(0.0, w, h, 1, 2.5, 1, false),
                stage(1.0, w, h, 1, 2.5, 1, false),
            ];
            backend.try_compute_chain_f32(FILTER, input, &stages)
        }
        "merge_pyramid_down" => {
            let channels = dimension(p, 2)?;
            if ![1, 3].contains(&channels) || input.len() != pixels * channels {
                bail!("invalid pyramid input");
            }
            let stages = [
                stage(0.0, w, h, channels, 1.0, 1, false),
                stage(1.0, w, h, channels, 1.0, 2, false),
            ];
            backend.try_compute_chain_f32(FILTER, input, &stages)
        }
        "merge_resize" => {
            let channels = dimension(p, 2)?;
            let ow = dimension(p, 3)?;
            let oh = dimension(p, 4)?;
            if ![1, 3].contains(&channels)
                || input.len() != pixels * channels
                || ow * oh > 120_000_000
            {
                bail!("invalid resize input");
            }
            if p.len() != 7 || p[5] <= 0.0 {
                bail!("invalid resize sampling");
            }
            let params = [
                3.0,
                w as f32,
                h as f32,
                channels as f32,
                0.0,
                1.0,
                0.0,
                ow as f32,
                oh as f32,
                p[5],
                p[6],
            ];
            backend.try_compute_f32(FILTER, input, &params, ow * oh * channels, groups(ow, oh))
        }
        _ => bail!("unknown merge operation"),
    };
    result.context("GPU merge compute unavailable or exceeds device limits")
}
