#include <metal_stdlib>
using namespace metal;

struct VertexOut {
    float4 position [[position]];
    float2 uv;
};

struct GradeUniforms {
    float4 tone0;       // exposure, contrast, highlights, shadows
    float4 tone1;       // whites, blacks, temp, tint
    float4 tone2;       // vibrance, saturation, texture, clarity
    float4 tone3;       // dehaze, vignette, texel x, texel y
    float4 vignetteShape; // size, feather, reserved, reserved
    float4 detail0;     // sharpness, radius, detail, masking
    float4 detail1;     // luminance noise, color noise, red/cyan CA, blue/yellow CA
    float4 curveOn;
    float4 viewport;    // UV scale x/y, offset x/y
    float4 sourceRegion; // tile span and origin in full-frame UV
    float4 compare;     // original-image wipe position, 0 disables
    float4 reference0;  // active, split mode, amount, scale
    float4 reference1;  // x/y offset, reserved
    float4 hsl0;
    float4 hsl1;
    float4 hsl2;
    float4 hsl3;
    float4 hsl4;
    float4 hsl5;
    float4 hsl6;
    float4 hsl7;
    float4 point0;
    float4 point1;
    float4 point2;
    float4 point3;
    float4 point4;
    float4 point5;
    float4 point6;
    float4 point7;
    float4 pointLuma0;
    float4 pointLuma1;
    float4 pointUniform0;
    float4 pointUniform1;
    float4 pointUniform2;
    float4 pointUniform3;
    float4 pointUniform4;
    float4 pointUniform5;
    float4 pointUniform6;
    float4 pointUniform7;
    float4 pointRefLuma0;
    float4 pointRefLuma1;
    float4 pointRefOn0;
    float4 pointRefOn1;
    float4 pointCount;
    float4 colorGrade0;
    float4 colorGrade1;
    float4 colorGrade2;
    float4 colorGrade3;
    float4 colorGradeSettings;
    float4 softProof;
    float4 spotVisualization; // preview only: enabled, threshold
    float4 optics0;     // distortion, vertical, horizontal, rotation radians
    float4 optics1;     // scale, vignette, heal count, mask atlas tiles
    float4 optics2;     // horizontal flip, vertical flip, reserved, reserved
};

struct LocalUniform {
    float4 tone;
    float4 color;
    float4 range;
    float4 detail;      // texture, clarity, reserved, reserved
    float4 colorRange;  // hue, range, amount, enabled
};

struct HealUniform {
    float4 points;      // target x/y, source x/y
    float4 settings;    // radius, feather, opacity, mode (1 remove, 2 heal, 3 clone)
};

vertex VertexOut nativePreviewVertex(uint vertexID [[vertex_id]]) {
    const float2 positions[4] = {
        float2(-1.0, -1.0), float2(1.0, -1.0),
        float2(-1.0, 1.0), float2(1.0, 1.0),
    };
    float2 position = positions[vertexID];
    return {
        float4(position, 0.0, 1.0),
        float2(position.x * 0.5 + 0.5, 0.5 - position.y * 0.5),
    };
}

constant float3 LUMA = float3(0.2126, 0.7152, 0.0722);

float3 sampleFrame(texture2d<float> image, texture2d<float> fallback,
                   sampler linearSampler, float2 coordinate, float4 region) {
    coordinate = clamp(coordinate, 0.0, 1.0);
    float2 tile = (coordinate - region.zw) / region.xy;
    if (any(tile < 0.0) || any(tile > 1.0)) {
        return fallback.sample(linearSampler, coordinate).rgb;
    }
    return image.sample(linearSampler, tile).rgb;
}

float3 sampleSource(texture2d<float> image, texture2d<float> fallback, sampler linearSampler,
                    float2 coordinate, float2 texel,
                    float redCyan, float blueYellow, float4 region) {
    float3 base = sampleFrame(image, fallback, linearSampler, coordinate, region);
    float2 radial = coordinate - 0.5;
    float red = sampleFrame(image, fallback, linearSampler,
        coordinate + radial * texel * redCyan * 6.0, region).r;
    float blue = sampleFrame(image, fallback, linearSampler,
        coordinate + radial * texel * blueYellow * 6.0, region).b;
    return float3(red, base.g, blue);
}

