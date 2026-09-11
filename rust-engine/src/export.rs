// SPDX-License-Identifier: GPL-3.0-only
use anyhow::{Context, Result, bail};
use image::{ImageBuffer, Rgb, imageops::FilterType};
use rayon::prelude::*;
use serde_json::Value;

const LUMA: [f32; 3] = [0.2126, 0.7152, 0.0722];
// Match the established preview radius when using the new shape controls.
#[allow(clippy::approx_constant)]
const VIGNETTE_RADIUS_SCALE: f32 = 1.4142;
const HSL_BANDS: [(&str, f32); 8] = [
    ("red", 0.0),
    ("orange", 30.0),
    ("yellow", 60.0),
    ("green", 120.0),
    ("aqua", 180.0),
    ("blue", 240.0),
    ("purple", 280.0),
    ("magenta", 320.0),
];

pub(crate) struct ExportImage {
    pub width: u32,
    pub height: u32,
    pub samples: Vec<f32>,
}

fn number(value: &Value, key: &str, default: f32) -> f32 {
    value
        .get(key)
        .and_then(Value::as_f64)
        .map(|number| number as f32)
        .filter(|number| number.is_finite())
        .unwrap_or(default)
}

fn boolean(value: &Value, key: &str, default: bool) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(default)
}

fn point(value: &Value, key: &str, default: [f32; 2]) -> [f32; 2] {
    let Some(values) = value.get(key).and_then(Value::as_array) else {
        return default;
    };
    [
        values
            .first()
            .and_then(Value::as_f64)
            .map(|value| value as f32)
            .unwrap_or(default[0]),
        values
            .get(1)
            .and_then(Value::as_f64)
            .map(|value| value as f32)
            .unwrap_or(default[1]),
    ]
}

fn clamp(value: f32) -> f32 {
    value.clamp(0.0, 1.0)
}

fn luma(pixel: &[f32]) -> f32 {
    pixel[0] * LUMA[0] + pixel[1] * LUMA[1] + pixel[2] * LUMA[2]
}

