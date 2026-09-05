export function nativeBridge() {
  return window.lightTableNativeBridge ||
    window.webkit?.messageHandlers?.lightTable || null;
}

export function sendNative(action, detail = {}) {
  const bridge = nativeBridge();
  if (!bridge) return false;
  bridge.postMessage({ action, ...detail });
  return true;
}
