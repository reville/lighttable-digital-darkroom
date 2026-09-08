@group(0) @binding(0) var<storage, read> input: array<f32>;
@group(0) @binding(1) var<storage, read> p: array<f32>;
@group(0) @binding(2) var<storage, read_write> output: array<f32>;
fn sample(x:i32,y:i32)->vec4<f32>{
    let w=i32(p[0]);let h=i32(p[1]);
    if(x<0 || y<0 || x>=w || y>=h){return vec4(0.0);}
    let i=u32((y*w+x)*3);let xx=x+i32(p[15]);let yy=y+i32(p[16]);
    let feather=f32(min(min(xx+1,i32(p[13])-xx),min(yy+1,i32(p[14])-yy)));
    return vec4(input[i],input[i+1u],input[i+2u],feather);
}
@compute @workgroup_size(16,16)
fn main(@builtin(global_invocation_id) id:vec3<u32>){
    let ow=u32(p[2]);let oh=u32(p[3]);if(id.x>=ow || id.y>=oh){return;}
    let x=f32(id.x);let y=f32(id.y);let d=p[10]*x+p[11]*y+p[12];
    let sx=(p[4]*x+p[5]*y+p[6])/d;let sy=(p[7]*x+p[8]*y+p[9])/d;
    let ix=i32(floor(sx));let iy=i32(floor(sy));let fx=sx-floor(sx);let fy=sy-floor(sy);
    var v=mix(mix(sample(ix,iy),sample(ix+1,iy),fx),mix(sample(ix,iy+1),sample(ix+1,iy+1),fx),fy);
    if(p[17]>0.5){
        // scipy shift's constant boundary rejects the entire interpolant when
        // its coordinate lies outside the image, including fractional edges.
        if(sx<0.0 || sy<0.0 || sx>p[0]-1.0 || sy>p[1]-1.0){v=vec4(0.0);}else{v.w=1.0;}
    }else{v=vec4(v.xyz*v.w,v.w);}
    let o=(id.y*ow+id.x)*4u;output[o]=v.x;output[o+1u]=v.y;output[o+2u]=v.z;output[o+3u]=v.w;
}