fn smoothstep(edge0: f32, edge1: f32, value: f32) -> f32 {
    if edge1 <= edge0 {
        return if value >= edge1 { 1.0 } else { 0.0 };
    }
    let t = ((value - edge0) / (edge1 - edge0)).clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

fn rgb_to_hsv(pixel: &[f32]) -> (f32, f32, f32) {
    let maximum = pixel[0].max(pixel[1]).max(pixel[2]);
    let minimum = pixel[0].min(pixel[1]).min(pixel[2]);
    let delta = maximum - minimum;
    let saturation = if maximum > 1e-5 {
        delta / maximum.max(1e-5)
    } else {
        0.0
    };
    if delta <= 1e-5 {
        return (0.0, saturation, maximum);
    }
    let hue = if maximum == pixel[0] {
        ((pixel[1] - pixel[2]) / delta).rem_euclid(6.0)
    } else if maximum == pixel[1] {
        (pixel[2] - pixel[0]) / delta + 2.0
    } else {
        (pixel[0] - pixel[1]) / delta + 4.0
    };
    ((hue * 60.0).rem_euclid(360.0), saturation, maximum)
}

fn hsv_to_rgb(hue: f32, saturation: f32, value: f32) -> [f32; 3] {
    let hue = hue.rem_euclid(360.0) / 60.0;
    let saturation = saturation.clamp(0.0, 1.0);
    let value = value.clamp(0.0, 1.0);
    let sector = hue.floor() as usize % 6;
    let fraction = hue - hue.floor();
    let p = value * (1.0 - saturation);
    let q = value * (1.0 - saturation * fraction);
    let t = value * (1.0 - saturation * (1.0 - fraction));
    match sector {
        0 => [value, t, p],
        1 => [q, value, p],
        2 => [p, value, t],
        3 => [p, q, value],
        4 => [t, p, value],
        _ => [value, p, q],
    }
}

fn sample_channel(samples: &[f32], width: u32, height: u32, x: f32, y: f32, channel: usize) -> f32 {
    let x = x.clamp(0.0, width.saturating_sub(1) as f32);
    let y = y.clamp(0.0, height.saturating_sub(1) as f32);
    let x0 = x.floor() as u32;
    let y0 = y.floor() as u32;
    let x1 = (x0 + 1).min(width - 1);
    let y1 = (y0 + 1).min(height - 1);
    let fx = x - x0 as f32;
    let fy = y - y0 as f32;
    let at = |px: u32, py: u32| -> f32 { samples[((py * width + px) * 3) as usize + channel] };
    (at(x0, y0) * (1.0 - fx) + at(x1, y0) * fx) * (1.0 - fy)
        + (at(x0, y1) * (1.0 - fx) + at(x1, y1) * fx) * fy
}

fn cross_blur(center: &[f32], neighbors: &[f32], width: u32, height: u32, radius: f32) -> Vec<f32> {
    let mut output = vec![0.0_f32; center.len()];
    output
        .par_chunks_mut(3)
        .enumerate()
        .for_each(|(index, pixel)| {
            let x = (index as u32 % width) as f32;
            let y = (index as u32 / width) as f32;
            for channel in 0..3 {
                pixel[channel] = (center[index * 3 + channel]
                    + sample_channel(neighbors, width, height, x + radius, y, channel)
                    + sample_channel(neighbors, width, height, x - radius, y, channel)
                    + sample_channel(neighbors, width, height, x, y + radius, channel)
                    + sample_channel(neighbors, width, height, x, y - radius, channel))
                    / 5.0;
            }
        });
    output
}

fn curve_sample(curve: &Value, value: f32) -> f32 {
    let Some(entries) = curve.as_array() else {
        return value;
    };
    if entries.len() != 256 {
        return value;
    }
    let position = clamp(value) * 255.0;
    let lower = position.floor() as usize;
    let upper = (lower + 1).min(255);
    let fraction = position - lower as f32;
    let read = |index: usize| {
        entries[index]
            .as_f64()
            .map(|value| value as f32)
            .unwrap_or(index as f32 / 255.0)
    };
    read(lower) * (1.0 - fraction) + read(upper) * fraction
}

fn apply_hsl(pixel: &mut [f32], settings: &Value) {
    let (mut hue, mut saturation, mut value) = rgb_to_hsv(pixel);
    let mut hue_shift = 0.0;
    let mut saturation_shift = 0.0;
    let mut luminance_shift = 0.0;
    for (name, centre) in HSL_BANDS {
        let Some(adjustment) = settings.get(name) else {
            continue;
        };
        let difference = ((hue - centre + 180.0).rem_euclid(360.0) - 180.0).abs();
        let mut weight = (1.0 - difference / 45.0).clamp(0.0, 1.0);
        weight = weight * weight * (3.0 - 2.0 * weight) * saturation;
        hue_shift += weight * number(adjustment, "h", 0.0) * 30.0;
        saturation_shift += weight * number(adjustment, "s", 0.0);
        luminance_shift += weight * number(adjustment, "l", 0.0);
    }
    hue = (hue + hue_shift).rem_euclid(360.0);
    saturation = (saturation * (1.0 + saturation_shift)).clamp(0.0, 1.0);
    value = (value * (1.0 + luminance_shift * 0.5)).clamp(0.0, 1.0);
    pixel.copy_from_slice(&hsv_to_rgb(hue, saturation, value));
}

fn apply_monochrome(pixel: &mut [f32], settings: Option<&Value>) {
    let (hue, saturation, _value) = rgb_to_hsv(pixel);
    let mut dl = 0.0;
    if let Some(settings) = settings {
        for (name, centre) in HSL_BANDS {
            let Some(adjustment) = settings.get(name) else {
                continue;
            };
            let difference = ((hue - centre + 180.0).rem_euclid(360.0) - 180.0).abs();
            let mut weight = (1.0 - difference / 45.0).clamp(0.0, 1.0);
            weight = weight * weight * (3.0 - 2.0 * weight) * saturation;
            dl += weight * number(adjustment, "l", 0.0) * 0.5;
        }
    }
    let luma = (pixel[0] * LUMA[0] + pixel[1] * LUMA[1] + pixel[2] * LUMA[2] + dl).clamp(0.0, 1.0);
    pixel[0] = luma;
    pixel[1] = luma;
    pixel[2] = luma;
}

fn apply_point_color(pixel: &mut [f32], points: &Value) {
    let Some(points) = points.as_array() else {
        return;
    };
    let (mut hue, mut saturation, mut value) = rgb_to_hsv(pixel);
    for point in points.iter().take(8) {
        let target_hue = number(point, "hue", 0.0).rem_euclid(360.0);
        let range = number(point, "range", 30.0).clamp(2.0, 90.0);
        let difference = ((hue - target_hue + 180.0).rem_euclid(360.0) - 180.0).abs();
        let weight = (1.0 - smoothstep(range * 0.45, range, difference)) * saturation;
        if point.get("refSaturation").is_some() && point.get("refLuminance").is_some() {
            let shortest = (target_hue - hue + 180.0).rem_euclid(360.0) - 180.0;
            hue = (hue + shortest * number(point, "uniformHue", 0.0) * weight).rem_euclid(360.0);
            saturation = (saturation
                + (number(point, "refSaturation", saturation) - saturation)
                    * number(point, "uniformSaturation", 0.0)
                    * weight)
                .clamp(0.0, 1.0);
            value = (value
                + (number(point, "refLuminance", value) - value)
                    * number(point, "uniformLuminance", 0.0)
                    * weight)
                .clamp(0.0, 1.0);
        }
        hue = (hue + number(point, "hueShift", 0.0) * weight).rem_euclid(360.0);
        saturation =
            (saturation * (1.0 + number(point, "saturation", 0.0) * weight)).clamp(0.0, 1.0);
        value = (value * (1.0 + number(point, "luminance", 0.0) * 0.5 * weight)).clamp(0.0, 1.0);
    }
    pixel.copy_from_slice(&hsv_to_rgb(hue, saturation, value));
}

fn apply_color_grading(pixel: &mut [f32], settings: &Value) {
    let luminance = luma(pixel);
    let balance = number(settings, "balance", 0.0);
    let blending = number(settings, "blending", 0.5);
    let shift = balance * 0.2;
    let width = 0.18 + blending * 0.22;
    let shadows = 1.0 - smoothstep(0.28 + shift - width, 0.28 + shift + width, luminance);
    let highlights = smoothstep(0.72 + shift - width, 0.72 + shift + width, luminance);
    let midtones = (1.0 - shadows - highlights).clamp(0.0, 1.0);
    for (name, weight) in [
        ("shadows", shadows),
        ("midtones", midtones),
        ("highlights", highlights),
    ] {
        let tone = settings.get(name).unwrap_or(&Value::Null);
        let tint = hsv_to_rgb(number(tone, "hue", 0.0), 1.0, 1.0);
        let tint_luma = luma(&tint);
        for channel in 0..3 {
            let chroma = tint[channel] - tint_luma;
            pixel[channel] += (chroma * number(tone, "saturation", 0.0) * 0.28
                + number(tone, "luminance", 0.0) * 0.22)
                * weight;
        }
    }
    let global = settings.get("global").unwrap_or(&Value::Null);
    let tint = hsv_to_rgb(number(global, "hue", 0.0), 1.0, 1.0);
    let tint_luma = luma(&tint);
    for channel in 0..3 {
        pixel[channel] = clamp(
            pixel[channel]
                + (tint[channel] - tint_luma) * number(global, "saturation", 0.0) * 0.2
                + number(global, "luminance", 0.0) * 0.18,
        );
    }
}

pub(crate) fn grade_is_identity(grade: &Value) -> bool {
    // Vignette size and feather only shape a nonzero amount, so they do not
    // affect identity on their own.
    const NUMERIC_DEFAULTS: [(&str, f32); 23] = [
        ("exposure", 0.0),
        ("contrast", 0.0),
        ("highlights", 0.0),
        ("shadows", 0.0),
        ("whites", 0.0),
        ("blacks", 0.0),
        ("temp", 0.0),
        ("tint", 0.0),
        ("vibrance", 0.0),
        ("saturation", 0.0),
        ("texture", 0.0),
        ("clarity", 0.0),
        ("dehaze", 0.0),
        ("vignette", 0.0),
        ("sharpness", 0.0),
        ("sharpenRadius", 1.0),
        ("sharpenDetail", 0.25),
        ("sharpenMasking", 0.0),
        ("luminanceNoise", 0.0),
        ("colorNoise", 0.0),
        ("chromaticAberrationRedCyan", 0.0),
        ("chromaticAberrationBlueYellow", 0.0),
        ("monochrome", 0.0),
    ];
    let numeric = NUMERIC_DEFAULTS
        .iter()
        .all(|(key, default)| (number(grade, key, *default) - default).abs() <= 1e-6)
        && !boolean(grade, "monochrome", false);
    numeric
        && [
            "curveL",
            "curveR",
            "curveG",
            "curveB",
            "hsl",
            "pointColor",
            "colorGrading",
        ]
        .iter()
        .all(|key| grade.get(key).is_none_or(Value::is_null))
}

pub(crate) fn apply_grade(samples: &mut [f32], width: u32, height: u32, grade: &Value) {
    if grade_is_identity(grade) {
        return;
    }
    samples
        .par_iter_mut()
        .for_each(|value| *value = clamp(*value));

    let red_cyan = number(grade, "chromaticAberrationRedCyan", 0.0);
    let blue_yellow = number(grade, "chromaticAberrationBlueYellow", 0.0);
    if red_cyan != 0.0 || blue_yellow != 0.0 {
        let source = samples.to_vec();
        samples
            .par_chunks_mut(3)
            .enumerate()
            .for_each(|(index, pixel)| {
                let x = (index as u32 % width) as f32;
                let y = (index as u32 / width) as f32;
                let nx = x / width.saturating_sub(1).max(1) as f32 - 0.5;
                let ny = y / height.saturating_sub(1).max(1) as f32 - 0.5;
                if red_cyan != 0.0 {
                    pixel[0] = sample_channel(
                        &source,
                        width,
                        height,
                        x + nx * red_cyan * 6.0,
                        y + ny * red_cyan * 6.0,
                        0,
                    );
                }
                if blue_yellow != 0.0 {
                    pixel[2] = sample_channel(
                        &source,
                        width,
                        height,
                        x + nx * blue_yellow * 6.0,
                        y + ny * blue_yellow * 6.0,
                        2,
                    );
                }
            });
    }

    // Python deliberately retains the post-CA source for the neighbor taps in
    // later detail stages. Matching that snapshot avoids cumulative halos.
    let detail_source = samples.to_vec();
    let luminance_noise = number(grade, "luminanceNoise", 0.0);
    let color_noise = number(grade, "colorNoise", 0.0);
    if luminance_noise != 0.0 || color_noise != 0.0 {
        let blur = cross_blur(samples, samples, width, height, 1.0);
        samples
            .par_chunks_mut(3)
            .zip(blur.par_chunks(3))
            .for_each(|(pixel, blurred)| {
                let original_luma = luma(pixel);
                let blurred_luma = luma(blurred);
                if luminance_noise != 0.0 {
                    for channel in pixel.iter_mut() {
                        *channel =
                            clamp(*channel + (blurred_luma - original_luma) * luminance_noise);
                    }
                }
                if color_noise != 0.0 {
                    let current_luma = luma(pixel);
                    for channel in 0..3 {
                        let chroma = pixel[channel] - current_luma;
                        let blur_chroma = blurred[channel] - blurred_luma;
                        pixel[channel] = clamp(
                            current_luma + chroma * (1.0 - color_noise) + blur_chroma * color_noise,
                        );
                    }
                }
            });
    }

    let texture = number(grade, "texture", 0.0);
    let clarity = number(grade, "clarity", 0.0);
    if texture != 0.0 || clarity != 0.0 {
        let blur = cross_blur(samples, &detail_source, width, height, 1.0);
        samples
            .par_chunks_mut(3)
            .zip(blur.par_chunks(3))
            .for_each(|(pixel, blurred)| {
                let detail = [
                    pixel[0] - blurred[0],
                    pixel[1] - blurred[1],
                    pixel[2] - blurred[2],
                ];
                if texture != 0.0 {
                    for channel in 0..3 {
                        pixel[channel] = clamp(pixel[channel] + detail[channel] * texture * 1.1);
                    }
                }
                if clarity != 0.0 {
                    let middle = (1.0 - (luma(pixel) - 0.5).abs() * 2.0).clamp(0.0, 1.0);
                    for channel in 0..3 {
                        pixel[channel] =
                            clamp(pixel[channel] + detail[channel] * clarity * 1.8 * middle);
                    }
                }
            });
    }

    let sharpness = number(grade, "sharpness", 0.0);
    if sharpness != 0.0 {
        let radius = number(grade, "sharpenRadius", 1.0);
        let detail_amount = number(grade, "sharpenDetail", 0.25);
        let masking = number(grade, "sharpenMasking", 0.0);
        let blur = cross_blur(samples, &detail_source, width, height, radius);
        samples
            .par_chunks_mut(3)
            .zip(blur.par_chunks(3))
            .for_each(|(pixel, blurred)| {
                let detail = [
                    pixel[0] - blurred[0],
                    pixel[1] - blurred[1],
                    pixel[2] - blurred[2],
                ];
                let luma_detail = luma(&detail);
                let edge =
                    (detail[0] * detail[0] + detail[1] * detail[1] + detail[2] * detail[2]).sqrt();
                let threshold = smoothstep(0.015, 0.16, edge);
                let mask = 1.0 - masking + threshold * masking;
                for channel in 0..3 {
                    let shaped = luma_detail + (detail[channel] - luma_detail) * detail_amount;
                    pixel[channel] = clamp(pixel[channel] + shaped * sharpness * 1.8 * mask);
                }
            });
    }

    let exposure = number(grade, "exposure", 0.0);
    let highlights = number(grade, "highlights", 0.0);
    let shadows = number(grade, "shadows", 0.0);
    let whites = number(grade, "whites", 0.0);
    let blacks = number(grade, "blacks", 0.0);
    let contrast = number(grade, "contrast", 0.0);
    let dehaze = number(grade, "dehaze", 0.0);
    let temperature = number(grade, "temp", 0.0);
    let tint = number(grade, "tint", 0.0);
    let saturation = number(grade, "saturation", 0.0);
    let vibrance = number(grade, "vibrance", 0.0);
    let hsl = grade.get("hsl");
    let points = grade.get("pointColor");
    let color_grading = grade.get("colorGrading");
    let vignette = number(grade, "vignette", 0.0);
    let vignette_size = number(grade, "vignetteSize", 0.5).clamp(0.0, 1.0);
    let vignette_feather = number(grade, "vignetteFeather", 1.0).clamp(0.0, 1.0);
    let legacy_vignette = vignette_size == 0.5 && vignette_feather == 1.0;
    let vignette_outer = 0.25 + 1.5 * vignette_size;
    let vignette_width = vignette_outer * vignette_feather.max(0.01);

    samples
        .par_chunks_mut(3)
        .enumerate()
        .for_each(|(index, pixel)| {
            for channel in pixel.iter_mut() {
                *channel = if *channel <= 0.04045 {
                    *channel / 12.92
                } else {
                    ((*channel + 0.055) / 1.055).powf(2.4)
                } * 2.0_f32.powf(exposure);
            }
            let linear_luma = luma(pixel).max(0.0);
            if highlights != 0.0 {
                let mask = ((linear_luma - 0.35) / 0.65).clamp(0.0, 1.0).powf(1.2);
                for channel in pixel.iter_mut() {
                    *channel *= 1.0 + highlights * 0.85 * mask;
                }
            }
            if shadows != 0.0 {
                let mask = ((0.45 - linear_luma) / 0.45).clamp(0.0, 1.0).powf(1.2);
                for channel in pixel.iter_mut() {
                    *channel *= 1.0 + shadows * 1.5 * mask;
                }
            }
            for channel in pixel.iter_mut() {
                let linear = channel.max(0.0);
                *channel = clamp(if linear <= 0.0031308 {
                    linear * 12.92
                } else {
                    1.055 * linear.powf(1.0 / 2.4) - 0.055
                });
            }
            if whites != 0.0 || blacks != 0.0 {
                let white = 1.0 + whites * 0.35;
                let black = blacks * -0.25;
                let range = (white - black).max(1e-4);
                for channel in pixel.iter_mut() {
                    *channel = clamp((*channel - black) / range);
                }
            }
            if contrast > 0.0 {
                for channel in pixel.iter_mut() {
                    let smooth = *channel * *channel * (3.0 - 2.0 * *channel);
                    *channel = clamp(*channel + (smooth - *channel) * contrast);
                }
            } else if contrast < 0.0 {
                for channel in pixel.iter_mut() {
                    *channel = clamp(0.5 + (*channel - 0.5) * (1.0 + contrast * 0.8));
                }
            }
            if dehaze != 0.0 {
                let haze = dehaze * 0.12;
                let denominator = (1.0 - haze).max(0.2);
                for channel in pixel.iter_mut() {
                    *channel = clamp((*channel - haze) / denominator);
                }
                let luminance = luma(pixel);
                for channel in pixel.iter_mut() {
                    *channel = clamp(luminance + (*channel - luminance) * (1.0 + dehaze * 0.18));
                }
            }
            if temperature != 0.0 || tint != 0.0 {
                let gains = [
                    1.0 + temperature * 0.18 + tint * 0.06,
                    1.0 - tint * 0.12,
                    1.0 - temperature * 0.18 + tint * 0.06,
                ];
                for channel in 0..3 {
                    pixel[channel] = clamp(pixel[channel] * gains[channel]);
                }
            }
            if saturation != 0.0 {
                let luminance = luma(pixel);
                for channel in pixel.iter_mut() {
                    *channel = clamp(luminance + (*channel - luminance) * (1.0 + saturation));
                }
            }
            if vibrance != 0.0 {
                let maximum = pixel[0].max(pixel[1]).max(pixel[2]);
                let minimum = pixel[0].min(pixel[1]).min(pixel[2]);
                let current_saturation = (maximum - minimum) / maximum.max(1e-4);
                let luminance = luma(pixel);
                for channel in pixel.iter_mut() {
                    *channel = clamp(
                        luminance
                            + (*channel - luminance)
                                * (1.0 + vibrance * (1.0 - current_saturation)),
                    );
                }
            }
            if number(grade, "monochrome", 0.0) > 0.5 || boolean(grade, "monochrome", false) {
                apply_monochrome(pixel, hsl);
            } else if let Some(settings) = hsl {
                apply_hsl(pixel, settings);
            }
            if let Some(settings) = points {
                apply_point_color(pixel, settings);
            }
            if let Some(settings) = color_grading {
                apply_color_grading(pixel, settings);
            }
            if let Some(curve) = grade.get("curveL") {
                for channel in pixel.iter_mut() {
                    *channel = curve_sample(curve, *channel);
                }
            }
            for (channel, key) in ["curveR", "curveG", "curveB"].iter().enumerate() {
                if let Some(curve) = grade.get(key) {
                    pixel[channel] = curve_sample(curve, pixel[channel]);
                }
            }
            if vignette != 0.0 {
                let x = (index as u32 % width) as f32;
                let y = (index as u32 / width) as f32;
                let nx = (x / width.saturating_sub(1).max(1) as f32 - 0.5) * 2.0;
                let ny = (y / height.saturating_sub(1).max(1) as f32 - 0.5) * 2.0;
                let distance = (nx * nx + ny * ny).sqrt();
                let shaped = if legacy_vignette {
                    // Keep the original CPU divisor and unbounded radius at
                    // default settings so existing edits retain their pixels.
                    distance / std::f32::consts::SQRT_2
                } else {
                    ((distance / VIGNETTE_RADIUS_SCALE - vignette_outer + vignette_width)
                        / vignette_width)
                        .clamp(0.0, 1.0)
                };
                let falloff = (1.0 - vignette * 0.9 * shaped.powf(2.2)).clamp(0.0, 2.0);
                for channel in pixel.iter_mut() {
                    *channel = clamp(*channel * falloff);
                }
            }
        });
}

fn decode_base64(text: &str) -> Result<Vec<u8>> {
    let mut output = Vec::with_capacity(text.len() * 3 / 4);
    let mut quartet = [0_u8; 4];
    let mut used = 0;
    for byte in text.bytes().filter(|byte| !byte.is_ascii_whitespace()) {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'=' => 64,
            _ => bail!("invalid base64 mask"),
        };
        quartet[used] = value;
        used += 1;
        if used == 4 {
            output.push((quartet[0] << 2) | (quartet[1] >> 4));
            if quartet[2] != 64 {
                output.push((quartet[1] << 4) | (quartet[2] >> 2));
            }
            if quartet[3] != 64 {
                output.push((quartet[2] << 6) | quartet[3]);
            }
            used = 0;
        }
    }
    if used != 0 {
        bail!("truncated base64 mask");
    }
    Ok(output)
}

