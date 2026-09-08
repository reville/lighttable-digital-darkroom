// Reduce a flat f32 buffer to per-workgroup maxima.
// Each workgroup covers 2048 input values: 256 threads x 8 values/thread.

struct Params {
    n_values: u32,
    _pad: vec3<u32>,
}

@group(0) @binding(0) var<uniform> params: Params;
@group(0) @binding(1) var<storage, read> input: array<f32>;
@group(0) @binding(2) var<storage, read_write> output: array<f32>;

var<workgroup> partial: array<f32, 256>;

@compute @workgroup_size(256)
fn main(
    @builtin(workgroup_id) wid: vec3<u32>,
    @builtin(local_invocation_id) lid: vec3<u32>,
    @builtin(num_workgroups) grid: vec3<u32>
) {
    let block = wid.x + wid.y * grid.x;
    if block >= (params.n_values + 2047u) / 2048u {
        return;
    }
    let local = lid.x;
    let base = block * 2048u + local * 8u;
    var m = -3.402823466e38;
    for (var i = 0u; i < 8u; i++) {
        let idx = base + i;
        if idx < params.n_values {
            m = max(m, input[idx]);
        }
    }
    partial[local] = m;
    workgroupBarrier();

    var stride = 128u;
    loop {
        if local < stride {
            partial[local] = max(partial[local], partial[local + stride]);
        }
        workgroupBarrier();
        if stride == 1u {
            break;
        }
        stride = stride / 2u;
    }

    if local == 0u {
        output[block] = partial[0];
    }
}
