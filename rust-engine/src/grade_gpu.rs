//! Full-precision display-referred finishing. No RGB8 surface is involved.
use anyhow::{Context, Result, bail};
use serde_json::Value;
use spektrafilm_gpu::ComputeBackend;

fn number(value: &Value, key: &str, default: f32) -> f32 {
    value
        .get(key)
        .and_then(Value::as_f64)
        .filter(|v| v.is_finite())
        .map(|v| v as f32)
        .unwrap_or(default)
}

pub(crate) fn parameters(width: u32, height: u32, grade: &Value) -> Vec<f32> {
    let mut p = vec![0.0; 1204];
    p[0] = width as f32;
    p[1] = height as f32;
    for (i, (key, default)) in [
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
    ]
    .iter()
    .enumerate()
    {
        p[i + 2] = number(grade, key, *default);
    }
    p[24] = if p[2..12].iter().any(|&x| x != 0.0) || p[14] != 0.0 {
        1.0
    } else {
        0.0
    };
    if let Some(hsl) = grade.get("hsl").filter(|v| v.is_object()) {
        p[25] = 1.0;
        for (i, (key, hue)) in [
            ("red", 0.0),
            ("orange", 30.0),
            ("yellow", 60.0),
            ("green", 120.0),
            ("aqua", 180.0),
            ("blue", 240.0),
            ("purple", 280.0),
            ("magenta", 320.0),
        ]
        .iter()
        .enumerate()
        {
            let v = &hsl[key];
            let at = 32 + i * 4;
            p[at] = *hue;
            p[at + 1] = number(v, "h", 0.0);
            p[at + 2] = number(v, "s", 0.0);
            p[at + 3] = number(v, "l", 0.0);
        }
    }
    if let Some(points) = grade.get("pointColor").and_then(Value::as_array) {
        p[26] = points.len().min(8) as f32;
        for (i, point) in points.iter().take(8).enumerate() {
            let at = 64 + i * 12;
            for (j, (key, default)) in [
                ("hue", 0.0),
                ("range", 30.0),
                ("hueShift", 0.0),
                ("saturation", 0.0),
                ("luminance", 0.0),
                ("uniformHue", 0.0),
                ("uniformSaturation", 0.0),
                ("uniformLuminance", 0.0),
                ("refSaturation", 0.0),
                ("refLuminance", 0.0),
            ]
            .iter()
            .enumerate()
            {
                p[at + j] = number(point, key, *default);
            }
            p[at + 10] = if point.get("refSaturation").is_some_and(Value::is_number)
                && point.get("refLuminance").is_some_and(Value::is_number)
            {
                1.0
            } else {
                0.0
            };
        }
    }
    if let Some(color) = grade.get("colorGrading").filter(|v| v.is_object()) {
        p[27] = 1.0;
        for (i, key) in ["shadows", "midtones", "highlights", "global"]
            .iter()
            .enumerate()
        {
            let v = &color[key];
            let at = 160 + i * 4;
            p[at] = number(v, "hue", 0.0);
            p[at + 1] = number(v, "saturation", 0.0);
            p[at + 2] = number(v, "luminance", 0.0);
        }
        p[176] = number(color, "balance", 0.0);
        p[177] = number(color, "blending", 0.5);
    }
    for (i, key) in ["curveL", "curveR", "curveG", "curveB"].iter().enumerate() {
        if let Some(curve) = grade
            .get(key)
            .and_then(Value::as_array)
            .filter(|v| v.len() == 256)
        {
            p[28 + i] = 1.0;
            for (j, value) in curve.iter().enumerate() {
                p[180 + i * 256 + j] = value.as_f64().unwrap_or(j as f64 / 255.0) as f32;
            }
        }
    }
    p
}

pub(crate) fn apply(
    backend: &dyn ComputeBackend,
    input: &[f32],
    width: u32,
    height: u32,
    grade: &Value,
) -> Result<Vec<f32>> {
    if std::env::var("LIGHTTABLE_GPU_COMPUTE").as_deref() == Ok("0") {
        bail!("GPU application compute disabled");
    }
    if width == 0
        || height == 0
        || width > 65535
        || height > 65535
        || input.len() != width as usize * height as usize * 3
    {
        bail!("invalid grade dimensions");
    }
    if number(grade, "chromaticAberrationRedCyan", 0.0) != 0.0
        || number(grade, "chromaticAberrationBlueYellow", 0.0) != 0.0
    {
        bail!("chromatic aberration requires reference coordinate sampling");
    }
    if grade
        .get("pointColor")
        .and_then(Value::as_array)
        .is_some_and(|points| {
            points.iter().any(|point| {
                number(point, "uniformLuminance", 0.0) > 0.0
                    && number(point, "refLuminance", 0.0) > 0.0
                    && point.get("refSaturation").is_some_and(Value::is_number)
            })
        })
    {
        bail!("Point Color luminance uniformity requires reference precision");
    }
    backend
        .try_compute_f32(
            include_str!("grade_gpu.wgsl"),
            input,
            &parameters(width, height, grade),
            input.len(),
            [width.div_ceil(16), height.div_ceil(16), 1],
        )
        .context("GPU float grading is unavailable for this image")
}

#[cfg(test)]
mod tests {
    use super::*;
    use spektrafilm_gpu::cpu_backend::CpuBackend;

    #[test]
    fn unavailable_gpu_and_invalid_shapes_return_to_reference() {
        let grade = serde_json::json!({"exposure": 0.3});
        assert!(apply(&CpuBackend, &[0.1, 0.2, 0.3], 1, 1, &grade).is_err());
        assert!(apply(&CpuBackend, &[0.1], 1, 1, &grade).is_err());
        assert!(apply(&CpuBackend, &[], 0, 0, &grade).is_err());
        assert!(apply(&CpuBackend, &[], u32::MAX, u32::MAX, &grade).is_err());
    }

    #[test]
    fn all_point_references_are_required_for_uniformity() {
        let p = parameters(
            13,
            17,
            &serde_json::json!({"pointColor": [
                {"hue": 30, "refSaturation": 0.5},
                {"hue": 200, "refSaturation": 0.5, "refLuminance": 0.7}
            ]}),
        );
        assert_eq!(p[26], 2.0);
        assert_eq!(p[74], 0.0);
        assert_eq!(p[86], 1.0);
    }
}