fn bitmap_layer(bitmap: &Value, width: u32, height: u32) -> Result<Vec<f32>> {
    let source_width = number(bitmap, "width", 0.0) as u32;
    let source_height = number(bitmap, "height", 0.0) as u32;
    if source_width == 0 || source_height == 0 {
        bail!("invalid bitmap mask dimensions");
    }
    let encoded = bitmap
        .get("data")
        .and_then(Value::as_str)
        .context("missing bitmap mask data")?;
    let decoded = decode_base64(encoded)?;
    let values = if bitmap.get("encoding").and_then(Value::as_str) == Some("png") {
        let image = image::load_from_memory(&decoded)?.to_luma8();
        if image.dimensions() != (source_width, source_height) {
            bail!("bitmap mask PNG dimensions do not match metadata");
        }
        image.into_raw()
    } else {
        if decoded.len() != source_width as usize * source_height as usize {
            bail!("bitmap mask byte count does not match dimensions");
        }
        decoded
    };
    let mut layer = vec![0.0_f32; width as usize * height as usize];
    layer
        .par_iter_mut()
        .enumerate()
        .for_each(|(index, weight)| {
            let x = (index as u32 % width) as f32;
            let y = (index as u32 / width) as f32;
            let sx = ((x + 0.5) * source_width as f32 / width as f32 - 0.5)
                .clamp(0.0, source_width.saturating_sub(1) as f32);
            let sy = ((y + 0.5) * source_height as f32 / height as f32 - 0.5)
                .clamp(0.0, source_height.saturating_sub(1) as f32);
            let x0 = sx.floor() as u32;
            let y0 = sy.floor() as u32;
            let x1 = (x0 + 1).min(source_width - 1);
            let y1 = (y0 + 1).min(source_height - 1);
            let fx = sx - x0 as f32;
            let fy = sy - y0 as f32;
            let at = |px: u32, py: u32| values[(py * source_width + px) as usize] as f32 / 255.0;
            *weight = (at(x0, y0) * (1.0 - fx) + at(x1, y0) * fx) * (1.0 - fy)
                + (at(x0, y1) * (1.0 - fx) + at(x1, y1) * fx) * fy;
        });
    Ok(layer)
}

