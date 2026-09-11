// SPDX-License-Identifier: GPL-3.0-only
@group(0) @binding(0) var<storage, read> input: array<f32>;
@group(0) @binding(1) var<storage, read> p: array<f32>;
@group(0) @binding(2) var<storage, read_write> output: array<f32>;
fn reflect(index:i32, n:i32)->i32 {
    let period=2*n;let i=((index%period)+period)%period;
    return select(i,period-1-i,i>=n);
}
fn mirror(index:i32,n:i32)->i32 {
    if(n<=1){return 0;}let period=2*n-2;let i=((index%period)+period)%period;
    return select(i,period-i,i>=n);
}
fn sample(x:i32,y:i32,c:u32)->f32 {
    let w=i32(p[1]);let h=i32(p[2]);let channels=u32(p[3]);
    let i=u32(reflect(y,h)*w+reflect(x,w))*channels;
    if(p[6]>0.5){return dot(vec3(input[i],input[i+1u],input[i+2u]),vec3(0.2125,0.7154,0.0721));}
    return input[i+c];
}
fn resize_sample(x:i32,y:i32,c:u32)->f32 {
    let w=i32(p[1]);let h=i32(p[2]);return input[u32(mirror(y,h)*w+mirror(x,w))*u32(p[3])+c];
}
@compute @workgroup_size(16,16)
fn main(@builtin(global_invocation_id) id:vec3<u32>){
    let ow=u32(p[7]);let oh=u32(p[8]);if(id.x>=ow || id.y>=oh){return;}
    let channels=select(u32(p[3]),1u,p[6]>0.5);let op=u32(p[0]);
    let x=i32(id.x*u32(p[5]));let y=i32(id.y*u32(p[5]));
    for(var c=0u;c<channels;c++){
        var value=0.0;
        if(op==2u){value=abs(sample(x-1,y,c)+sample(x+1,y,c)+sample(x,y-1,c)+sample(x,y+1,c)-4.0*sample(x,y,c));}
        else if(op==3u){
            let sx=(f32(id.x)+0.5)*p[1]/p[7]-0.5;let sy=f32(id.y)*p[9]+p[10];
            let ix=i32(floor(sx));let iy=i32(floor(sy));let fx=sx-floor(sx);let fy=sy-floor(sy);
            value=mix(mix(resize_sample(ix,iy,c),resize_sample(ix+1,iy,c),fx),mix(resize_sample(ix,iy+1,c),resize_sample(ix+1,iy+1,c),fx),fy);
        }else{
            let sigma=p[4];let radius=i32(4.0*sigma+0.5);var total=0.0;
            for(var k=-radius;k<=radius;k++){
                let weight=exp(-0.5*f32(k*k)/(sigma*sigma));
                let xx=select(x,x+k,op==0u);let yy=select(y,y+k,op==1u);
                value+=sample(xx,yy,c)*weight;total+=weight;
            }
            value/=total;
        }
        output[(id.y*ow+id.x)*channels+c]=value;
    }
}
