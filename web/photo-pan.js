/** Per-window inspection positions, independent of zoom and photo edits. */
export function createPhotoPanMemory() {
  const positions = new Map();
  return {
    remember(key, view, frame) {
      if (!key || view.viewMode !== 'detail' || view.cropping || view.cropTransition ||
          ['fit', 'crop'].includes(view.zoomMode) || !(frame.width > 0 && frame.height > 0)) return;
      const x = -view.panX / frame.width, y = -view.panY / frame.height;
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      // Normalized offsets keep the inspected area stable at a different zoom.
      positions.set(key, {x, y});
    },
    restore(key, view, frame) {
      if (view.cropping || view.cropTransition) return;
      const position = view.zoomMode === 'fit' ? null : positions.get(key);
      view.panX = position && frame.width > 0 ? -position.x * frame.width : 0;
      view.panY = position && frame.height > 0 ? -position.y * frame.height : 0;
    },
  };
}
