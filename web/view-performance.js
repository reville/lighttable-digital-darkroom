/* Pure viewport math. Layout runs on membership/geometry changes; scrolling only
 * binary-searches each column and visits the cells that can actually be seen. */
export function createGridLayout(images, { width, cell = 180, photo = false }) {
  const gap = photo ? 6 : 12;
  width = Math.max(1, width);
  const columns = Math.max(1, Math.floor((width + gap) / (cell + gap)));
  const cellWidth = (width - gap * (columns - 1)) / columns;
  const lanes = Array.from({ length: columns }, () => []);
  const bottoms = Array(columns).fill(0);
  const positions = images.map((image, index) => {
    const column = photo ? bottoms.indexOf(Math.min(...bottoms)) : index % columns;
    const ratio = image.width > 0 && image.height > 0 ? image.height / image.width : (image.thumbnailAspectRatio || 1);
    const height = photo ? cellWidth * ratio : cellWidth + 44;
    const top = photo ? bottoms[column] : Math.floor(index / columns) * (height + gap);
    const position = { index, left: column * (cellWidth + gap), top, width: cellWidth,
      height, bottom: top + height };
    lanes[column].push(position);
    bottoms[column] = position.bottom + gap;
    return position;
  });
  return { positions, lanes, height: Math.max(0, ...bottoms) - (images.length ? gap : 0) };
}

export function visibleGridPositions(layout, top, height, overscan = 500) {
  const low = Math.max(0, top - overscan), high = top + height + overscan;
  const visible = [];
  for (const lane of layout.lanes) {
    let left = 0, right = lane.length;
    while (left < right) {
      const mid = (left + right) >>> 1;
      if (lane[mid].bottom < low) left = mid + 1;
      else right = mid;
    }
    for (let i = left; i < lane.length && lane[i].top <= high; i++) visible.push(lane[i]);
  }
  return visible.sort((a, b) => a.index - b.index);
}

const PREVIEW_BUCKETS = [900, 1100, 1400, 1800, 2200, 2600, 3000, 3500, 4000, 4500, 5000, 6000, 7000, 8000];
export function automaticPreviewWidth({ sourceWidth, sourceHeight, viewportWidth,
  viewportHeight, deviceScale = 1, zoom = 1, crop = null, actualSize = false }) {
  const source = Math.max(sourceWidth || 0, sourceHeight || 0);
  if (actualSize) return Math.max(64, Math.min(8000, source || 1100));
  if (!(source > 0 && viewportWidth > 0 && viewportHeight > 0)) return 1100;
  const fit = Math.min(viewportWidth / (sourceWidth * (crop?.w || 1)), viewportHeight / (sourceHeight * (crop?.h || 1)));
  const needed = Math.min(source, source * fit * Math.max(1, deviceScale) * Math.max(1, zoom));
  const bucket = PREVIEW_BUCKETS.find((size) => size >= needed) || 8000;
  return Math.max(64, Math.min(source, bucket));
}

/* Callers choose a cheap revision/scope key. Cache hits never visit catalog rows. */
export function createSummaryCache() {
  let lastKey, lastImages, value;
  return (key, images, compute) => {
    if (key !== lastKey || images !== lastImages || !value) {
      value = compute(); lastKey = key; lastImages = images;
    }
    return value;
  };
}