fn raster_component(component: &Value, width: u32, height: u32) -> Result<Vec<f32>> {
    let kind = component
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("radial");
    let mut layer = match kind {
        "radial" => {
            let center = point(component, "center", [0.5, 0.5]);
            let radius = number(component, "radius", 0.25);
            let feather = number(component, "feather", 0.65);
            let minimum = width.min(height).max(1) as f32;
            (0..width as usize * height as usize)
                .into_par_iter()
                .map(|index| {
                    let x = (index as u32 % width) as f32;
                    let y = (index as u32 / width) as f32;
                    let dx = x - center[0] * width.saturating_sub(1) as f32;
                    let dy = y - center[1] * height.saturating_sub(1) as f32;
                    let normalized = (dx * dx + dy * dy).sqrt() / (radius * minimum).max(1.0);
                    1.0 - smoothstep((1.0 - feather).max(0.0), 1.0, normalized)
                })
                .collect()
        }
        "linear" => {
            let start = point(component, "start", [0.25, 0.5]);
            let end = point(component, "end", [0.75, 0.5]);
            let sx = start[0] * width.saturating_sub(1) as f32;
            let sy = start[1] * height.saturating_sub(1) as f32;
            let dx = end[0] * width.saturating_sub(1) as f32 - sx;
            let dy = end[1] * height.saturating_sub(1) as f32 - sy;
            let denominator = (dx * dx + dy * dy).max(1.0);
            (0..width as usize * height as usize)
                .into_par_iter()
                .map(|index| {
                    let x = (index as u32 % width) as f32;
                    let y = (index as u32 / width) as f32;
                    smoothstep(0.0, 1.0, ((x - sx) * dx + (y - sy) * dy) / denominator)
                })
                .collect()
        }
        "brush" => bail!("brush masks require the Python parity path"),
        _ => bitmap_layer(
            component.get("bitmap").context("missing bitmap mask")?,
            width,
            height,
        )?,
    };
    if boolean(component, "invert", false) {
        layer
            .par_iter_mut()
            .for_each(|weight| *weight = 1.0 - *weight);
    }
    Ok(layer)
}