float2 manualSourceCoordinate(float2 uv, constant GradeUniforms &grade,
                              float2 dimensions, thread float &radiusSquared) {
    float halfSize = max(min(dimensions.x, dimensions.y) * 0.5, 1.0);
    float scale = max(grade.optics1.x, 1.0);
    float2 normalized = ((uv - 0.5) * dimensions) / halfSize / scale;
    if (grade.optics2.x > 0.5) normalized.x = -normalized.x;
    if (grade.optics2.y > 0.5) normalized.y = -normalized.y;
    float cosine = cos(grade.optics0.w);
    float sine = sin(grade.optics0.w);
    float2 rotated = float2(
        cosine * normalized.x - sine * normalized.y,
        sine * normalized.x + cosine * normalized.y);
    float2 projected = float2(
        rotated.x * (1.0 + grade.optics0.y * 0.45 * rotated.y),
        rotated.y * (1.0 + grade.optics0.z * 0.45 * rotated.x));
    radiusSquared = dot(projected, projected);
    float factor = 1.0 + grade.optics0.x * 0.18 * radiusSquared;
    return projected * factor * halfSize / dimensions + 0.5;
}

float3 sampleEditedSource(texture2d<float> image, texture2d<float> fallback, sampler linearSampler,
                          float2 outputCoordinate, constant GradeUniforms &grade,
                          float2 texel, float redCyan, float blueYellow) {
    float radiusSquared = 0.0;
    float2 dimensions = float2(image.get_width(), image.get_height()) / grade.sourceRegion.xy;
    float2 edge = 0.5 / dimensions;
    // Upscaled display pixels can lie outside the source's outermost centres.
    // Replicate those output edges before applying the optical mapping.
    outputCoordinate = clamp(outputCoordinate, edge, 1.0 - edge);
    float2 coordinate = manualSourceCoordinate(
        outputCoordinate, grade, dimensions, radiusSquared);
    // scipy's constant-border interpolation accepts pixel centres, not the
    // half-pixel strip outside them. Allow only floating-point roundoff so
    // identity mapping keeps its outermost pixels.
    float2 roundoff = 0.0001 / dimensions;
    if (any(coordinate < edge - roundoff) ||
        any(coordinate > 1.0 - edge + roundoff)) return 0.0;
    float3 color = sampleSource(
        image, fallback, linearSampler, coordinate, texel, redCyan, blueYellow, grade.sourceRegion);
    if (grade.optics1.y != 0.0) {
        float radial = clamp(radiusSquared / 2.0, 0.0, 1.5);
        color *= 1.0 + grade.optics1.y * 0.8 * radial;
    }
    return clamp(color, 0.0, 1.0);
}

float3 sampleEditedNeighbor(texture2d<float> image, texture2d<float> fallback, sampler linearSampler,
                            float2 outputCoordinate, constant GradeUniforms &grade,
                            float2 texel, float redCyan, float blueYellow) {
    // CPU grading replicates the outermost pixel for blur/sharpen taps. Clamp
    // in output space, before optical mapping: genuine empty areas created by
    // rotation/distortion must still become black in sampleEditedSource.
    float2 coordinate = clamp(outputCoordinate, texel * 0.5, 1.0 - texel * 0.5);
    return sampleEditedSource(image, fallback, linearSampler, coordinate,
                              grade, texel, redCyan, blueYellow);
}

