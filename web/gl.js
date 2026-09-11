// SPDX-License-Identifier: GPL-3.0-only
import { t as tr } from './i18n.js';
// WebGL grade preview. The fragment shader below mirrors grade.py exactly —
// same order of operations, same constants. Change one, change the other.

export const HSL_BANDS = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue',
  'purple', 'magenta'];

export const GRADE_DEFAULTS = {
  exposure: 0, contrast: 0, highlights: 0, shadows: 0, whites: 0, blacks: 0,
  temp: 0, tint: 0, vibrance: 0, saturation: 0,
  texture: 0, clarity: 0, dehaze: 0, vignette: 0,
  vignetteSize: 0.5, vignetteFeather: 1,
  sharpness: 0, sharpenRadius: 1, sharpenDetail: 0.25, sharpenMasking: 0,
  luminanceNoise: 0, colorNoise: 0,
  chromaticAberrationRedCyan: 0, chromaticAberrationBlueYellow: 0,
  monochrome: 0,
};

const VERT = `
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  v_uv = vec2(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5);
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const FRAG = `
precision highp float;
varying vec2 v_uv;
uniform sampler2D u_img;
uniform sampler2D u_original;
uniform sampler2D u_reference;
uniform sampler2D u_masks;          // vertical RGBA atlas, four masks per tile
uniform float u_compare, u_originalReady;
uniform vec4 u_referenceView;       // active, split mode, amount, scale
uniform vec2 u_referenceOffset;
uniform float u_exposure, u_contrast, u_highlights, u_shadows;
uniform float u_whites, u_blacks, u_temp, u_tint;
uniform float u_vibrance, u_saturation, u_texture, u_clarity, u_dehaze, u_vignette;
uniform float u_vignetteSize, u_vignetteFeather;
uniform float u_sharpness, u_sharpenRadius, u_sharpenDetail, u_sharpenMasking;
uniform float u_luminanceNoise, u_colorNoise;
uniform float u_chromaticAberrationRedCyan, u_chromaticAberrationBlueYellow;
uniform float u_monochrome;
uniform vec2 u_texel;
uniform vec2 u_uvScale, u_uvOffset;
uniform sampler2D u_curve;          // 256x1 RGBA: L, R, G, B tables
uniform vec4 u_curveOn;             // which of those tables are active
uniform vec3 u_hsl[8];              // per band: hue, sat, lum
uniform float u_hslOn;
uniform vec4 u_pointColor[8];        // hue, range, hue shift, saturation
uniform float u_pointLuminance[8];
uniform vec4 u_pointUniform[8];      // hue, saturation, luminance uniformity, ref saturation
uniform vec2 u_pointRefLuminance[8]; // ref luminance, references available
uniform float u_pointColorCount;
uniform vec3 u_cgShadows, u_cgMidtones, u_cgHighlights, u_cgGlobal;
uniform float u_cgBalance, u_cgBlending, u_cgOn;
uniform vec4 u_softProof;            // enabled, target id, paper simulation, gamut warning
uniform vec2 u_spotVisualization;    // preview only: enabled, threshold
uniform float u_clippingOverlay;     // preview only: enabled (1.0 or 0.0)
uniform vec4 u_localOn[4], u_localOpacity[4], u_localLumaLow[4], u_localLumaHigh[4];
uniform vec4 u_localExposure[4], u_localContrast[4], u_localHighlights[4], u_localShadows[4];
uniform vec4 u_localTemp[4], u_localTint[4], u_localSaturation[4];
uniform vec4 u_localDetail[4];       // texture channels, clarity follows in paired lanes
uniform vec4 u_localClarity[4];
uniform vec4 u_localColorRange[4];   // hue/range/amount/on, one mask per lane below
uniform vec4 u_localColorRangeB[4];
uniform vec4 u_localColorRangeC[4];
uniform vec4 u_localColorRangeD[4];
uniform float u_maskTiles;
float hslCentre(int i) {
  if (i == 0) return 0.0;
  if (i == 1) return 30.0;
  if (i == 2) return 60.0;
  if (i == 3) return 120.0;
  if (i == 4) return 180.0;
  if (i == 5) return 240.0;
  if (i == 6) return 280.0;
  return 320.0;
}
const vec3 LUMA = vec3(0.2126, 0.7152, 0.0722);

vec3 sampleSource(vec2 coord) {
  coord = clamp(coord, vec2(0.0), vec2(1.0));
  vec3 base = texture2D(u_img, coord).rgb;
  vec2 radial = coord - 0.5;
  float red = texture2D(u_img, clamp(coord + radial * u_texel *
    u_chromaticAberrationRedCyan * 6.0, vec2(0.0), vec2(1.0))).r;
  float blue = texture2D(u_img, clamp(coord + radial * u_texel *
    u_chromaticAberrationBlueYellow * 6.0, vec2(0.0), vec2(1.0))).b;
  return vec3(red, base.g, blue);
}

vec3 srgbToLinear(vec3 c) {
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}
vec3 linearToSrgb(vec3 c) {
  c = max(c, 0.0);
  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}

// Sample a 256-entry table so it interpolates exactly like numpy's np.interp:
// texel i sits at (i + 0.5) / 256, so value v maps to (v * 255 + 0.5) / 256.
float curveAt(float v, int ch) {
  vec4 t = texture2D(u_curve, vec2((clamp(v, 0.0, 1.0) * 255.0 + 0.5) / 256.0, 0.5));
  if (ch == 0) return t.r;
  if (ch == 1) return t.g;
  if (ch == 2) return t.b;
  return t.a;
}

vec3 rgb2hsv(vec3 c) {
  float mx = max(c.r, max(c.g, c.b));
  float mn = min(c.r, min(c.g, c.b));
  float d = mx - mn;
  float h = 0.0;
  if (d > 1e-5) {
    if (mx == c.r)      h = mod((c.g - c.b) / d, 6.0);
    else if (mx == c.g) h = (c.b - c.r) / d + 2.0;
    else                h = (c.r - c.g) / d + 4.0;
    h = mod(h * 60.0, 360.0);
  }
  return vec3(h, mx > 1e-5 ? d / mx : 0.0, mx);
}