pub(crate) fn apply_masks(
    samples: &mut [f32],
    width: u32,
    height: u32,
    masks: &Value,
) -> Result<()> {
    let Some(masks) = masks.as_array() else {
        return Ok(());
    };
    for mask in masks {
        if !boolean(mask, "enabled", true) {
            continue;
        }
        let local_grade = mask.get("grade").unwrap_or(&Value::Null);
        if grade_is_identity(local_grade) {
            continue;
        }
        let components = mask
            .get("components")
            .and_then(Value::as_array)
            .context("mask has no components")?;
        let mut weight = vec![0.0_f32; width as usize * height as usize];
        for (index, component) in components.iter().enumerate() {
            let layer = raster_component(component, width, height)?;
            let combine = if index == 0 {
                "add"
            } else {
                component
                    .get("combine")
                    .and_then(Value::as_str)
                    .unwrap_or("add")
            };
            weight.par_iter_mut().zip(layer.par_iter()).for_each(
                |(current, layer)| match combine {
                    "subtract" => *current *= 1.0 - layer,
                    "intersect" => *current = current.min(*layer),
                    _ => *current = current.max(*layer),
                },
            );
        }
        let opacity = number(mask, "opacity", 1.0);
        let invert = boolean(mask, "invert", false);
        let luma_low = number(mask, "lumaLow", 0.0);
        let luma_high = number(mask, "lumaHigh", 1.0);
        let color_hue = mask
            .get("colorHue")
            .and_then(Value::as_f64)
            .map(|value| value as f32);
        let color_range = number(mask, "colorRange", 30.0);
        let color_amount = number(mask, "colorAmount", 1.0);
        weight
            .par_iter_mut()
            .zip(samples.par_chunks(3))
            .for_each(|(weight, pixel)| {
                if invert {
                    *weight = 1.0 - *weight;
                }
                *weight = clamp(*weight * opacity);
                let luminance = luma(pixel);
                let lower = if luma_low <= 0.0 {
                    1.0
                } else {
                    smoothstep(luma_low - 0.04, luma_low + 0.04, luminance)
                };
                let upper = if luma_high >= 1.0 {
                    1.0
                } else {
                    1.0 - smoothstep(luma_high - 0.04, luma_high + 0.04, luminance)
                };
                *weight *= lower * upper;
                if let Some(target_hue) = color_hue.filter(|_| color_amount > 0.0) {
                    let (hue, saturation, _) = rgb_to_hsv(pixel);
                    let difference = ((hue - target_hue + 180.0).rem_euclid(360.0) - 180.0).abs();
                    let selected = (1.0 - smoothstep(color_range * 0.45, color_range, difference))
                        * saturation;
                    *weight *= clamp(1.0 - color_amount * (1.0 - selected));
                }
            });
        let mut adjusted = samples.to_vec();
        apply_grade(&mut adjusted, width, height, local_grade);
        samples
            .par_chunks_mut(3)
            .zip(adjusted.par_chunks(3))
            .zip(weight.par_iter())
            .for_each(|((pixel, adjusted), weight)| {
                for channel in 0..3 {
                    pixel[channel] = pixel[channel] * (1.0 - weight) + adjusted[channel] * weight;
                }
            });
    }
    Ok(())
}

