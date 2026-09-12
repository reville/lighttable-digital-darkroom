// SPDX-License-Identifier: GPL-3.0-only
//! Versioned LightTable interpretation of the source before spectral filming.
//! Keep this operation in sync with `film_tuning.prepare_input`. It works on
//! linear ProPhoto input, preserves its luminance, and leaves colors outside
//! the linear-sRGB yellow-green sector untouched.

use anyhow::{Result, bail};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use spektrafilm_math::{image::ImageBuf, precision};

/// Middle grey in linear light; the fixed point of the display expansion.
const MIDDLE_GREY_LINEAR: f64 = 0.18;

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Specification {
    pub version: u32,
    pub green_amount: f64,
    pub input_cctf_decoding: bool,
    /// Power applied to processed (display-referred) sources around middle
    /// grey before filming, so a finished JPEG's compressed highlights and
    /// lifted shadows go back to scene proportions. Zero leaves the input
    /// alone; RAW decodes always send zero.
    #[serde(default)]
    pub display_expansion: f64,
    /// Linear luminance the expansion pivots on. The application measures it
    /// so the film meter's centre-weighted mean is unchanged by the expansion;
    /// middle grey is the fallback.
    #[serde(default = "default_anchor")]
    pub display_expansion_anchor: f64,
}

fn default_anchor() -> f64 {
    MIDDLE_GREY_LINEAR
}

impl Specification {
    pub fn validate(&self) -> Result<()> {
        if self.version != 1 {
            bail!("unsupported LightTable film tuning version");
        }
        if !self.green_amount.is_finite() || !(0.0..1.0).contains(&self.green_amount) {
            bail!("invalid LightTable green amount");
        }
        if !self.display_expansion.is_finite() || !(0.0..=4.0).contains(&self.display_expansion) {
            bail!("invalid LightTable display expansion");
        }
        if !self.display_expansion_anchor.is_finite()
            || !(0.001..=4.0).contains(&self.display_expansion_anchor)
        {
            bail!("invalid LightTable display expansion anchor");
        }
        Ok(())
    }

    /// Absent tuning retains every existing cache key byte for byte.
    pub fn extend_key(&self, original: String) -> String {
        format!(
            "{original}:input_tuning:{}",
            serde_json::to_string(self).expect("validated finite tuning specification")
        )
    }
}

const TO_SRGB: [[f64; 3]; 3] = [
    [
        2.0340626798588866,
        -0.7275927647469631,
        -0.30682606926988176,
    ],
    [
        -0.22868941146731234,
        1.2317680014826924,
        -0.0028926564624774157,
    ],
    [
        -0.008499732546934672,
        -0.15328915068335006,
        1.1615688759268012,
    ],
];
const FROM_SRGB_RED: [f64; 3] = [
    0.5293363376743782,
    0.09831587646200844,
    0.016847881261919936,
];
const PROPHOTO_Y: [f64; 3] = [0.2880402, 0.7118741, 0.0000857];

fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn smooth(value: f64) -> f64 {
    let value = value.clamp(0.0, 1.0);
    value * value * (3.0 - 2.0 * value)
}

fn prepare_pixel(mut linear: [f64; 3], spec: Specification) -> [f64; 3] {
    if spec.input_cctf_decoding {
        for channel in &mut linear {
            // ROMM/ProPhoto transfer function, including its linear toe.
            *channel = if *channel < 0.03125 {
                *channel / 16.0
            } else {
                channel.max(0.0).powf(1.8)
            };
        }
    }
    if spec.display_expansion > 0.0 {
        for channel in &mut linear {
            // Anchored at middle grey, so exposure is unchanged and highlights
            // are free to exceed 1.0. Non-positive values are left alone.
            if *channel > 0.0 {
                let anchor = spec.display_expansion_anchor;
                *channel = anchor * (channel.max(0.0) / anchor).powf(spec.display_expansion);
            }
        }
    }
    let [r, g, b] = TO_SRGB.map(|row| dot(row, linear));
    if !(g > r && r > b && b >= 0.0 && g > 1e-10) {
        return linear;
    }
    let delta = g - b;
    let hue = 120.0 + 60.0 * if delta > 1e-10 { (b - r) / delta } else { 0.0 };
    let saturation = delta / g;
    let weight = smooth((hue - 60.0) / 30.0) * smooth(saturation / 0.15);
    let shift = delta * (120.0 - hue) / 60.0 * spec.green_amount * weight;
    if shift <= 0.0 {
        return linear;
    }
    let mut candidate = std::array::from_fn(|c| linear[c] - shift * FROM_SRGB_RED[c]);
    let before = dot(linear, PROPHOTO_Y);
    let after = dot(candidate, PROPHOTO_Y);
    let gain = if after > 1e-10 { before / after } else { 1.0 };
    for channel in &mut candidate {
        *channel *= gain;
    }
    let difference: [f64; 3] = std::array::from_fn(|c| candidate[c] - linear[c]);
    let blend = (0..3)
        .map(|c| {
            if difference[c] > 1e-10 {
                (1.0 - linear[c]) / difference[c]
            } else {
                1.0
            }
        })
        .fold(1.0_f64, f64::min)
        .clamp(0.0, 1.0);
    std::array::from_fn(|c| linear[c] + blend * difference[c])
}