float3 applyHeals(float3 color, float2 uv,
                  texture2d<float> image, texture2d<float> fallback, sampler linearSampler,
                  constant GradeUniforms &grade,
                  constant HealUniform *heals,
                  float2 texel, float redCyan, float blueYellow) {
    float2 dimensions = float2(image.get_width(), image.get_height()) / grade.sourceRegion.xy;
    float minimum = max(min(dimensions.x, dimensions.y), 1.0);
    uint count = min(uint(max(grade.optics1.z, 0.0)), 16u);
    for (uint index = 0; index < count; index++) {
        HealUniform spot = heals[index];
        float2 target = spot.points.xy;
        float2 source = spot.points.zw;
        float radius = max(spot.settings.x, 0.0001);
        float distance = length((uv - target) * dimensions) / (radius * minimum);
        if (distance >= 1.0) continue;
        float inner = max(0.0, 1.0 - spot.settings.y);
        float weight = (1.0 - smoothstep(inner, 1.0, distance))
            * spot.settings.z;
        float3 replacement;
        if (spot.settings.w < 1.5) {
            float2 ring = radius * minimum / dimensions * 1.25;
            replacement = (
                sampleEditedSource(image, fallback, linearSampler, target + float2(ring.x, 0.0),
                    grade, texel, redCyan, blueYellow)
                + sampleEditedSource(image, fallback, linearSampler, target - float2(ring.x, 0.0),
                    grade, texel, redCyan, blueYellow)
                + sampleEditedSource(image, fallback, linearSampler, target + float2(0.0, ring.y),
                    grade, texel, redCyan, blueYellow)
                + sampleEditedSource(image, fallback, linearSampler, target - float2(0.0, ring.y),
                    grade, texel, redCyan, blueYellow)) * 0.25;
        } else {
            replacement = sampleEditedSource(
                image, fallback, linearSampler, uv + source - target,
                grade, texel, redCyan, blueYellow);
        }
        color = mix(color, replacement, clamp(weight, 0.0, 1.0));
    }
    return color;
}

float3 srgbToLinear(float3 c) {
    return select(c / 12.92,
                  pow((c + 0.055) / 1.055, float3(2.4)),
                  c >= 0.04045);
}

float3 linearToSrgb(float3 c) {
    c = max(c, 0.0);
    return select(c * 12.92,
                  1.055 * pow(c, float3(1.0 / 2.4)) - 0.055,
                  c >= 0.0031308);
}

float curveAt(float value, int channel,
              texture2d<float> curve, sampler linearSampler) {
    float x = (clamp(value, 0.0, 1.0) * 255.0 + 0.5) / 256.0;
    float4 table = curve.sample(linearSampler, float2(x, 0.5));
    return channel == 0 ? table.r
        : channel == 1 ? table.g
        : channel == 2 ? table.b : table.a;
}

float3 rgbToHsv(float3 c) {
    float maximum = max(c.r, max(c.g, c.b));
    float minimum = min(c.r, min(c.g, c.b));
    float delta = maximum - minimum;
    float hue = 0.0;
    if (delta > 1e-5) {
        if (maximum == c.r) hue = fmod((c.g - c.b) / delta, 6.0);
        else if (maximum == c.g) hue = (c.b - c.r) / delta + 2.0;
        else hue = (c.r - c.g) / delta + 4.0;
        hue = fmod(hue * 60.0 + 360.0, 360.0);
    }
    return float3(hue, maximum > 1e-5 ? delta / maximum : 0.0, maximum);
}

float3 hsvToRgb(float3 hsv) {
    float hue = fmod(hsv.x + 360.0, 360.0) / 60.0;
    float saturation = hsv.y;
    float value = hsv.z;
    int index = int(fmod(floor(hue), 6.0));
    float fraction = hue - floor(hue);
    float p = value * (1.0 - saturation);
    float q = value * (1.0 - saturation * fraction);
    float t = value * (1.0 - saturation * (1.0 - fraction));
    if (index == 0) return float3(value, t, p);
    if (index == 1) return float3(q, value, p);
    if (index == 2) return float3(p, value, t);
    if (index == 3) return float3(p, q, value);
    if (index == 4) return float3(t, p, value);
    return float3(value, p, q);
}

float hslCentre(int index) {
    if (index == 0) return 0.0;
    if (index == 1) return 30.0;
    if (index == 2) return 60.0;
    if (index == 3) return 120.0;
    if (index == 4) return 180.0;
    if (index == 5) return 240.0;
    if (index == 6) return 280.0;
    return 320.0;
}

float3 hslValue(constant GradeUniforms &grade, int index) {
    if (index == 0) return grade.hsl0.xyz;
    if (index == 1) return grade.hsl1.xyz;
    if (index == 2) return grade.hsl2.xyz;
    if (index == 3) return grade.hsl3.xyz;
    if (index == 4) return grade.hsl4.xyz;
    if (index == 5) return grade.hsl5.xyz;
    if (index == 6) return grade.hsl6.xyz;
    return grade.hsl7.xyz;
}

float4 pointValue(constant GradeUniforms &grade, int index) {
    if (index == 0) return grade.point0;
    if (index == 1) return grade.point1;
    if (index == 2) return grade.point2;
    if (index == 3) return grade.point3;
    if (index == 4) return grade.point4;
    if (index == 5) return grade.point5;
    if (index == 6) return grade.point6;
    return grade.point7;
}