fn apply_crop(image: ExportImage, crop: &Value) -> Result<ExportImage> {
    // Match Python's double-precision coordinates before rounding to pixels.
    // Narrowing to f32 can move a value across a half-pixel boundary.
    let coordinate = |key: &str, default: f64| {
        crop.get(key)
            .and_then(Value::as_f64)
            .unwrap_or(default)
            .clamp(0.0, 1.0)
    };
    let x0 = (coordinate("x", 0.0) * f64::from(image.width)).round_ties_even() as u32;
    let y0 = (coordinate("y", 0.0) * f64::from(image.height)).round_ties_even() as u32;
    let crop_width = (coordinate("w", 1.0) * f64::from(image.width))
        .round_ties_even()
        .max(1.0) as u32;
    let crop_height = (coordinate("h", 1.0) * f64::from(image.height))
        .round_ties_even()
        .max(1.0) as u32;
    let x1 = (x0 + crop_width).min(image.width);
    let y1 = (y0 + crop_height).min(image.height);
    if x1 <= x0 || y1 <= y0 {
        bail!("crop falls outside the image");
    }
    let output_width = x1 - x0;
    let output_height = y1 - y0;
    let mut samples = vec![0.0_f32; output_width as usize * output_height as usize * 3];
    samples
        .par_chunks_mut(output_width as usize * 3)
        .enumerate()
        .for_each(|(row, destination)| {
            let source_start = (((y0 + row as u32) * image.width + x0) * 3) as usize;
            destination.copy_from_slice(
                &image.samples[source_start..source_start + output_width as usize * 3],
            );
        });
    Ok(ExportImage {
        width: output_width,
        height: output_height,
        samples,
    })
}

