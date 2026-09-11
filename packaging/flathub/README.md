# Flathub Submission Guide: LightTable (`app.lighttable.LightTable`)

This directory contains the prepared submission files for Flathub.

## Generative AI Policy & Human Submission Requirement

> **Important**: Under Flathub's official submission policies, pull requests to `flathub/flathub` must be opened by a human maintainer. AI agents are prohibited from opening submission pull requests or generating submission/review commentary. Submitters must disclose any AI-assisted tools transparently in their PR description.

---

## Submission Package Contents

The `app.lighttable.LightTable/` directory is fully self-contained and ready to be placed directly into the root of the `flathub/flathub` repository:

| File | Purpose |
| :--- | :--- |
| `app.lighttable.LightTable.json` | Complete Flatpak manifest with 117 modules, 816 vendored Rust crates, and all native dependencies |
| `app.lighttable.LightTable.metainfo.xml` | AppStream 1.0 metadata specifying 0.6.1, screenshots from `https://lighttable.app/screenshots/linux/`, licensing, and URLs |
| `app.lighttable.LightTable.desktop` | Desktop launcher configuration with MIME scheme handler and categories |
| `flathub.json` | Flathub build configuration (`only-arches: ["x86_64"]`) |
| `source-*.py`, `lighttable-*`, `*.lock`, `source-status.json` | Pinned companion build scripts and lockfiles required for offline compilation |

---

## Step-by-Step Submission Instructions

### Step 1: Fork the Flathub Repository
1. Navigate to [https://github.com/flathub/flathub](https://github.com/flathub/flathub).
2. Click **Fork** (top right) to create a fork under your GitHub account (`reville`).

### Step 2: Clone and Create a Branch
In your local terminal:
```bash
git clone git@github.com:reville/flathub.git /tmp/flathub-fork
cd /tmp/flathub-fork
git checkout -b add-lighttable
```

### Step 3: Copy the Prepared Submission Package
```bash
cp -r "/Users/nicholasreville/CODING/Film Lab/lighttable-digital-darkroom/packaging/flathub/app.lighttable.LightTable" .
```

Verify that the files exist directly in `/tmp/flathub-fork/app.lighttable.LightTable/`:
```bash
ls -la app.lighttable.LightTable/
```

### Step 4: Commit and Push
```bash
git add app.lighttable.LightTable
git commit -m "Add app.lighttable.LightTable"
git push -u origin add-lighttable
```

### Step 5: Open the Pull Request on GitHub
1. Navigate to [https://github.com/flathub/flathub/pulls](https://github.com/flathub/flathub/pulls).
2. Click **New pull request**, then select **compare across forks**, and pick `reville/flathub` with branch `add-lighttable`.
3. Complete the Flathub submission PR template:
   - **App ID**: `app.lighttable.LightTable`
   - **App Name**: `LightTable`
   - **Summary**: `Digital darkroom for RAW photographs and photographic film simulation`
   - **License**: `GPL-3.0-only`
   - **Upstream Repository**: `https://github.com/reville/lighttable-digital-darkroom`
   - **Domain Verification**: `lighttable.app`
   - **Notes**: Note that this is a source-only build incorporating CPython, OpenBLAS, OpenCV, and native image codecs, pre-fetching all sources for offline compilation (`--disable-download`). Disclose AI-assisted packaging tooling per Flathub requirements.

### Step 6: Flathub CI & Verification
- Flathub's automated buildbot will run a test build of the manifest.
- Reviewers will check the manifest, metadata, and sandbox permissions.
- Once merged, Flathub will automatically create the repository `https://github.com/flathub/app.lighttable.LightTable` and send you an invitation as the repository maintainer.
