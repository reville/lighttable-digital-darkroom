// SPDX-License-Identifier: GPL-3.0-only
// Float32 counterpart of grade.apply. The detail taps always read the
// post-chromatic-aberration source, not an already sharpened neighbor.
// Chromatic-aberration geometry is completed by the precise CPU path first.
@group(0) @binding(0) var<storage, read> image: array<f32>;
@group(0) @binding(1) var<storage, read> p: array<f32>;
@group(0) @binding(2) var<storage, read_write> output: array<f32>;
const LUMA = vec3<f32>(0.2126, 0.7152, 0.0722);
fn bounded(c: vec3<f32>) -> vec3<f32> { return clamp(c, vec3<f32>(0.0), vec3<f32>(1.0)); }
fn modulo(x: f32, divisor: f32) -> f32 { return x - floor(x / divisor) * divisor; }
fn at(x: u32, y: u32) -> vec3<f32> {
    let i = (y * u32(p[0]) + x) * 3u;
    return bounded(vec3<f32>(image[i], image[i + 1u], image[i + 2u]));
}
fn sample_image(coordinate: vec2<f32>) -> vec3<f32> {
    let xy = clamp(coordinate, vec2<f32>(0.0), vec2<f32>(p[0] - 1.0, p[1] - 1.0));
    let a = vec2<u32>(floor(xy));
    let b = min(a + vec2<u32>(1u), vec2<u32>(u32(p[0]) - 1u, u32(p[1]) - 1u));
    let f = fract(xy);
    return mix(mix(at(a.x, a.y), at(b.x, a.y), f.x),
        mix(at(a.x, b.y), at(b.x, b.y), f.x), f.y);
}
fn blur(center: vec3<f32>, xy: vec2<f32>, radius: f32) -> vec3<f32> {
    return (center + sample_image(xy + vec2<f32>(radius, 0.0))
        + sample_image(xy - vec2<f32>(radius, 0.0))
        + sample_image(xy + vec2<f32>(0.0, radius))
        + sample_image(xy - vec2<f32>(0.0, radius))) / 5.0;
}
fn rgb_to_hsv(c: vec3<f32>) -> vec3<f32> {
    let hi = max(c.r, max(c.g, c.b)); let lo = min(c.r, min(c.g, c.b));
    let delta = hi - lo;
    let saturation = select(0.0, delta / max(hi, 1e-5), hi > 1e-5);
    if delta <= 1e-5 { return vec3<f32>(0.0, saturation, hi); }
    var hue: f32;
    if hi == c.r { hue = modulo((c.g - c.b) / delta, 6.0); }
    else if hi == c.g { hue = (c.b - c.r) / delta + 2.0; }
    else { hue = (c.r - c.g) / delta + 4.0; }
    return vec3<f32>(modulo(hue * 60.0, 360.0), saturation, hi);
}
fn hsv_to_rgb(hsv: vec3<f32>) -> vec3<f32> {
    let h = modulo(hsv.x, 360.0) / 60.0;
    let s = clamp(hsv.y, 0.0, 1.0); let v = clamp(hsv.z, 0.0, 1.0);
    let f = fract(h); let low = v * (1.0 - s);
    let q = v * (1.0 - s * f); let t = v * (1.0 - s * (1.0 - f));
    switch u32(floor(h)) % 6u {
        case 0u: { return vec3<f32>(v, t, low); }
        case 1u: { return vec3<f32>(q, v, low); }
        case 2u: { return vec3<f32>(low, v, t); }
        case 3u: { return vec3<f32>(low, q, v); }
        case 4u: { return vec3<f32>(t, low, v); }
        default: { return vec3<f32>(v, low, q); }
    }
}
fn curve(value: f32, table: u32) -> f32 {
    let position = clamp(value, 0.0, 1.0) * 255.0;
    let lower = u32(floor(position)); let upper = min(lower + 1u, 255u);
    return mix(p[180u + table * 256u + lower], p[180u + table * 256u + upper], fract(position));
}
@compute @workgroup_size(16, 16)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if id.x >= u32(p[0]) || id.y >= u32(p[1]) { return; }
    let xy = vec2<f32>(id.xy);
    var c = at(id.x, id.y);
    if p[20] != 0.0 || p[21] != 0.0 {
        let blurred = blur(c, xy, 1.0);
        let by = dot(blurred, LUMA); var y = dot(c, LUMA);
        if p[20] != 0.0 { c = bounded(c + vec3<f32>((by - y) * p[20])); y = dot(c, LUMA); }
        if p[21] != 0.0 {
            c = bounded(vec3<f32>(y) + (c - vec3<f32>(y)) * (1.0 - p[21])
                + (blurred - vec3<f32>(by)) * p[21]);
        }
    }
    if p[12] != 0.0 || p[13] != 0.0 {
        let detail = c - blur(c, xy, 1.0);
        if p[12] != 0.0 { c = bounded(c + detail * p[12] * 1.1); }
        if p[13] != 0.0 {
            let mid = clamp(1.0 - abs(dot(c, LUMA) - 0.5) * 2.0, 0.0, 1.0);
            c = bounded(c + detail * p[13] * 1.8 * mid);
        }
    }
    if p[16] != 0.0 {
        let detail = c - blur(c, xy, p[17]); let y = vec3<f32>(dot(detail, LUMA));
        let shaped = y + (detail - y) * p[18];
        let mask = 1.0 - p[19] + smoothstep(0.015, 0.16, length(detail)) * p[19];
        c = bounded(c + shaped * p[16] * 1.8 * mask);
    }
    if p[24] != 0.0 {
        var linear = select(pow((c + vec3<f32>(0.055)) / 1.055, vec3<f32>(2.4)), c / 12.92,
            c <= vec3<f32>(0.04045)) * exp2(p[2]);
        let y = max(dot(linear, LUMA), 0.0);
        if p[4] != 0.0 { linear *= 1.0 + p[4] * 0.85 * pow(clamp((y - 0.35) / 0.65, 0.0, 1.0), 1.2); }
        if p[5] != 0.0 { linear *= 1.0 + p[5] * 1.5 * pow(clamp((0.45 - y) / 0.45, 0.0, 1.0), 1.2); }
        linear = max(linear, vec3<f32>(0.0));
        c = bounded(select(1.055 * pow(linear, vec3<f32>(1.0 / 2.4)) - vec3<f32>(0.055),
            linear * 12.92, linear <= vec3<f32>(0.0031308)));
        if p[6] != 0.0 || p[7] != 0.0 {
            let white = 1.0 + p[6] * 0.35; let black = p[7] * -0.25;
            c = bounded((c - vec3<f32>(black)) / max(white - black, 1e-4));
        }
        if p[3] > 0.0 { c = bounded(c + (c * c * (vec3<f32>(3.0) - 2.0 * c) - c) * p[3]); }
        else if p[3] < 0.0 { c = bounded(vec3<f32>(0.5) + (c - vec3<f32>(0.5)) * (1.0 + p[3] * 0.8)); }
        if p[14] != 0.0 {
            let haze = p[14] * 0.12;
            c = bounded((c - vec3<f32>(haze)) / max(1.0 - haze, 0.2));
            let l = vec3<f32>(dot(c, LUMA)); c = bounded(l + (c - l) * (1.0 + p[14] * 0.18));
        }
        if p[8] != 0.0 || p[9] != 0.0 {
            c = bounded(c * vec3<f32>(1.0 + p[8] * 0.18 + p[9] * 0.06,
                1.0 - p[9] * 0.12, 1.0 - p[8] * 0.18 + p[9] * 0.06));
        }
        if p[11] != 0.0 { let l = vec3<f32>(dot(c, LUMA)); c = bounded(l + (c - l) * (1.0 + p[11])); }
        if p[10] != 0.0 {
            let hi = max(c.r, max(c.g, c.b)); let lo = min(c.r, min(c.g, c.b));
            let saturation = (hi - lo) / max(hi, 1e-4); let l = vec3<f32>(dot(c, LUMA));
            c = bounded(l + (c - l) * (1.0 + p[10] * (1.0 - saturation)));
        }
    }
    if p[1204] > 0.5 {
        let hsv = rgb_to_hsv(c);
        var delta = 0.0;
        for (var i = 0u; i < 8u; i++) {
            let at = 32u + i * 4u;
            let difference = abs(modulo(hsv.x - p[at] + 180.0, 360.0) - 180.0);
            var weight = clamp(1.0 - difference / 45.0, 0.0, 1.0);
            weight = weight * weight * (3.0 - 2.0 * weight) * hsv.y;
            delta += weight * p[at + 3u] * 0.5;
        }
        c = bounded(vec3<f32>(dot(c, LUMA) + delta));
    } else if p[25] != 0.0 {
        var hsv = rgb_to_hsv(c); var delta = vec3<f32>(0.0);
        for (var i = 0u; i < 8u; i++) {
            let at = 32u + i * 4u;
            let difference = abs(modulo(hsv.x - p[at] + 180.0, 360.0) - 180.0);
            var weight = clamp(1.0 - difference / 45.0, 0.0, 1.0);
            weight = weight * weight * (3.0 - 2.0 * weight) * hsv.y;
            delta += weight * vec3<f32>(p[at + 1u] * 30.0, p[at + 2u], p[at + 3u]);
        }
        c = hsv_to_rgb(vec3<f32>(hsv.x + delta.x, hsv.y * (1.0 + delta.y), hsv.z * (1.0 + delta.z * 0.5)));
    }
    if p[26] != 0.0 {
        var hsv = rgb_to_hsv(c);
        for (var i = 0u; i < u32(p[26]); i++) {
            let at = 64u + i * 12u;
            let difference = abs(modulo(hsv.x - p[at] + 180.0, 360.0) - 180.0);
            let weight = (1.0 - smoothstep(p[at + 1u] * 0.45, p[at + 1u], difference)) * hsv.y;
            if p[at + 10u] != 0.0 {
                let shortest = modulo(p[at] - hsv.x + 180.0, 360.0) - 180.0;
                hsv.x = modulo(hsv.x + shortest * p[at + 5u] * weight, 360.0);
                hsv.y = clamp(hsv.y + (p[at + 8u] - hsv.y) * p[at + 6u] * weight, 0.0, 1.0);
                hsv.z = clamp(hsv.z + (p[at + 9u] - hsv.z) * p[at + 7u] * weight, 0.0, 1.0);
            }
            hsv.x = modulo(hsv.x + p[at + 2u] * weight, 360.0);
            hsv.y = clamp(hsv.y * (1.0 + p[at + 3u] * weight), 0.0, 1.0);
            hsv.z = clamp(hsv.z * (1.0 + p[at + 4u] * 0.5 * weight), 0.0, 1.0);
        }
        c = hsv_to_rgb(hsv);
    }
    if p[27] != 0.0 {
        let y = dot(c, LUMA); let shift = p[176] * 0.2; let spread = 0.18 + p[177] * 0.22;
        let shadows = 1.0 - smoothstep(0.28 + shift - spread, 0.28 + shift + spread, y);
        let highlights = smoothstep(0.72 + shift - spread, 0.72 + shift + spread, y);
        let weights = vec4<f32>(shadows, clamp(1.0 - shadows - highlights, 0.0, 1.0), highlights, 1.0);
        for (var i = 0u; i < 4u; i++) {
            let at = 160u + i * 4u; let tint = hsv_to_rgb(vec3<f32>(p[at], 1.0, 1.0));
            let chroma = tint - vec3<f32>(dot(tint, LUMA));
            c += (chroma * p[at + 1u] * select(0.28, 0.2, i == 3u)
                + vec3<f32>(p[at + 2u] * select(0.22, 0.18, i == 3u))) * weights[i];
        }
        c = bounded(c);
    }
    if p[28] != 0.0 { c = vec3<f32>(curve(c.r, 0u), curve(c.g, 0u), curve(c.b, 0u)); }
    if p[29] != 0.0 { c.r = curve(c.r, 1u); }
    if p[30] != 0.0 { c.g = curve(c.g, 2u); }
    if p[31] != 0.0 { c.b = curve(c.b, 3u); }
    if p[15] != 0.0 {
        let n = (xy / max(vec2<f32>(p[0] - 1.0, p[1] - 1.0), vec2<f32>(1.0)) - vec2<f32>(0.5)) * 2.0;
        let radius = length(n) / 1.4142;
        var shaped = radius;
        if p[178] != 0.5 || p[179] != 1.0 {
            let outer = 0.25 + 1.5 * p[178];
            let width = outer * max(p[179], 0.01);
            shaped = clamp((radius - outer + width) / width, 0.0, 1.0);
        }
        c = bounded(c * clamp(1.0 - p[15] * 0.9 * pow(shaped, 2.2), 0.0, 2.0));
    }
    let index = (id.y * u32(p[0]) + id.x) * 3u;
    output[index] = c.r; output[index + 1u] = c.g; output[index + 2u] = c.b;
}
