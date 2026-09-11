// SPDX-License-Identifier: GPL-3.0-only
// Range inputs round programmatic values to their step (and clamp to bounds).
// Keep the model's precision when merely reading back an unchanged control.
const displayedValues = new WeakMap();

export function syncNumericControl(input, value) {
  input.value = String(value);
  displayedValues.set(input, input.value);
}

export function readNumericControl(input, current) {
  return displayedValues.get(input) === input.value ? current : +input.value;
}
