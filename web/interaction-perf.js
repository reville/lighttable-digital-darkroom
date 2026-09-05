// Opt-in input-to-presentation measurements. Production keeps this inert.
export function createInteractionRecorder(enabled = false, clock = () => performance.now()) {
  let nextId = 0;
  let pending = null;
  let scenario = null;
  const samples = [];
  const inputs = [];
  return {
    samples, inputs,
    start(name) { scenario = name; pending = null; samples.length = 0; inputs.length = 0; },
    stop() { scenario = null; pending = null; },
    input(generation) {
      if (!enabled || !scenario) return;
      pending = { id: ++nextId, inputAt: clock(), generation, scenario };
      inputs.push(pending);
      if (inputs.length > 2000) inputs.shift();
    },
    take() { const value = pending; pending = null; return value; },
    presented(input, backend, detail = {}) {
      if (!enabled || !input || input.scenario !== scenario) return;
      if (input.measurement === 'drawable-presented') {
        const completed = samples.find((sample) => sample.id === input.id);
        if (completed) { completed.presentedAckMs = clock() - input.inputAt; return; }
      }
      if (samples.some((sample) => sample.id === input.id)) return;
      samples.push({ ...input, ...detail, backend,
        acknowledgedAt: clock(), latencyMs: clock() - input.inputAt });
      if (samples.length > 2000) samples.shift();
    },
    snapshot() {
      const values = samples.map((entry) => entry.latencyMs).sort((a, b) => a - b);
      const percentile = (p) => values.length
        ? values[Math.min(values.length - 1, Math.ceil(values.length * p) - 1)] : null;
      return { scenario, inputs: inputs.length, presented: samples.length,
        coalescedInputs: Math.max(0, inputs.length - samples.length),
        medianMs: percentile(0.5), p95Ms: percentile(0.95), p99Ms: percentile(0.99),
        maxMs: values.at(-1) ?? null, samples: samples.map((sample) => ({ ...sample })) };
    },
  };
}