pub fn prepare_input(source: &ImageBuf, spec: Specification) -> Result<ImageBuf> {
    spec.validate()?;
    if !source
        .data
        .par_iter()
        .all(|&value| precision::to_f64(value).is_finite())
    {
        bail!("film tuning requires finite input");
    }
    let mut output = ImageBuf::new(source.width, source.height);
    output
        .data
        .par_chunks_exact_mut(3)
        .zip(source.data.par_chunks_exact(3))
        .for_each(|(out, pixel)| {
            let result = prepare_pixel(
                std::array::from_fn(|c| {
                    // Python's input is explicitly float32, even in precision-f64 builds.
                    f64::from(precision::to_f32(pixel[c]))
                }),
                spec,
            );
            for c in 0..3 {
                out[c] = precision::from_f32(result[c] as f32);
            }
        });
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> Specification {
        Specification {
            version: 1,
            green_amount: 0.9,
            input_cctf_decoding: false,
            display_expansion: 0.0,
            display_expansion_anchor: MIDDLE_GREY_LINEAR,
        }
    }

    #[test]
    fn display_expansion_holds_grey_and_frees_highlights() {
        // Values captured from film_tuning.prepare_input with expansion 1.8,
        // green amount 0, on float32 sources.
        let expanded = Specification {
            green_amount: 0.0,
            display_expansion: 1.8,
            ..spec()
        };
        let cases: [([f32; 3], [f64; 3]); 4] = [
            ([0.18, 0.18, 0.18], [0.18000000715255737; 3]),
            ([1.0, 0.5, 0.05], [3.9425933361053467, 1.1322126388549805, 0.01794436201453209]),
            ([0.0, -0.01, 0.02], [0.0, -0.009999999776482582, 0.003448545467108488]),
            ([0.36, 0.09, 0.72], [0.6267964243888855, 0.05169142782688141, 2.1826319694519043]),
        ];
        for (pixel, expected) in cases {
            // prepare_input feeds prepare_pixel float32-rounded values.
            let actual = prepare_pixel(pixel.map(f64::from), expanded);
            for c in 0..3 {
                assert!((actual[c] - expected[c]).abs() < 1e-6, "{pixel:?} -> {actual:?}");
            }
        }
        assert!(Specification { display_expansion: 4.5, ..spec() }.validate().is_err());
        assert!(Specification { display_expansion: -0.1, ..spec() }.validate().is_err());
        let absent: Specification = serde_json::from_str(
            r#"{"version":1,"green_amount":0.8,"input_cctf_decoding":true}"#,
        )
        .unwrap();
        assert_eq!(absent.display_expansion, 0.0);
        assert_eq!(absent.display_expansion_anchor, MIDDLE_GREY_LINEAR);
        // The anchor is the fixed point of the expansion whatever its value.
        let anchored = Specification { green_amount: 0.0, display_expansion: 1.8, display_expansion_anchor: 0.42, ..spec() };
        let held = prepare_pixel([0.42, 0.42, 0.42], anchored);
        assert!(held.iter().all(|v| (v - 0.42).abs() < 1e-12));
        assert!(Specification { display_expansion_anchor: 0.0, ..spec() }.validate().is_err());
    }

    #[test]
    fn python_reference_pixels_match_for_linear_and_romm_inputs() {
        // Values captured from film_tuning.prepare_input on float32 sources.
        let pixels = [
            [0.22, 0.32, 0.07],
            [0.98, 0.99, 0.55],
            [0.18, 0.18, 0.18],
            [0.45, 0.2, 0.08],
            [0.05, 0.2, 0.35],
            [-0.01, 0.1, 0.05],
            [0.03125, 0.02, 0.01],
        ];
        let expected_linear = [
            [0.16070108115673065, 0.3439929485321045, 0.07601317018270493],
            [0.9800000190734863, 0.9900000095367432, 0.550000011920929],
            [0.18000000715255737; 3],
            [
                0.44999998807907104,
                0.20000000298023224,
                0.07999999821186066,
            ],
            [0.05000000074505806, 0.20000000298023224, 0.3499999940395355],
            [
                -0.009999999776482582,
                0.10000000149011612,
                0.05000000074505806,
            ],
            [0.03125, 0.019999999552965164, 0.009999999776482582],
        ];
        let expected_encoded = [
            [
                0.06551802903413773,
                0.1286085844039917,
                0.008340200409293175,
            ],
            [0.9642884135246277, 0.9820720553398132, 0.3409202992916107],
            [0.045655231922864914; 3],
            [0.2375650256872177, 0.055189188569784164, 0.0106062525883317],
            [
                0.004551410675048828,
                0.055189188569784164,
                0.15112018585205078,
            ],
            [
                -0.0006249999860301614,
                0.015848932787775993,
                0.004551410675048828,
            ],
            [0.001953125, 0.0012499999720603228, 0.0006249999860301614],
        ];
        let source = ImageBuf::from_data(
            7,
            1,
            pixels
                .into_iter()
                .flatten()
                .map(precision::from_f32)
                .collect(),
        );
        for (decode, expected) in [(false, expected_linear), (true, expected_encoded)] {
            let mut specification = spec();
            specification.input_cctf_decoding = decode;
            let actual = prepare_input(&source, specification).unwrap();
            for (&actual, expected) in actual.data.iter().zip(expected.into_iter().flatten()) {
                assert!((precision::to_f64(actual) - expected).abs() < 1e-7);
            }
        }
        assert_eq!(precision::to_f32(source.data[0]), 0.22);
    }

    #[test]
    fn green_adjustment_preserves_luminance_and_stays_in_gamut() {
        let source = [0.22, 0.32, 0.07];
        for amount in [0.0, 0.55, 0.8, 0.9, 0.999] {
            for exposure in [0.1, 1.0, 2.0, 3.0] {
                let pixel = source.map(|value| value * exposure);
                let tuned = prepare_pixel(
                    pixel,
                    Specification {
                        green_amount: amount,
                        ..spec()
                    },
                );
                assert!((dot(pixel, PROPHOTO_Y) - dot(tuned, PROPHOTO_Y)).abs() < 1e-12);
                assert!(tuned.iter().all(|&value| (0.0..=1.0).contains(&value)));
                assert!(tuned[0] <= pixel[0]);
                assert!(tuned[1] >= pixel[1]);
            }
        }
    }

    #[test]
    fn zero_amount_and_outside_sector_are_exact_noops() {
        for pixel in [
            [0.18; 3],
            [0.45, 0.2, 0.08],
            [0.05, 0.2, 0.35],
            [0.5, 0.5, 0.03],
            [-0.01, 0.1, 0.05],
        ] {
            assert_eq!(prepare_pixel(pixel, spec()), pixel);
        }
        let pixel = [0.22, 0.32, 0.07];
        assert_eq!(
            prepare_pixel(
                pixel,
                Specification {
                    green_amount: 0.0,
                    ..spec()
                }
            ),
            pixel
        );
    }

    #[test]
    fn invalid_specifications_and_pixels_are_rejected() {
        for amount in [-0.1, 1.0, f64::NAN, f64::INFINITY] {
            assert!(
                Specification {
                    green_amount: amount,
                    ..spec()
                }
                .validate()
                .is_err()
            );
        }
        assert!(
            Specification {
                version: 2,
                ..spec()
            }
            .validate()
            .is_err()
        );
        let source = ImageBuf::from_data(1, 1, vec![precision::from_f32(f32::NAN); 3]);
        assert!(prepare_input(&source, spec()).is_err());
    }
}
