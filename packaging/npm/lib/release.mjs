// SPDX-License-Identifier: GPL-3.0-only
export const REPOSITORY = 'reville/lighttable-digital-darkroom';
export const VERSION_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/;
export const PLATFORMS = {
  'darwin-arm64': { suffix: 'macos-arm64.zip' },
  'win32-x64': { suffix: 'windows-x64-setup.exe' },
};

export function assetName(version, platform) {
  if (!VERSION_RE.test(version ?? '') || !PLATFORMS[platform]) throw new Error('Invalid LightTable release version or platform.');
  return `LightTable-${version}-${PLATFORMS[platform].suffix}`;
}

export function releaseAsset(metadata, platform) {
  if (!PLATFORMS[platform]) throw new Error(`LightTable desktop installation is not supported on ${platform}. Supported: macOS Apple silicon and Windows x64.`);
  if (metadata?.schema !== 1 || !metadata.version) throw new Error('A desktop release is not yet published in this npm package. Check https://github.com/reville/lighttable-digital-darkroom/releases.');
  const name = assetName(metadata.version, platform);
  const entry = metadata.platforms?.[platform];
  if (!entry) throw new Error(`This npm release does not include a verified desktop installer for ${platform}. Check the GitHub releases page.`);
  if (!/^[a-f0-9]{64}$/.test(entry.sha256 ?? '') || /^0{64}$/.test(entry.sha256)) throw new Error('The packaged LightTable release checksum is invalid. Reinstall the npm package.');
  return { name, sha256: entry.sha256, url: `https://github.com/${REPOSITORY}/releases/download/v${metadata.version}/${name}` };
}
