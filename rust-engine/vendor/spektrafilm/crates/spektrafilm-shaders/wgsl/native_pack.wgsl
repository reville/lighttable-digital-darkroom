// Last display-only pass: rotation, ties-to-even RGBA8 packing and one
// mean partial per workgroup. No full float frame returns to the CPU.
struct Params {
    width: u32, height: u32, row_words: u32, rotation: u32,
    crop_x: u32, crop_y: u32, crop_width: u32, crop_height: u32,
    dispatch_width: u32, _pad0: u32, _pad1: u32, _pad2: u32,
}
@group(0) @binding(0) var<uniform> params: Params;
@group(0) @binding(1) var<storage, read> rgb: array<f32>;
@group(0) @binding(2) var<storage, read_write> packed: array<u32>;
var<workgroup> sums: array<f32, 256>;
fn byte(value: f32) -> u32 {
    let scaled = clamp(value, 0.0, 1.0) * 255.0;
    let lower = u32(floor(scaled));
    let fraction = scaled - f32(lower);
    return lower + select(0u, 1u, fraction > 0.5 || (fraction == 0.5 && (lower & 1u) != 0u));
}
@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid: vec3<u32>,
        @builtin(local_invocation_id) lid: vec3<u32>,
        @builtin(workgroup_id) group: vec3<u32>) {
    let count = params.width * params.height;
    let group_index = group.y * params.dispatch_width + group.x;
    let index = group_index * 256u + lid.x;
    var sum = 0.0;
    let output_height = select(params.crop_height, params.crop_width, (params.rotation & 1u) != 0u);
    if index < count {
        let source_x = index % params.width;
        let source_y = index / params.width;
        if source_x >= params.crop_x && source_x < params.crop_x + params.crop_width &&
            source_y >= params.crop_y && source_y < params.crop_y + params.crop_height {
        let x = source_x - params.crop_x;
        let y = source_y - params.crop_y;
        let r = rgb[index * 3u];
        let g = rgb[index * 3u + 1u];
        let b = rgb[index * 3u + 2u];
        var destination = vec2<u32>(x, y);
        if params.rotation == 1u { destination = vec2<u32>(y, params.crop_width - 1u - x); }
        if params.rotation == 2u { destination = vec2<u32>(params.crop_width - 1u - x, params.crop_height - 1u - y); }
        if params.rotation == 3u { destination = vec2<u32>(params.crop_height - 1u - y, x); }
        packed[destination.y * params.row_words + destination.x] =
            byte(r) | (byte(g) << 8u) | (byte(b) << 16u) | 0xff000000u;
        sum = r + g + b;
        }
    }
    sums[lid.x] = sum;
    workgroupBarrier();
    for (var stride = 128u; stride > 0u; stride /= 2u) {
        if lid.x < stride { sums[lid.x] += sums[lid.x + stride]; }
        workgroupBarrier();
    }
    if lid.x == 0u && group_index < (count + 255u) / 256u {
        packed[params.row_words * output_height + group_index] = bitcast<u32>(sums[0]);
    }
}
