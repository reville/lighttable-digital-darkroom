# Windows support: macOS regression check

Date: 2026-09-02<br>
Machine: macOS 26.6.2, arm64, Python 3.13.12<br>
Baseline: `4c34bf465a09ab2531ab3f734a0838c3336d70fc`<br>
Candidate: the uncommitted Windows-support patch on that baseline

Both full runs used a fresh temporary render cache:

```bash
label=baseline  # use after for the candidate run
.venv/bin/python bench/benchmark.py \
  --app-root . --photos raw-test \
  --widths 1100,2200,5000 \
  --iterations 30 --browser-iterations 30 \
  --large-library-count 2048 --skip-export \
  --output "/tmp/film-lab-windows-${label}.json"
```

## Hot-path medians

| Measurement | Before | After | Change |
|---|---:|---:|---:|
| Resident render, distinct settings | 110.00 ms | 110.00 ms | 0.0% |
| Server preview, 1100 px | 109.00 ms | 113.00 ms | +3.7% |
| Server preview, 2200 px | 163.00 ms | 163.50 ms | +0.3% |
| Server preview, 5000 px | 203.50 ms | 165.50 ms | -18.7% |
| Browser first frame, 1100 px | 53.65 ms | 54.05 ms | +0.7% |
| Browser settled frame, 1100 px | 53.65 ms | 54.05 ms | +0.7% |
| Browser first frame, 2200 px | 65.30 ms | 64.95 ms | -0.5% |
| Browser settled frame, 2200 px | 229.05 ms | 230.20 ms | +0.5% |
| Browser first frame, 5000 px | 79.10 ms | 76.90 ms | -2.8% |
| Browser settled frame, 5000 px | 372.15 ms | 360.90 ms | -3.0% |
| Navigation first frame | 80.85 ms | 77.15 ms | -4.6% |
| Navigation settled frame | 412.95 ms | 401.85 ms | -2.7% |
| 2048-image library API | 12.93 ms | 13.22 ms | +2.2% |

The single server-start sample moved from 337.73 ms to 437.71 ms. Because one
sample cannot distinguish import cost from scheduling noise, startup was then
measured as 20 alternating clean-process imports of the baseline and candidate.
The medians were 296.23 ms before and 292.39 ms after (-1.3%); p90 was 331.13
ms before and 335.12 ms after (+1.2%).

Conclusion: no measured macOS hot-path or startup regression. The largest
positive hot-path movement is +3.7% at the 1100 px server stage while the
browser-visible movement at that size is +0.7%. The platform split retains the
existing macOS image-service and Metal routes.