float pointLuminance(constant GradeUniforms &grade, int index) {
    return index < 4 ? grade.pointLuma0[index] : grade.pointLuma1[index - 4];
}

float4 pointUniformity(constant GradeUniforms &grade, int index) {
    if (index == 0) return grade.pointUniform0;
    if (index == 1) return grade.pointUniform1;
    if (index == 2) return grade.pointUniform2;
    if (index == 3) return grade.pointUniform3;
    if (index == 4) return grade.pointUniform4;
    if (index == 5) return grade.pointUniform5;
    if (index == 6) return grade.pointUniform6;
    return grade.pointUniform7;
}

float pointReferenceLuminance(constant GradeUniforms &grade, int index) {
    return index < 4 ? grade.pointRefLuma0[index] : grade.pointRefLuma1[index - 4];
}

float pointReferencesAvailable(constant GradeUniforms &grade, int index) {
    return index < 4 ? grade.pointRefOn0[index] : grade.pointRefOn1[index - 4];
}

float3 applyColorGradeTone(float3 color, float3 settings, float weight,
                           float strength, float luminanceStrength) {
    float3 tint = hsvToRgb(float3(settings.x, 1.0, 1.0));
    float3 chroma = tint - dot(tint, LUMA);
    return color + (chroma * settings.y * strength
        + settings.z * luminanceStrength) * weight;
}

float3 applySoftProof(float3 color, float4 proof, float2 position) {
    if (proof.x < 0.5) return color;
    float gamutScale = 1.0;
    float blackPoint = 0.0;
    float paperWhite = 1.0;
    if (proof.y > 2.5) {
        gamutScale = 0.86; blackPoint = 0.012; paperWhite = 0.96;
    } else if (proof.y > 1.5) {
        gamutScale = 0.68; blackPoint = 0.032; paperWhite = 0.90;
    } else if (proof.y > 0.5) {
        gamutScale = 1.15;
    }
    float3 linear = srgbToLinear(color);
    float luminance = dot(linear, LUMA);
    float3 target = luminance + (linear - luminance) / gamutScale;
    bool outside = min(target.r, min(target.g, target.b)) < 0.0
        || max(target.r, max(target.g, target.b)) > 1.0;
    float3 result = luminance + (linear - luminance) * gamutScale;
    if (proof.z > 0.5 && proof.y > 1.5) {
        result = blackPoint + result * (paperWhite - blackPoint);
    }
    color = clamp(linearToSrgb(result), 0.0, 1.0);
    if (proof.w > 0.5 && outside) {
        float stripe = step(0.5, fract((position.x + position.y) / 10.0));
        color = mix(color, float3(1.0, 0.0, 0.72), 0.48 + stripe * 0.18);
    }
    return color;
}

float3 localGrade(float3 color, float4 tone, float4 localColorValue,
                  float4 detail, float2 uv, texture2d<float> image, texture2d<float> fallback,
                  sampler linearSampler, constant GradeUniforms &grade,
                  float2 texel) {
    if (detail.x != 0.0 || detail.y != 0.0) {
        float3 blur = (color
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(texel.x, 0.0),
                                 grade, texel, grade.detail1.z, grade.detail1.w)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(texel.x, 0.0),
                                 grade, texel, grade.detail1.z, grade.detail1.w)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(0.0, texel.y),
                                 grade, texel, grade.detail1.z, grade.detail1.w)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(0.0, texel.y),
                                 grade, texel, grade.detail1.z, grade.detail1.w)) / 5.0;
        float3 localDetail = color - blur;
        color = clamp(color + localDetail * detail.x * 1.1, 0.0, 1.0);
        float middle = clamp(1.0 - abs(dot(color, LUMA) - 0.5) * 2.0, 0.0, 1.0);
        color = clamp(color + localDetail * detail.y * 1.8 * middle, 0.0, 1.0);
    }
    float3 linear = srgbToLinear(color) * pow(2.0, tone.x);
    if (tone.z != 0.0 || tone.w != 0.0) {
        float luminance = max(dot(linear, LUMA), 0.0);
        if (tone.z != 0.0) {
            float mask = pow(clamp((luminance - 0.35) / 0.65, 0.0, 1.0), 1.2);
            linear *= 1.0 + tone.z * 0.85 * mask;
        }
        if (tone.w != 0.0) {
            float mask = pow(clamp((0.45 - luminance) / 0.45, 0.0, 1.0), 1.2);
            linear *= 1.0 + tone.w * 1.5 * mask;
        }
    }
    color = clamp(linearToSrgb(linear), 0.0, 1.0);
    if (tone.y != 0.0) {
        if (tone.y > 0.0) {
            float3 shaped = color * color * (3.0 - 2.0 * color);
            color += (shaped - color) * tone.y;
        } else {
            color = 0.5 + (color - 0.5) * (1.0 + tone.y * 0.8);
        }
        color = clamp(color, 0.0, 1.0);
    }
    if (localColorValue.x != 0.0 || localColorValue.y != 0.0) {
        color = clamp(color * float3(
            1.0 + localColorValue.x * 0.18 + localColorValue.y * 0.06,
            1.0 - localColorValue.y * 0.12,
            1.0 - localColorValue.x * 0.18 + localColorValue.y * 0.06),
            0.0, 1.0);
    }
    if (localColorValue.z != 0.0) {
        float luminance = dot(color, LUMA);
        color = clamp(luminance + (color - luminance)
            * (1.0 + localColorValue.z), 0.0, 1.0);
    }
    return color;
}

