const PREVIEW_RESOLUTIONS = new Set([
  'auto', '900', '1100', '1400', '1800', '2200', '2600', '3000',
  '3500', '4000', '4500', '5000',
]);

export function previewResolutionPreference(prefs = {}) {
  // Legacy `pw` values predate automatic previews as the default. Only an
  // explicit choice in Advanced should keep a fixed resolution after upgrade.
  const value = String(prefs?.previewResolution ?? 'auto');
  return PREVIEW_RESOLUTIONS.has(value) ? value : 'auto';
}
