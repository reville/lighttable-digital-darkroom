@group(0) @binding(0) var<storage, read> input: array<f32>;
@group(0) @binding(1) var<storage, read> p: array<f32>;
@group(0) @binding(2) var<storage, read_write> output: array<f32>;
fn rgb(frame:u32, x:i32, y:i32) -> vec3<f32> {
    let w = u32(p[0]); let h = u32(p[1]);
    let i = ((frame * h + u32(clamp(y,0,i32(h)-1))) * w + u32(clamp(x,0,i32(w)-1))) * 4u;
    return vec3(input[i],input[i+1u],input[i+2u]);
}
fn gray(v:vec3<f32>)->f32 {return dot(v,vec3(0.2125,0.7154,0.0721));}
fn linear(v:f32)->f32 {if(v<=0.04045){return v/12.92;} return pow((v+0.055)/1.055,2.4);}
fn srgb(v:f32)->f32 {if(v<=0.0031308){return v*12.92;} return 1.055*pow(v,1.0/2.4)-0.055;}
@compute @workgroup_size(16,16)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    let w=u32(p[0]); let h=u32(p[1]); let n=u32(p[2]);
    if(id.x>=w || id.y>=h){return;}
    let x=i32(id.x); let y=i32(id.y);
    var med=vec3<f32>(0.0);
    for(var c=0u;c<3u;c++) {
        var values:array<f32,9>;
        for(var f=0u;f<n;f++){values[f]=rgb(f,x,y)[c];}
        for(var a=1u;a<n;a++) {
            let v=values[a]; var b=a;
            while(b>0u) {if(values[b-1u]<=v){break;} values[b]=values[b-1u];b--;}
            values[b]=v;
        }
        med[c]=0.5*(values[(n-1u)/2u]+values[n/2u]);
    }
    var sum=vec3<f32>(0.0);var total=0.0;
    for(var f=0u;f<n;f++) {
        let v=rgb(f,x,y);let l=gray(v);
        let contrast=abs(gray(rgb(f,x-1,y))+gray(rgb(f,x+1,y))+gray(rgb(f,x,y-1))+gray(rgb(f,x,y+1))-4.0*l);
        let mean=(v.x+v.y+v.z)/3.0;
        let deviation=v-vec3(mean);
        let saturation=sqrt(dot(deviation,deviation)/3.0);
        let distance=v-vec3(0.5);
        let exposed=exp(-dot(distance,distance)/(2.0*0.24*0.24));
        let delta=abs(v-med);let motion=(delta.x+delta.y+delta.z)/3.0;
        let weight=(1e-5+exposed+contrast*0.35+saturation*0.15)*exp(-pow(motion/0.18,2.0))*input[((f*h+id.y)*w+id.x)*4u+3u];
        sum+=vec3(linear(v.x),linear(v.y),linear(v.z))*weight;total+=weight;
    }
    let fused=max(sum/max(total,1e-6),vec3(0.0));let o=(id.y*w+id.x)*3u;
    output[o]=clamp(srgb(fused.x),0.0,1.0);output[o+1u]=clamp(srgb(fused.y),0.0,1.0);output[o+2u]=clamp(srgb(fused.z),0.0,1.0);
}