float3 applyLocal(float3 color, float geometric, constant LocalUniform &local,
                  float2 uv, texture2d<float> image, texture2d<float> fallback, sampler linearSampler,
                  constant GradeUniforms &grade, float2 texel) {
    float4 tone = local.tone;
    float4 localColorValue = local.color;
    float4 range = local.range;
    if (range.z < 0.5 || geometric <= 0.0) return color;
    float luminance = dot(color, LUMA);
    float lower = range.x <= 0.0 ? 1.0
        : smoothstep(range.x - 0.04, range.x + 0.04, luminance);
    float upper = range.y >= 1.0 ? 1.0
        : 1.0 - smoothstep(range.y - 0.04, range.y + 0.04, luminance);
    float colorWeight = 1.0;
    if (local.colorRange.w > 0.5) {
        float3 hsv = rgbToHsv(color);
        float difference = abs(fmod(
            hsv.x - local.colorRange.x + 540.0, 360.0) - 180.0);
        float selected = (1.0 - smoothstep(local.colorRange.y * 0.45,
            local.colorRange.y, difference)) * hsv.y;
        colorWeight = 1.0 - local.colorRange.z * (1.0 - selected);
    }
    float weight = clamp(geometric * localColorValue.w * lower * upper
                         * colorWeight, 0.0, 1.0);
    return mix(color, localGrade(color, tone, localColorValue, local.detail,
        uv, image, fallback, linearSampler, grade, texel), weight);
}