fn resize(image: ExportImage, long_edge: u32) -> Result<ExportImage> {
    if long_edge == 0 || image.width.max(image.height) <= long_edge {
        return Ok(image);
    }
    let scale = long_edge as f64 / f64::from(image.width.max(image.height));
    let width = (f64::from(image.width) * scale).round_ties_even().max(1.0) as u32;
    let height = (f64::from(image.height) * scale).round_ties_even().max(1.0) as u32;
    let buffer =
        ImageBuffer::<Rgb<f32>, Vec<f32>>::from_raw(image.width, image.height, image.samples)
            .context("invalid RGB export buffer")?;
    let resized = image::imageops::resize(&buffer, width, height, FilterType::Lanczos3);
    Ok(ExportImage {
        width,
        height,
        samples: resized.into_raw(),
    })
}

pub(crate) fn postprocess(
    width: u32,
    height: u32,
    mut samples: Vec<f32>,
    grade: Option<&Value>,
    masks: Option<&Value>,
    crop: Option<&Value>,
    long_edge: Option<u32>,
) -> Result<ExportImage> {
    if let Some(grade) = grade {
        apply_grade(&mut samples, width, height, grade);
    }
    if let Some(masks) = masks {
        apply_masks(&mut samples, width, height, masks)?;
    }
    let mut image = ExportImage {
        width,
        height,
        samples,
    };
    if let Some(crop) = crop.filter(|crop| !crop.is_null()) {
        image = apply_crop(image, crop)?;
    }
    if let Some(long_edge) = long_edge {
        image = resize(image, long_edge)?;
    }
    Ok(image)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn vignette_default_shape_preserves_legacy_pixels() {
        let (width, height) = (17_u32, 13_u32);
        for amount in [-0.7_f32, 0.7] {
            let input: Vec<f32> = (0..width * height * 3)
                .map(|i| 0.15 + (i % 17) as f32 * 0.02)
                .collect();
            let mut implicit = input.clone();
            apply_grade(&mut implicit, width, height, &json!({"vignette": amount}));
            let mut explicit = input.clone();
            apply_grade(
                &mut explicit,
                width,
                height,
                &json!({"vignette": amount, "vignetteSize": 0.5, "vignetteFeather": 1.0}),
            );
            assert_eq!(implicit, explicit);
            for (i, (&source, &actual)) in input.iter().zip(&implicit).enumerate() {
                let x = ((i / 3) as u32 % width) as f32;
                let y = ((i / 3) as u32 / width) as f32;
                let nx = (x / (width - 1) as f32 - 0.5) * 2.0;
                let ny = (y / (height - 1) as f32 - 0.5) * 2.0;
                let radius = (nx * nx + ny * ny).sqrt() / std::f32::consts::SQRT_2;
                let linear = ((source + 0.055) / 1.055).powf(2.4);
                let neutral = clamp(1.055 * linear.powf(1.0 / 2.4) - 0.055);
                let expected = clamp(neutral * (1.0 - amount * 0.9 * radius.powf(2.2)));
                assert_eq!(actual, expected, "legacy vignette changed at sample {i}");
            }
        }
    }

    #[test]
    fn vignette_size_protects_more_of_the_center() {
        let mut small = vec![0.4; 21 * 21 * 3];
        let mut large = small.clone();
        apply_grade(
            &mut small,
            21,
            21,
            &json!({"vignette": 0.8, "vignetteSize": 0.2, "vignetteFeather": 0.5}),
        );
        apply_grade(
            &mut large,
            21,
            21,
            &json!({"vignette": 0.8, "vignetteSize": 0.8, "vignetteFeather": 0.5}),
        );
        let middle = (10 * 21 + 10) * 3;
        let shoulder = (10 * 21 + 17) * 3;
        assert_eq!(small[middle], large[middle]);
        assert!((large[shoulder] - 0.4).abs() < 1e-6);
        assert!(large[shoulder] > small[shoulder] + 0.1);
        assert!(large[0] < large[middle]);
    }

    #[test]
    fn vignette_feather_spreads_the_edge_transition() {
        let mut hard = vec![0.4; 21 * 21 * 3];
        let mut soft = hard.clone();
        apply_grade(
            &mut hard,
            21,
            21,
            &json!({"vignette": 0.8, "vignetteSize": 0.5, "vignetteFeather": 0.0}),
        );
        apply_grade(
            &mut soft,
            21,
            21,
            &json!({"vignette": 0.8, "vignetteSize": 0.5, "vignetteFeather": 1.0}),
        );
        let edge_midpoint = (10 * 21 + 20) * 3;
        assert!((hard[edge_midpoint] - 0.4).abs() < 1e-6);
        assert!(soft[edge_midpoint] < hard[edge_midpoint] - 0.1);
        assert!((hard[0] - soft[0]).abs() < 1e-6);
        assert!(hard.iter().chain(&soft).all(|sample| sample.is_finite()));
    }

    #[test]
    fn vignette_shape_without_amount_keeps_exact_identity() {
        let original = vec![-0.1, 0.3, 1.2, 0.8, 0.0, 1.0];
        for amount in [json!(null), json!(0.0)] {
            let grade = json!({"vignette": amount, "vignetteSize": 0.1, "vignetteFeather": 0.0});
            assert!(grade_is_identity(&grade));
            let mut actual = original.clone();
            apply_grade(&mut actual, 2, 1, &grade);
            assert_eq!(actual, original);
        }
    }

    #[test]
    fn resized_dimensions_match_python_half_even_rounding() {
        for (width, height, expected) in [
            (640, 401, (320, 200)),
            (401, 640, (200, 320)),
            (640, 403, (320, 202)),
            (403, 640, (202, 320)),
        ] {
            let image = postprocess(
                width,
                height,
                vec![0.25; (width * height * 3) as usize],
                None,
                None,
                None,
                Some(320),
            )
            .unwrap();
            assert_eq!((image.width, image.height), expected);
        }
    }

    #[test]
    fn crop_dimensions_retain_json_precision_at_pixel_boundaries() {
        for (width, height, crop, expected) in [
            (1000, 2, json!({"w": 0.50149999}), (501, 2)),
            (2, 1000, json!({"h": 0.50149999}), (2, 501)),
            (1000, 2, json!({"w": 0.50050001}), (501, 2)),
            (2, 1000, json!({"h": 0.50050001}), (2, 501)),
        ] {
            let image = postprocess(
                width,
                height,
                vec![0.25; (width * height * 3) as usize],
                None,
                None,
                Some(&crop),
                None,
            )
            .unwrap();
            assert_eq!((image.width, image.height), expected);
        }
    }

    #[test]
    fn crop_origin_retains_json_precision_at_pixel_boundaries() {
        let pixels = (0..1000).flat_map(|value| [value as f32; 3]).collect();
        let crop = json!({"x": 0.50149999, "y": 0.0, "w": 0.1, "h": 1.0});
        let image = postprocess(1000, 1, pixels, None, None, Some(&crop), None).unwrap();
        assert_eq!(image.samples[0], 501.0);
        let pixels = (0..1000).flat_map(|value| [value as f32; 3]).collect();
        let crop = json!({"x": 0.0, "y": 0.50149999, "w": 1.0, "h": 0.1});
        let image = postprocess(1, 1000, pixels, None, None, Some(&crop), None).unwrap();
        assert_eq!(image.samples[0], 501.0);
    }

    #[test]
    fn exposure_and_crop_are_applied_before_resize() {
        let image = postprocess(
            4,
            2,
            vec![0.25; 4 * 2 * 3],
            Some(&json!({"exposure": 1.0})),
            None,
            Some(&json!({"x": 0.25, "y": 0.0, "w": 0.5, "h": 1.0})),
            Some(1),
        )
        .unwrap();
        assert_eq!((image.width, image.height), (1, 1));
        assert!(image.samples[0] > 0.34);
    }

    #[test]
    fn radial_mask_limits_a_local_grade() {
        let mut samples = vec![0.25; 9 * 9 * 3];
        apply_masks(
            &mut samples,
            9,
            9,
            &json!([{
                "enabled": true,
                "opacity": 1.0,
                "grade": {"exposure": 1.0},
                "components": [{
                    "type": "radial", "center": [0.5, 0.5],
                    "radius": 0.2, "feather": 0.0
                }]
            }]),
        )
        .unwrap();
        assert!(samples[(4 * 9 + 4) * 3] > samples[0] + 0.05);
    }
}
