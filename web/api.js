// SPDX-License-Identifier: GPL-3.0-only
export function api(path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (window.__LIGHTTABLE_CLIENT_ID__) {
    headers['X-LightTable-Client'] = window.__LIGHTTABLE_CLIENT_ID__;
  }
  return fetch(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  }).then((response) => response.json());
}