fragment float4 nativePreviewFragment(
    VertexOut input [[stage_in]],
    texture2d<float> image [[texture(0)]],
    texture2d<float> curve [[texture(1)]],
    texture2d<float> original [[texture(2)]],
    texture2d<float> masks [[texture(3)]],
    texture2d<float> reference [[texture(4)]],
    texture2d<float> fallback [[texture(5)]],
    sampler linearSampler [[sampler(0)]],
    constant GradeUniforms &grade [[buffer(0)]],
    constant HealUniform *heals [[buffer(1)]],
    constant LocalUniform *locals [[buffer(2)]]) {
    float2 uv = input.uv * grade.viewport.xy + grade.viewport.zw;
    if (grade.compare.x > 0.0 && uv.x <= grade.compare.x) {
        return float4(
            original.sample(linearSampler, clamp(uv, 0.0, 1.0)).rgb, 1.0);
    }
    float exposure = grade.tone0.x;
    float contrast = grade.tone0.y;
    float highlights = grade.tone0.z;
    float shadows = grade.tone0.w;
    float whites = grade.tone1.x;
    float blacks = grade.tone1.y;
    float temperature = grade.tone1.z;
    float tint = grade.tone1.w;
    float vibrance = grade.tone2.x;
    float saturation = grade.tone2.y;
    float texture = grade.tone2.z;
    float clarity = grade.tone2.w;
    float dehaze = grade.tone3.x;
    float vignette = grade.tone3.y;
    float2 texel = grade.tone3.zw;
    float sharpness = grade.detail0.x;
    float sharpenRadius = grade.detail0.y;
    float sharpenDetail = grade.detail0.z;
    float sharpenMasking = grade.detail0.w;
    float luminanceNoise = grade.detail1.x;
    float colorNoise = grade.detail1.y;
    float redCyan = grade.detail1.z;
    float blueYellow = grade.detail1.w;
    float3 color = sampleEditedSource(
        image, fallback, linearSampler, uv, grade, texel, redCyan, blueYellow);
    color = applyHeals(
        color, uv, image, fallback, linearSampler, grade, heals,
        texel, redCyan, blueYellow);

    if (luminanceNoise != 0.0 || colorNoise != 0.0) {
        float3 blur = (color
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(texel.x, 0.0),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(texel.x, 0.0),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(0.0, texel.y),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(0.0, texel.y),
                                 grade, texel, redCyan, blueYellow)) / 5.0;
        float luminance = dot(color, LUMA);
        float blurLuminance = dot(blur, LUMA);
        if (luminanceNoise != 0.0) {
            color = clamp(color + (blurLuminance - luminance) * luminanceNoise,
                          0.0, 1.0);
            luminance = dot(color, LUMA);
        }
        if (colorNoise != 0.0) {
            float3 chroma = color - luminance;
            float3 blurChroma = blur - blurLuminance;
            color = clamp(luminance + chroma * (1.0 - colorNoise)
                + blurChroma * colorNoise, 0.0, 1.0);
        }
    }

    if (texture != 0.0 || clarity != 0.0) {
        float3 blur = (color
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(texel.x, 0.0),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(texel.x, 0.0),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(0.0, texel.y),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(0.0, texel.y),
                                 grade, texel, redCyan, blueYellow)) / 5.0;
        float3 detail = color - blur;
        color = clamp(color + detail * texture * 1.1, 0.0, 1.0);
        float middle = clamp(1.0 - abs(dot(color, LUMA) - 0.5) * 2.0, 0.0, 1.0);
        color = clamp(color + detail * clarity * 1.8 * middle, 0.0, 1.0);
    }

    if (sharpness != 0.0) {
        float2 radius = texel * sharpenRadius;
        float3 blur = (color
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(radius.x, 0.0),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(radius.x, 0.0),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv + float2(0.0, radius.y),
                                 grade, texel, redCyan, blueYellow)
            + sampleEditedNeighbor(image, fallback, linearSampler, uv - float2(0.0, radius.y),
                                 grade, texel, redCyan, blueYellow)) / 5.0;
        float3 detail = color - blur;
        float luminanceDetail = dot(detail, LUMA);
        float3 shaped = luminanceDetail
            + (detail - luminanceDetail) * sharpenDetail;
        float edgeMask = smoothstep(0.015, 0.16, length(detail));
        float mask = mix(1.0, edgeMask, sharpenMasking);
        color = clamp(color + shaped * sharpness * 1.8 * mask, 0.0, 1.0);
    }

    float3 linear = srgbToLinear(color) * pow(2.0, exposure);
    if (highlights != 0.0 || shadows != 0.0) {
        float luminance = max(dot(linear, LUMA), 0.0);
        if (highlights != 0.0) {
            float mask = pow(clamp((luminance - 0.35) / 0.65, 0.0, 1.0), 1.2);
            linear *= 1.0 + highlights * 0.85 * mask;
        }
        if (shadows != 0.0) {
            float mask = pow(clamp((0.45 - luminance) / 0.45, 0.0, 1.0), 1.2);
            linear *= 1.0 + shadows * 1.5 * mask;
        }
    }
    color = clamp(linearToSrgb(linear), 0.0, 1.0);

    if (whites != 0.0 || blacks != 0.0) {
        float whitePoint = 1.0 + whites * 0.35;
        float blackPoint = blacks * -0.25;
        color = clamp((color - blackPoint) / max(whitePoint - blackPoint, 1e-4), 0.0, 1.0);
    }
    if (contrast != 0.0) {
        if (contrast > 0.0) {
            float3 curveColor = color * color * (3.0 - 2.0 * color);
            color += (curveColor - color) * contrast;
        } else {
            color = 0.5 + (color - 0.5) * (1.0 + contrast * 0.8);
        }
        color = clamp(color, 0.0, 1.0);
    }
    if (dehaze != 0.0) {
        float haze = dehaze * 0.12;
        color = clamp((color - haze) / max(1.0 - haze, 0.2), 0.0, 1.0);
        float luminance = dot(color, LUMA);
        color = clamp(luminance + (color - luminance) * (1.0 + dehaze * 0.18), 0.0, 1.0);
    }
    if (temperature != 0.0 || tint != 0.0) {
        float3 gain = float3(
            1.0 + temperature * 0.18 + tint * 0.06,
            1.0 - tint * 0.12,
            1.0 - temperature * 0.18 + tint * 0.06);
        color = clamp(color * gain, 0.0, 1.0);
    }
    if (saturation != 0.0) {
        float luminance = dot(color, LUMA);
        color = clamp(luminance + (color - luminance) * (1.0 + saturation), 0.0, 1.0);
    }
    if (vibrance != 0.0) {
        float maximum = max(color.r, max(color.g, color.b));
        float minimum = min(color.r, min(color.g, color.b));
        float currentSaturation = (maximum - minimum) / max(maximum, 1e-4);
        float luminance = dot(color, LUMA);
        color = clamp(luminance + (color - luminance)
            * (1.0 + vibrance * (1.0 - currentSaturation)), 0.0, 1.0);
    }

    bool hslEnabled = false;
    for (int index = 0; index < 8; index++) {
        hslEnabled = hslEnabled || any(abs(hslValue(grade, index)) > 1e-6);
    }
    if (hslEnabled) {
        float3 hsv = rgbToHsv(color);
        float hueShift = 0.0;
        float saturationShift = 0.0;
        float luminanceShift = 0.0;
        for (int index = 0; index < 8; index++) {
            float difference = abs(fmod(hsv.x - hslCentre(index) + 540.0, 360.0) - 180.0);
            float weight = clamp(1.0 - difference / 45.0, 0.0, 1.0);
            weight = weight * weight * (3.0 - 2.0 * weight) * hsv.y;
            float3 adjustment = hslValue(grade, index);
            hueShift += weight * adjustment.x * 30.0;
            saturationShift += weight * adjustment.y;
            luminanceShift += weight * adjustment.z;
        }
        hsv.x = fmod(hsv.x + hueShift + 360.0, 360.0);
        hsv.y = clamp(hsv.y * (1.0 + saturationShift), 0.0, 1.0);
        hsv.z = clamp(hsv.z * (1.0 + luminanceShift * 0.5), 0.0, 1.0);
        color = clamp(hsvToRgb(hsv), 0.0, 1.0);
    }

    if (grade.pointCount.x > 0.5) {
        float3 hsv = rgbToHsv(color);
        int count = min(int(grade.pointCount.x), 8);
        for (int index = 0; index < count; index++) {
            float4 point = pointValue(grade, index);
            float difference = abs(
                fmod(hsv.x - point.x + 540.0, 360.0) - 180.0);
            float weight = (1.0 - smoothstep(
                point.y * 0.45, point.y, difference)) * hsv.y;
            float4 uniformity = pointUniformity(grade, index);
            if (pointReferencesAvailable(grade, index) > 0.5) {
                float shortest = fmod(point.x - hsv.x + 540.0, 360.0) - 180.0;
                hsv.x = fmod(hsv.x + shortest * uniformity.x * weight
                    + 360.0, 360.0);
                hsv.y = clamp(hsv.y + (uniformity.w - hsv.y)
                    * uniformity.y * weight, 0.0, 1.0);
                hsv.z = clamp(hsv.z + (pointReferenceLuminance(grade, index)
                    - hsv.z) * uniformity.z * weight, 0.0, 1.0);
            }
            hsv.x = fmod(hsv.x + point.z * weight + 360.0, 360.0);
            hsv.y = clamp(hsv.y * (1.0 + point.w * weight), 0.0, 1.0);
            hsv.z = clamp(hsv.z * (1.0 + pointLuminance(
                grade, index) * 0.5 * weight), 0.0, 1.0);
        }
        color = clamp(hsvToRgb(hsv), 0.0, 1.0);
    }

    if (grade.colorGradeSettings.z > 0.5) {
        float luminance = dot(color, LUMA);
        float shift = grade.colorGradeSettings.x * 0.2;
        float width = 0.18 + grade.colorGradeSettings.y * 0.22;
        float shadows = 1.0 - smoothstep(
            0.28 + shift - width, 0.28 + shift + width, luminance);
        float highlights = smoothstep(
            0.72 + shift - width, 0.72 + shift + width, luminance);
        float midtones = clamp(1.0 - shadows - highlights, 0.0, 1.0);
        color = applyColorGradeTone(
            color, grade.colorGrade0.xyz, shadows, 0.28, 0.22);
        color = applyColorGradeTone(
            color, grade.colorGrade1.xyz, midtones, 0.28, 0.22);
        color = applyColorGradeTone(
            color, grade.colorGrade2.xyz, highlights, 0.28, 0.22);
        color = applyColorGradeTone(
            color, grade.colorGrade3.xyz, 1.0, 0.2, 0.18);
        color = clamp(color, 0.0, 1.0);
    }

    if (grade.curveOn.x > 0.5) {
        color = float3(
            curveAt(color.r, 0, curve, linearSampler),
            curveAt(color.g, 0, curve, linearSampler),
            curveAt(color.b, 0, curve, linearSampler));
    }
    if (grade.curveOn.y > 0.5) color.r = curveAt(color.r, 1, curve, linearSampler);
    if (grade.curveOn.z > 0.5) color.g = curveAt(color.g, 2, curve, linearSampler);
    if (grade.curveOn.w > 0.5) color.b = curveAt(color.b, 3, curve, linearSampler);

    if (vignette != 0.0) {
        float2 normalized = (uv - 0.5) * 2.0;
        float radius = length(normalized) / 1.4142;
        if (grade.vignetteShape.x != 0.5 || grade.vignetteShape.y != 1.0) {
            // Use the same pixel endpoints as export for defined vignette edges.
            float2 position = (uv - 0.5 * texel) / max(float2(1.0) - texel, texel);
            radius = length((position - 0.5) * 2.0) / 1.4142;
            float outer = 0.25 + 1.5 * grade.vignetteShape.x;
            float width = outer * max(grade.vignetteShape.y, 0.01);
            radius = clamp((radius - outer + width) / width, 0.0, 1.0);
        }
        color = clamp(color * clamp(1.0 - vignette * 0.9 * pow(radius, 2.2), 0.0, 2.0), 0.0, 1.0);
    }
    int maskTiles = max(1, min(int(grade.optics1.w), 4));
    for (int tile = 0; tile < maskTiles; tile++) {
        float2 maskUv = float2(uv.x, (uv.y + float(tile)) / float(maskTiles));
        float4 maskValues = masks.sample(linearSampler, clamp(maskUv, 0.0, 1.0));
        int base = tile * 4;
        color = applyLocal(color, maskValues.r, locals[base], uv,
            image, fallback, linearSampler, grade, texel);
        color = applyLocal(color, maskValues.g, locals[base + 1], uv,
            image, fallback, linearSampler, grade, texel);
        color = applyLocal(color, maskValues.b, locals[base + 2], uv,
            image, fallback, linearSampler, grade, texel);
        color = applyLocal(color, maskValues.a, locals[base + 3], uv,
            image, fallback, linearSampler, grade, texel);
    }
    color = applySoftProof(color, grade.softProof, input.position.xy);
    if (grade.spotVisualization.x > 0.5) {
        float threshold = grade.spotVisualization.y;
        float value = (0.5 - dot(color, LUMA)) * (2.0 + threshold * 7.0) + 0.5;
        color = float3(clamp(clamp(value, 0.0, 1.0) * (0.72 + threshold * 0.35), 0.0, 1.0));
    }
    if (grade.reference0.x > 0.5) {
        float2 referenceUv = (uv - 0.5 - grade.reference1.xy)
            / max(grade.reference0.w, 0.01) + 0.5;
        bool inside = all(referenceUv >= 0.0) && all(referenceUv <= 1.0);
        float weight = grade.reference0.y > 0.5
            ? (uv.x <= grade.reference0.z ? 1.0 : 0.0)
            : grade.reference0.z;
        if (inside && weight > 0.0) {
            float3 referenceColor = reference.sample(
                linearSampler, referenceUv).rgb;
            color = mix(color, referenceColor, clamp(weight, 0.0, 1.0));
        }
    }
    return float4(color, 1.0);
}