vec3 hsv2rgb(vec3 hsv) {
  float h = mod(hsv.x, 360.0) / 60.0;
  float s = hsv.y, v = hsv.z;
  int i = int(mod(floor(h), 6.0));
  float f = h - floor(h);
  float p = v * (1.0 - s);
  float q = v * (1.0 - s * f);
  float t = v * (1.0 - s * (1.0 - f));
  if (i == 0) return vec3(v, t, p);
  if (i == 1) return vec3(q, v, p);
  if (i == 2) return vec3(p, v, t);
  if (i == 3) return vec3(p, q, v);
  if (i == 4) return vec3(t, p, v);
  return vec3(v, p, q);
}

vec3 applyColorGradeTone(vec3 c, vec3 settings, float weight, float strength,
  float luminanceStrength) {
  vec3 tint = hsv2rgb(vec3(settings.x, 1.0, 1.0));
  vec3 chroma = tint - vec3(dot(tint, LUMA));
  return c + (chroma * settings.y * strength + vec3(settings.z * luminanceStrength)) * weight;
}

vec3 applySoftProof(vec3 c) {
  if (u_softProof.x < 0.5) return c;
  float gamutScale = 1.0;
  float blackPoint = 0.0;
  float paperWhite = 1.0;
  if (u_softProof.y > 2.5) {
    gamutScale = 0.86; blackPoint = 0.012; paperWhite = 0.96;
  } else if (u_softProof.y > 1.5) {
    gamutScale = 0.68; blackPoint = 0.032; paperWhite = 0.90;
  } else if (u_softProof.y > 0.5) {
    gamutScale = 1.15;
  }
  vec3 linear = srgbToLinear(c);
  float y = dot(linear, LUMA);
  vec3 targetCoordinates = vec3(y) + (linear - vec3(y)) / gamutScale;
  bool outside = min(targetCoordinates.r, min(targetCoordinates.g,
    targetCoordinates.b)) < 0.0 || max(targetCoordinates.r,
    max(targetCoordinates.g, targetCoordinates.b)) > 1.0;
  vec3 proof = vec3(y) + (linear - vec3(y)) * gamutScale;
  if (u_softProof.z > 0.5 && u_softProof.y > 1.5) {
    proof = vec3(blackPoint) + proof * (paperWhite - blackPoint);
  }
  c = clamp(linearToSrgb(proof), 0.0, 1.0);
  if (u_softProof.w > 0.5 && outside) {
    float stripe = step(0.5, fract((gl_FragCoord.x + gl_FragCoord.y) / 10.0));
    c = mix(c, vec3(1.0, 0.0, 0.72), 0.48 + stripe * 0.18);
  }
  return c;
}

vec3 localGrade(vec3 c, float exposure, float contrast,
  float highlights, float shadows, float temp, float tint, float saturation,
  float texture, float clarity, vec2 uv) {
  if (texture != 0.0 || clarity != 0.0) {
    vec3 blur = (c
      + sampleSource(uv + vec2(u_texel.x, 0.0))
      + sampleSource(uv - vec2(u_texel.x, 0.0))
      + sampleSource(uv + vec2(0.0, u_texel.y))
      + sampleSource(uv - vec2(0.0, u_texel.y))) / 5.0;
    vec3 detail = c - blur;
    c = clamp(c + detail * texture * 1.1, 0.0, 1.0);
    float middle = clamp(1.0 - abs(dot(c, LUMA) - 0.5) * 2.0, 0.0, 1.0);
    c = clamp(c + detail * clarity * 1.8 * middle, 0.0, 1.0);
  }
  vec3 lin = srgbToLinear(c) * pow(2.0, exposure);
  if (highlights != 0.0 || shadows != 0.0) {
    float y = max(dot(lin, LUMA), 0.0);
    if (highlights != 0.0) {
      float m = pow(clamp((y - 0.35) / 0.65, 0.0, 1.0), 1.2);
      lin *= 1.0 + highlights * 0.85 * m;
    }
    if (shadows != 0.0) {
      float m = pow(clamp((0.45 - y) / 0.45, 0.0, 1.0), 1.2);
      lin *= 1.0 + shadows * 1.5 * m;
    }
  }
  c = clamp(linearToSrgb(lin), 0.0, 1.0);
  if (contrast != 0.0) {
    if (contrast > 0.0) {
      vec3 shaped = c * c * (3.0 - 2.0 * c);
      c += (shaped - c) * contrast;
    } else {
      c = 0.5 + (c - 0.5) * (1.0 + contrast * 0.8);
    }
    c = clamp(c, 0.0, 1.0);
  }
  if (temp != 0.0 || tint != 0.0) {
    c = clamp(c * vec3(
      1.0 + temp * 0.18 + tint * 0.06,
      1.0 - tint * 0.12,
      1.0 - temp * 0.18 + tint * 0.06), 0.0, 1.0);
  }
  if (saturation != 0.0) {
    float y = dot(c, LUMA);
    c = clamp(vec3(y) + (c - vec3(y)) * (1.0 + saturation), 0.0, 1.0);
  }
  return c;
}

vec3 applyLocal(vec3 c, float geometric, float enabled, float opacity,
  float low, float high, float exposure, float contrast, float highlights,
  float shadows, float temp, float tint, float saturation, float texture,
  float clarity, vec4 colorRange, vec2 uv) {
  if (enabled < 0.5 || geometric <= 0.0) return c;
  float y = dot(c, LUMA);
  float lower = low <= 0.0 ? 1.0 : smoothstep(low - 0.04, low + 0.04, y);
  float upper = high >= 1.0 ? 1.0 : 1.0 - smoothstep(high - 0.04, high + 0.04, y);
  float colorWeight = 1.0;
  if (colorRange.w > 0.5) {
    vec3 hsv = rgb2hsv(c);
    float difference = abs(mod(hsv.x - colorRange.x + 180.0, 360.0) - 180.0);
    float selected = (1.0 - smoothstep(colorRange.y * 0.45,
      colorRange.y, difference)) * hsv.y;
    colorWeight = 1.0 - colorRange.z * (1.0 - selected);
  }
  float weight = clamp(geometric * opacity * lower * upper * colorWeight, 0.0, 1.0);
  return mix(c, localGrade(c, exposure, contrast, highlights, shadows,
    temp, tint, saturation, texture, clarity, uv), weight);
}

void main() {
  vec2 uv = v_uv * u_uvScale + u_uvOffset;
  if (u_originalReady > 0.5 && u_compare > 0.0 && uv.x <= u_compare) {
    gl_FragColor = vec4(texture2D(u_original, clamp(
      uv, vec2(0.0), vec2(1.0))).rgb, 1.0);
    return;
  }
  vec3 c = clamp(sampleSource(uv), 0.0, 1.0);

  if (u_luminanceNoise != 0.0 || u_colorNoise != 0.0) {
    vec3 blur = (c
      + sampleSource(uv + vec2(u_texel.x, 0.0))
      + sampleSource(uv - vec2(u_texel.x, 0.0))
      + sampleSource(uv + vec2(0.0, u_texel.y))
      + sampleSource(uv - vec2(0.0, u_texel.y))) / 5.0;
    float y = dot(c, LUMA);
    float blurY = dot(blur, LUMA);
    if (u_luminanceNoise != 0.0) {
      c = clamp(c + vec3(blurY - y) * u_luminanceNoise, 0.0, 1.0);
      y = dot(c, LUMA);
    }
    if (u_colorNoise != 0.0) {
      vec3 chroma = c - vec3(y);
      vec3 blurChroma = blur - vec3(blurY);
      c = clamp(vec3(y) + chroma * (1.0 - u_colorNoise)
        + blurChroma * u_colorNoise, 0.0, 1.0);
    }
  }

  // Compact local-detail controls. The cross blur uses exactly one source
  // pixel in each direction so the numpy exporter can mirror it precisely.
  if (u_texture != 0.0 || u_clarity != 0.0) {
    vec3 blur = (c
      + sampleSource(uv + vec2(u_texel.x, 0.0))
      + sampleSource(uv - vec2(u_texel.x, 0.0))
      + sampleSource(uv + vec2(0.0, u_texel.y))
      + sampleSource(uv - vec2(0.0, u_texel.y))) / 5.0;
    vec3 detail = c - blur;
    c = clamp(c + detail * u_texture * 1.1, 0.0, 1.0);
    float mid = clamp(1.0 - abs(dot(c, LUMA) - 0.5) * 2.0, 0.0, 1.0);
    c = clamp(c + detail * u_clarity * 1.8 * mid, 0.0, 1.0);
  }

  if (u_sharpness != 0.0) {
    vec2 radius = u_texel * u_sharpenRadius;
    vec3 blur = (c
      + sampleSource(uv + vec2(radius.x, 0.0))
      + sampleSource(uv - vec2(radius.x, 0.0))
      + sampleSource(uv + vec2(0.0, radius.y))
      + sampleSource(uv - vec2(0.0, radius.y))) / 5.0;
    vec3 detail = c - blur;
    float lumaDetail = dot(detail, LUMA);
    vec3 shaped = vec3(lumaDetail) + (detail - vec3(lumaDetail)) * u_sharpenDetail;
    float edge = length(detail);
    float edgeMask = smoothstep(0.015, 0.16, edge);
    float mask = mix(1.0, edgeMask, u_sharpenMasking);
    c = clamp(c + shaped * u_sharpness * 1.8 * mask, 0.0, 1.0);
  }

  vec3 lin = srgbToLinear(c);
  lin *= pow(2.0, u_exposure);

  if (u_highlights != 0.0 || u_shadows != 0.0) {
    float y = max(dot(lin, LUMA), 0.0);
    if (u_highlights != 0.0) {
      float m = pow(clamp((y - 0.35) / 0.65, 0.0, 1.0), 1.2);
      lin *= 1.0 + u_highlights * 0.85 * m;
    }
    if (u_shadows != 0.0) {
      float m = pow(clamp((0.45 - y) / 0.45, 0.0, 1.0), 1.2);
      lin *= 1.0 + u_shadows * 1.5 * m;
    }
  }
  c = clamp(linearToSrgb(lin), 0.0, 1.0);

  if (u_whites != 0.0 || u_blacks != 0.0) {
    float w = 1.0 + u_whites * 0.35;
    float b = u_blacks * -0.25;
    c = clamp((c - b) / max(w - b, 1e-4), 0.0, 1.0);
  }

  if (u_contrast != 0.0) {
    if (u_contrast > 0.0) {
      vec3 s = c * c * (3.0 - 2.0 * c);
      c = c + (s - c) * u_contrast;
    } else {
      c = 0.5 + (c - 0.5) * (1.0 + u_contrast * 0.8);
    }
    c = clamp(c, 0.0, 1.0);
  }

  if (u_dehaze != 0.0) {
    float haze = u_dehaze * 0.12;
    c = clamp((c - vec3(haze)) / max(1.0 - haze, 0.2), 0.0, 1.0);
    float y = dot(c, LUMA);
    c = clamp(vec3(y) + (c - vec3(y)) * (1.0 + u_dehaze * 0.18), 0.0, 1.0);
  }

  if (u_temp != 0.0 || u_tint != 0.0) {
    vec3 gain = vec3(
      1.0 + u_temp * 0.18 + u_tint * 0.06,
      1.0 - u_tint * 0.12,
      1.0 - u_temp * 0.18 + u_tint * 0.06);
    c = clamp(c * gain, 0.0, 1.0);
  }

  if (u_saturation != 0.0) {
    float y = dot(c, LUMA);
    c = clamp(vec3(y) + (c - vec3(y)) * (1.0 + u_saturation), 0.0, 1.0);
  }
  if (u_vibrance != 0.0) {
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    float sat = (mx - mn) / max(mx, 1e-4);
    float y = dot(c, LUMA);
    c = clamp(vec3(y) + (c - vec3(y)) * (1.0 + u_vibrance * (1.0 - sat)), 0.0, 1.0);
  }

  if (u_monochrome > 0.5) {
    vec3 hsv = rgb2hsv(c);
    float dl = 0.0;
    for (int i = 0; i < 8; i++) {
      float diff = abs(mod(hsv.x - hslCentre(i) + 180.0, 360.0) - 180.0);
      float w = clamp(1.0 - diff / 45.0, 0.0, 1.0);
      w = w * w * (3.0 - 2.0 * w) * hsv.y;
      dl += w * u_hsl[i].z * 0.5;
    }
    float luma = dot(c, LUMA) + dl;
    c = clamp(vec3(luma), 0.0, 1.0);
  } else if (u_hslOn > 0.5) {
    vec3 hsv = rgb2hsv(c);
    float dh = 0.0, ds = 0.0, dl = 0.0;
    for (int i = 0; i < 8; i++) {
      float diff = abs(mod(hsv.x - hslCentre(i) + 180.0, 360.0) - 180.0);
      float w = clamp(1.0 - diff / 45.0, 0.0, 1.0);
      w = w * w * (3.0 - 2.0 * w) * hsv.y;
      dh += w * u_hsl[i].x * 30.0;
      ds += w * u_hsl[i].y;
      dl += w * u_hsl[i].z;
    }
    hsv.x = mod(hsv.x + dh, 360.0);
    hsv.y = clamp(hsv.y * (1.0 + ds), 0.0, 1.0);
    hsv.z = clamp(hsv.z * (1.0 + dl * 0.5), 0.0, 1.0);
    c = clamp(hsv2rgb(hsv), 0.0, 1.0);
  }

  if (u_pointColorCount > 0.5) {
    vec3 hsv = rgb2hsv(c);
    for (int i = 0; i < 8; i++) {
      if (float(i) >= u_pointColorCount) break;
      vec4 point = u_pointColor[i];
      float difference = abs(mod(hsv.x - point.x + 180.0, 360.0) - 180.0);
      float weight = (1.0 - smoothstep(point.y * 0.45, point.y, difference)) * hsv.y;
      vec4 uniformity = u_pointUniform[i];
      vec2 reference = u_pointRefLuminance[i];
      if (reference.y > 0.5) {
        float shortest = mod(point.x - hsv.x + 540.0, 360.0) - 180.0;
        hsv.x = mod(hsv.x + shortest * uniformity.x * weight + 360.0, 360.0);
        hsv.y = clamp(hsv.y + (uniformity.w - hsv.y) * uniformity.y * weight,
          0.0, 1.0);
        hsv.z = clamp(hsv.z + (reference.x - hsv.z) * uniformity.z * weight,
          0.0, 1.0);
      }
      hsv.x = mod(hsv.x + point.z * weight, 360.0);
      hsv.y = clamp(hsv.y * (1.0 + point.w * weight), 0.0, 1.0);
      hsv.z = clamp(hsv.z * (1.0 + u_pointLuminance[i] * 0.5 * weight), 0.0, 1.0);
    }
    c = clamp(hsv2rgb(hsv), 0.0, 1.0);
  }

  if (u_cgOn > 0.5) {
    float y = dot(c, LUMA);
    float shift = u_cgBalance * 0.2;
    float width = 0.18 + u_cgBlending * 0.22;
    float shadows = 1.0 - smoothstep(0.28 + shift - width,
      0.28 + shift + width, y);
    float highlights = smoothstep(0.72 + shift - width,
      0.72 + shift + width, y);
    float midtones = clamp(1.0 - shadows - highlights, 0.0, 1.0);
    c = applyColorGradeTone(c, u_cgShadows, shadows, 0.28, 0.22);
    c = applyColorGradeTone(c, u_cgMidtones, midtones, 0.28, 0.22);
    c = applyColorGradeTone(c, u_cgHighlights, highlights, 0.28, 0.22);
    c = applyColorGradeTone(c, u_cgGlobal, 1.0, 0.2, 0.18);
    c = clamp(c, 0.0, 1.0);
  }

  if (u_curveOn.x > 0.5) {
    c = vec3(curveAt(c.r, 0), curveAt(c.g, 0), curveAt(c.b, 0));
  }
  if (u_curveOn.y > 0.5) c.r = curveAt(c.r, 1);
  if (u_curveOn.z > 0.5) c.g = curveAt(c.g, 2);
  if (u_curveOn.w > 0.5) c.b = curveAt(c.b, 3);

  if (u_vignette != 0.0) {
    vec2 n = (uv - 0.5) * 2.0;
    float r = length(n) / 1.4142;
    if (u_vignetteSize != 0.5 || u_vignetteFeather != 1.0) {
      // Match export's pixel endpoints, especially at a nearly hard edge.
      vec2 position = (uv - 0.5 * u_texel) / max(vec2(1.0) - u_texel, u_texel);
      r = length((position - 0.5) * 2.0) / 1.4142;
      float outer = 0.25 + 1.5 * u_vignetteSize;
      float width = outer * max(u_vignetteFeather, 0.01);
      r = clamp((r - outer + width) / width, 0.0, 1.0);
    }
    c = clamp(c * clamp(1.0 - u_vignette * 0.9 * pow(r, 2.2), 0.0, 2.0), 0.0, 1.0);
  }

  for (int tile = 0; tile < 4; tile++) {
    if (float(tile) >= u_maskTiles) break;
    vec4 masks = texture2D(u_masks, vec2(uv.x,
      (uv.y + float(tile)) / u_maskTiles));
    c = applyLocal(c, masks.r, u_localOn[tile].x, u_localOpacity[tile].x,
      u_localLumaLow[tile].x, u_localLumaHigh[tile].x, u_localExposure[tile].x,
      u_localContrast[tile].x, u_localHighlights[tile].x, u_localShadows[tile].x,
      u_localTemp[tile].x, u_localTint[tile].x, u_localSaturation[tile].x,
      u_localDetail[tile].x, u_localClarity[tile].x, u_localColorRange[tile], uv);
    c = applyLocal(c, masks.g, u_localOn[tile].y, u_localOpacity[tile].y,
      u_localLumaLow[tile].y, u_localLumaHigh[tile].y, u_localExposure[tile].y,
      u_localContrast[tile].y, u_localHighlights[tile].y, u_localShadows[tile].y,
      u_localTemp[tile].y, u_localTint[tile].y, u_localSaturation[tile].y,
      u_localDetail[tile].y, u_localClarity[tile].y, u_localColorRangeB[tile], uv);
    c = applyLocal(c, masks.b, u_localOn[tile].z, u_localOpacity[tile].z,
      u_localLumaLow[tile].z, u_localLumaHigh[tile].z, u_localExposure[tile].z,
      u_localContrast[tile].z, u_localHighlights[tile].z, u_localShadows[tile].z,
      u_localTemp[tile].z, u_localTint[tile].z, u_localSaturation[tile].z,
      u_localDetail[tile].z, u_localClarity[tile].z, u_localColorRangeC[tile], uv);
    c = applyLocal(c, masks.a, u_localOn[tile].w, u_localOpacity[tile].w,
      u_localLumaLow[tile].w, u_localLumaHigh[tile].w, u_localExposure[tile].w,
      u_localContrast[tile].w, u_localHighlights[tile].w, u_localShadows[tile].w,
      u_localTemp[tile].w, u_localTint[tile].w, u_localSaturation[tile].w,
      u_localDetail[tile].w, u_localClarity[tile].w, u_localColorRangeD[tile], uv);
  }

  c = applySoftProof(c);

  if (u_spotVisualization.x > 0.5) {
    float threshold = u_spotVisualization.y;
    float value = (0.5 - dot(c, LUMA)) * (2.0 + threshold * 7.0) + 0.5;
    c = vec3(clamp(clamp(value, 0.0, 1.0) * (0.72 + threshold * 0.35), 0.0, 1.0));
  }

  if (u_clippingOverlay > 0.5) {
    if (c.r <= 0.005 && c.g <= 0.005 && c.b <= 0.005) {
      c = vec3(0.0, 0.2, 1.0);
    } else if (c.r >= 0.995 || c.g >= 0.995 || c.b >= 0.995) {
      c = vec3(1.0, 0.0, 0.0);
    }
  }

  if (u_referenceView.x > 0.5) {
    vec2 referenceUv = (uv - vec2(0.5) - u_referenceOffset)
      / max(u_referenceView.w, 0.01) + vec2(0.5);
    float inside = step(0.0, referenceUv.x) * step(referenceUv.x, 1.0)
      * step(0.0, referenceUv.y) * step(referenceUv.y, 1.0);
    float weight = u_referenceView.y > 0.5
      ? (1.0 - step(u_referenceView.z, uv.x))
      : u_referenceView.z;
    vec3 referenceColor = texture2D(u_reference, clamp(referenceUv, 0.0, 1.0)).rgb;
    c = mix(c, referenceColor, clamp(weight * inside, 0.0, 1.0));
  }

  gl_FragColor = vec4(c, 1.0);
}`;

function compile(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    throw new Error(tr("shader: {value}", {value: gl.getShaderInfoLog(sh)}));
  }
  return sh;
}

export class GradeRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext('webgl', {
      preserveDrawingBuffer: false, antialias: false, alpha: false,
    });
    if (!this.gl) throw new Error(tr("WebGL unavailable"));
    const gl = this.gl;
    this.prog = gl.createProgram();
    gl.attachShader(this.prog, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(this.prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(this.prog);
    if (!gl.getProgramParameter(this.prog, gl.LINK_STATUS)) {
      throw new Error(tr("link: {value}", {value: gl.getProgramInfoLog(this.prog)}));
    }
    gl.useProgram(this.prog);

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(this.prog, 'a_pos');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

    this.tex = null;
    this.imageTextures = new Map();
    this.imageTextureBytes = 0;
    // Count RGB uploads as RGBA: drivers may store four bytes per pixel.
    // One displayed image may exceed the budget, but then nothing else is kept.
    this.maxImageTextureBytes = 256 * 1024 * 1024;
    this.maxImageTextures = 6;
    canvas.addEventListener('webglcontextlost', () => {
      this.imageTextures.clear();
      this.imageTextureBytes = 0;
      this.ready = false;
    });

    this.originalTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.originalTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA,
      gl.UNSIGNED_BYTE, new Uint8Array(4));
    this.originalReady = false;
    this.comparePosition = 0;

    this.referenceTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.referenceTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA,
      gl.UNSIGNED_BYTE, new Uint8Array(4));
    this.referenceReady = false;
    this.referenceSettings = { active: false, mode: 'overlay', amount: 0.5,
      scale: 1, x: 0, y: 0 };

    // 256x1 RGBA table: R = luma curve, G/B/A = per-channel curves.
    this.curveTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.curveTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    this.curveData = new Uint8Array(256 * 4);
    this.uploadCurves(null);

    this.maskTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.maskTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA,
      gl.UNSIGNED_BYTE, new Uint8Array(4));

    this.uCurve = gl.getUniformLocation(this.prog, 'u_curve');
    this.uCurveOn = gl.getUniformLocation(this.prog, 'u_curveOn');
    this.uHslOn = gl.getUniformLocation(this.prog, 'u_hslOn');
    this.uHsl = [];
    for (let i = 0; i < 8; i++) {
      this.uHsl.push(gl.getUniformLocation(this.prog, `u_hsl[${i}]`));
    }
    this.uPointColor = [];
    this.uPointLuminance = [];
    this.uPointUniform = [];
    this.uPointRefLuminance = [];
    for (let i = 0; i < 8; i++) {
      this.uPointColor.push(gl.getUniformLocation(this.prog, `u_pointColor[${i}]`));
      this.uPointLuminance.push(gl.getUniformLocation(this.prog, `u_pointLuminance[${i}]`));
      this.uPointUniform.push(gl.getUniformLocation(this.prog, `u_pointUniform[${i}]`));
      this.uPointRefLuminance.push(gl.getUniformLocation(
        this.prog, `u_pointRefLuminance[${i}]`));
    }
    this.uPointColorCount = gl.getUniformLocation(this.prog, 'u_pointColorCount');
    this.uCg = Object.fromEntries(['Shadows', 'Midtones', 'Highlights', 'Global',
      'Balance', 'Blending', 'On'].map((key) => [key,
      gl.getUniformLocation(this.prog, `u_cg${key}`)]));
    this.uSoftProof = gl.getUniformLocation(this.prog, 'u_softProof');
    this.uSpotVisualization = gl.getUniformLocation(this.prog, 'u_spotVisualization');
    this.spotVisualization = [0, 0];
    this.uClippingOverlay = gl.getUniformLocation(this.prog, 'u_clippingOverlay');
    this.clippingOverlay = 0;
    this.uImg = gl.getUniformLocation(this.prog, 'u_img');
    this.uOriginal = gl.getUniformLocation(this.prog, 'u_original');
    this.uReference = gl.getUniformLocation(this.prog, 'u_reference');
    this.uReferenceView = gl.getUniformLocation(this.prog, 'u_referenceView');
    this.uReferenceOffset = gl.getUniformLocation(this.prog, 'u_referenceOffset');
    this.uCompare = gl.getUniformLocation(this.prog, 'u_compare');
    this.uOriginalReady = gl.getUniformLocation(this.prog, 'u_originalReady');
    this.uMasks = gl.getUniformLocation(this.prog, 'u_masks');
    this.uMaskTiles = gl.getUniformLocation(this.prog, 'u_maskTiles');
    this.uTexel = gl.getUniformLocation(this.prog, 'u_texel');
    this.uUvScale = gl.getUniformLocation(this.prog, 'u_uvScale');
    this.uUvOffset = gl.getUniformLocation(this.prog, 'u_uvOffset');
    this.localUniforms = {};
    for (const key of ['On', 'Opacity', 'LumaLow', 'LumaHigh', 'Exposure',
      'Contrast', 'Highlights', 'Shadows', 'Temp', 'Tint', 'Saturation',
      'Detail', 'Clarity', 'ColorRange', 'ColorRangeB', 'ColorRangeC',
      'ColorRangeD']) {
      this.localUniforms[key] = [];
      for (let tile = 0; tile < 4; tile++) {
        this.localUniforms[key].push(gl.getUniformLocation(
          this.prog, `u_local${key}[${tile}]`));
      }
    }

    this.uniforms = {};
    for (const k of Object.keys(GRADE_DEFAULTS)) {
      this.uniforms[k] = gl.getUniformLocation(this.prog, 'u_' + k);
    }
    this.sampleWidth = 1;
    this.sampleHeight = 1;
    this.samplePixels = new Uint8Array(4);
    this.sampleTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.sampleTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0,
      gl.RGBA, gl.UNSIGNED_BYTE, null);
    this.sampleFramebuffer = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.sampleFramebuffer);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0,
      gl.TEXTURE_2D, this.sampleTexture, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    this.ready = false;
  }

  /** Pack up to four 256-entry tables into the LUT texture. */
  uploadCurves(grade) {
    const gl = this.gl;
    const keys = ['curveL', 'curveR', 'curveG', 'curveB'];
    const references = keys.map((key) => grade && grade[key]);
    if (this._curveRefs && references.every((value, index) =>
      value === this._curveRefs[index])) return false;
    this._curveRefs = references;
    const on = [0, 0, 0, 0];
    for (let i = 0; i < 256; i++) {
      for (let c = 0; c < 4; c++) {
        const t = grade && grade[keys[c]];
        this.curveData[i * 4 + c] = t ? Math.round(Math.max(0, Math.min(1, t[i])) * 255) : i;
      }
    }
    keys.forEach((k, c) => { on[c] = grade && grade[k] ? 1 : 0; });
    gl.bindTexture(gl.TEXTURE_2D, this.curveTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA,
                  gl.UNSIGNED_BYTE, this.curveData);
    this._curveOn = on;
    return true;
  }

  cachedImage(key) {
    return key ? this.imageTextures.get(key)?.image : null;
  }

  setImage(img, { resizeCanvas = true, cacheKey = null } = {}) {
    const gl = this.gl;
    const imageWidth = img.naturalWidth || img.width;
    const imageHeight = img.naturalHeight || img.height;
    const key = cacheKey || Symbol('uncached preview');
    let entry = this.imageTextures.get(key);
    const textureCacheHit = Boolean(entry);
    if (entry) {
      this.imageTextures.delete(key);
    } else {
      const texture = gl.createTexture();
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
      entry = { texture, image: img, bytes: imageWidth * imageHeight * 4 };
      this.imageTextureBytes += entry.bytes;
    }
    this.imageTextures.set(key, entry);
    this.tex = entry.texture;
    while (this.imageTextures.size > 1 &&
        (this.imageTextureBytes > this.maxImageTextureBytes ||
         this.imageTextures.size > this.maxImageTextures)) {
      const oldest = this.imageTextures.keys().next().value;
      const removed = this.imageTextures.get(oldest);
      this.imageTextures.delete(oldest);
      this.imageTextureBytes -= removed.bytes;
      gl.deleteTexture(removed.texture);
    }
    if (resizeCanvas &&
        (this.canvas.width !== imageWidth || this.canvas.height !== imageHeight)) {
      this.canvas.width = imageWidth;
      this.canvas.height = imageHeight;
    }
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.useProgram(this.prog);
    gl.uniform2f(this.uTexel, 1 / imageWidth, 1 / imageHeight);
    const scale = Math.min(1, 128 / Math.max(imageWidth, imageHeight));
    const sampleWidth = Math.max(1, Math.round(imageWidth * scale));
    const sampleHeight = Math.max(1, Math.round(imageHeight * scale));
    if (this.sampleWidth !== sampleWidth || this.sampleHeight !== sampleHeight) {
      this.sampleWidth = sampleWidth;
      this.sampleHeight = sampleHeight;
      this.samplePixels = new Uint8Array(sampleWidth * sampleHeight * 4);
      gl.bindTexture(gl.TEXTURE_2D, this.sampleTexture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, sampleWidth,
        sampleHeight, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    }
    this.ready = true;
    return { textureCacheHit };
  }

  setOriginalImage(img) {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.originalTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB,
      gl.UNSIGNED_BYTE, img);
    gl.activeTexture(gl.TEXTURE0);
    this.originalReady = true;
  }

  clearOriginalImage() {
    this.originalReady = false;
    this.comparePosition = 0;
  }

  setReferenceImage(img) {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, this.referenceTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB,
      gl.UNSIGNED_BYTE, img);
    gl.activeTexture(gl.TEXTURE0);
    this.referenceReady = true;
  }

  clearReferenceImage() {
    this.referenceReady = false;
    this.referenceSettings = { ...this.referenceSettings, active: false };
  }

  setReferenceSettings(settings) {
    this.referenceSettings = settings;
  }

  applyReferenceUniforms(settings = this.referenceSettings) {
    const gl = this.gl;
    this.referenceSettings = settings;
    const active = settings?.active && this.referenceReady ? 1 : 0;
    gl.uniform4f(this.uReferenceView, active,
      settings?.mode === 'split' ? 1 : 0,
      Math.max(0, Math.min(1, +settings?.amount || 0)),
      Math.max(0.01, +settings?.scale || 1));
    gl.uniform2f(this.uReferenceOffset, +settings?.x || 0, +settings?.y || 0);
  }

  drawReference(settings) {
    this.referenceSettings = settings;
    if (!this.ready) return;
    const gl = this.gl;
    gl.useProgram(this.prog);
    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, this.referenceTex);
    gl.uniform1i(this.uReference, 4);
    gl.activeTexture(gl.TEXTURE0);
    this.applyReferenceUniforms(settings);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  drawCompare(position) {
    if (!this.ready) return false;
    const gl = this.gl;
    this.comparePosition = Math.max(0, Math.min(1, +position || 0));
    gl.useProgram(this.prog);
    gl.uniform1f(this.uCompare, this.comparePosition);
    gl.uniform1f(this.uOriginalReady, this.originalReady ? 1 : 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.uniform2f(this.uUvScale, 1, 1);
    gl.uniform2f(this.uUvOffset, 0, 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return this.originalReady;
  }

  uploadMasks(canvas) {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.maskTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    if (canvas) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA,
        gl.UNSIGNED_BYTE, canvas);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA,
        gl.UNSIGNED_BYTE, new Uint8Array(4));
    }
  }

  draw(grade, localEdits = [], maskCanvas = undefined, softProof = null, spotVisualization = null) {
    if (!this.ready) return;
    const gl = this.gl;
    gl.useProgram(this.prog);
    this.spotVisualization = [spotVisualization?.enabled ? 1 : 0,
      Math.max(0, Math.min(1, +spotVisualization?.threshold || 0))];
    gl.uniform2f(this.uSpotVisualization, ...this.spotVisualization);
    this.clippingOverlay = spotVisualization?.clipping ? 1 : 0;
    gl.uniform1f(this.uClippingOverlay, this.clippingOverlay);
    for (const [k, def] of Object.entries(GRADE_DEFAULTS)) {
      const v = grade && grade[k] !== undefined ? +grade[k] : def;
      gl.uniform1f(this.uniforms[k], v);
    }

    this.uploadCurves(grade);
    gl.uniform4f(this.uCurveOn, ...this._curveOn);

    const hsl = (grade && grade.hsl) || null;
    gl.uniform1f(this.uHslOn, hsl ? 1 : 0);
    HSL_BANDS.forEach((b, i) => {
      const e = (hsl && hsl[b]) || { h: 0, s: 0, l: 0 };
      gl.uniform3f(this.uHsl[i], +e.h || 0, +e.s || 0, +e.l || 0);
    });

    const points = Array.isArray(grade?.pointColor) ? grade.pointColor.slice(0, 8) : [];
    gl.uniform1f(this.uPointColorCount, points.length);
    for (let i = 0; i < 8; i++) {
      const point = points[i] || {};
      gl.uniform4f(this.uPointColor[i], +point.hue || 0, +point.range || 30,
        +point.hueShift || 0, +point.saturation || 0);
      gl.uniform1f(this.uPointLuminance[i], +point.luminance || 0);
      gl.uniform4f(this.uPointUniform[i], +point.uniformHue || 0,
        +point.uniformSaturation || 0, +point.uniformLuminance || 0,
        +point.refSaturation || 0);
      gl.uniform2f(this.uPointRefLuminance[i], +point.refLuminance || 0,
        point.refSaturation == null || point.refLuminance == null ? 0 : 1);
    }
    const grading = grade?.colorGrading;
    gl.uniform1f(this.uCg.On, grading ? 1 : 0);
    for (const tone of ['shadows', 'midtones', 'highlights', 'global']) {
      const item = grading?.[tone] || {};
      const key = tone[0].toUpperCase() + tone.slice(1);
      gl.uniform3f(this.uCg[key], +item.hue || 0, +item.saturation || 0,
        +item.luminance || 0);
    }
    gl.uniform1f(this.uCg.Balance, +grading?.balance || 0);
    gl.uniform1f(this.uCg.Blending, grading?.blending === undefined
      ? 0.5 : +grading.blending);
    const proofTargets = { srgb: 0, display_p3: 1, matte: 2, gloss: 3 };
    gl.uniform4f(this.uSoftProof, softProof?.enabled ? 1 : 0,
      proofTargets[softProof?.profile] ?? 0, softProof?.paper ? 1 : 0,
      softProof?.gamut ? 1 : 0);

    const values = (key, tile, fallback = 0) => [0, 1, 2, 3].map((channel) => {
      const edit = localEdits[tile * 4 + channel];
      if (!edit) return fallback;
      if (key === 'On') return edit.enabled === false ? 0 : 1;
      if (key === 'Opacity') return +edit.opacity;
      if (key === 'LumaLow') return +edit.lumaLow;
      if (key === 'LumaHigh') return +edit.lumaHigh;
      if (key === 'Detail') return +(edit.grade && edit.grade.texture) || 0;
      if (key === 'Clarity') return +(edit.grade && edit.grade.clarity) || 0;
      const gradeKey = key[0].toLowerCase() + key.slice(1);
      return +(edit.grade && edit.grade[gradeKey]) || 0;
    });
    const rangeValue = (edit) => [
      +edit?.colorHue || 0, +edit?.colorRange || 30,
      edit?.colorAmount == null ? 1 : +edit.colorAmount,
      edit?.colorHue == null ? 0 : 1,
    ];
    for (let tile = 0; tile < 4; tile++) {
      for (const key of ['On', 'Opacity', 'LumaLow', 'LumaHigh', 'Exposure',
        'Contrast', 'Highlights', 'Shadows', 'Temp', 'Tint', 'Saturation',
        'Detail', 'Clarity']) {
        const fallback = key === 'Opacity' || key === 'LumaHigh' ? 1 : 0;
        gl.uniform4f(this.localUniforms[key][tile], ...values(key, tile, fallback));
      }
      ['ColorRange', 'ColorRangeB', 'ColorRangeC', 'ColorRangeD']
        .forEach((key, channel) => gl.uniform4f(
          this.localUniforms[key][tile], ...rangeValue(localEdits[tile * 4 + channel])));
    }
    gl.uniform1f(this.uMaskTiles,
      Math.max(1, Math.ceil(Math.min(localEdits.length, 16) / 4)));
    if (maskCanvas !== undefined) this.uploadMasks(maskCanvas);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.uniform1i(this.uImg, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.curveTex);
    gl.uniform1i(this.uCurve, 1);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.maskTex);
    gl.uniform1i(this.uMasks, 2);
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.originalTex);
    gl.uniform1i(this.uOriginal, 3);
    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, this.referenceTex);
    gl.uniform1i(this.uReference, 4);
    gl.activeTexture(gl.TEXTURE0);
    gl.uniform1f(this.uCompare, this.comparePosition);
    gl.uniform1f(this.uOriginalReady, this.originalReady ? 1 : 0);
    gl.uniform2f(this.uUvScale, 1, 1);
    gl.uniform2f(this.uUvOffset, 0, 0);
    this.applyReferenceUniforms();

    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  /** Render a tiny histogram surface and read 64 KiB instead of the full canvas. */
  sample() {
    if (!this.ready) return null;
    const gl = this.gl;
    gl.uniform2f(this.uSpotVisualization, 0, 0);
    gl.uniform1f(this.uClippingOverlay, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.sampleFramebuffer);
    gl.viewport(0, 0, this.sampleWidth, this.sampleHeight);
    gl.uniform2f(this.uUvScale, 1, 1);
    gl.uniform2f(this.uUvOffset, 0, 0);
    gl.uniform1f(this.uCompare, 0);
    gl.uniform4f(this.uReferenceView, 0, 0, 0, 1);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.readPixels(0, 0, this.sampleWidth, this.sampleHeight,
      gl.RGBA, gl.UNSIGNED_BYTE, this.samplePixels);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.uniform1f(this.uCompare, this.comparePosition);
    this.applyReferenceUniforms();
    gl.uniform2f(this.uSpotVisualization, ...this.spotVisualization);
    gl.uniform1f(this.uClippingOverlay, this.clippingOverlay);
    return { px: this.samplePixels, w: this.sampleWidth, h: this.sampleHeight };
  }

  /** Read one graded pixel without retaining or downloading the canvas. */
  samplePixel(u, v) {
    if (!this.ready) return null;
    const gl = this.gl, pixel = new Uint8Array(4);
    gl.uniform2f(this.uSpotVisualization, 0, 0);
    gl.uniform1f(this.uClippingOverlay, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.sampleFramebuffer);
    gl.viewport(0, 0, 1, 1);
    gl.uniform2f(this.uUvScale, 0, 0);
    gl.uniform2f(this.uUvOffset, u, v);
    gl.uniform1f(this.uCompare, 0);
    gl.uniform4f(this.uReferenceView, 0, 0, 0, 1);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
    gl.uniform2f(this.uUvScale, 1, 1);
    gl.uniform2f(this.uUvOffset, 0, 0);
    gl.uniform1f(this.uCompare, this.comparePosition);
    this.applyReferenceUniforms();
    gl.uniform2f(this.uSpotVisualization, ...this.spotVisualization);
    gl.uniform1f(this.uClippingOverlay, this.clippingOverlay);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    return pixel;
  }
}
