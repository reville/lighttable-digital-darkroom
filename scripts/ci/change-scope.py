#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Decide which pull-request checks a change needs from the files it touches.

Pushes to main, manual dispatches, release calls, an empty file list, a
`ci:full` label, and any change to CI itself run everything. A pull request
otherwise runs the light tier: the cross-platform unit suite without the
resident engine and pixel gates, and no Windows or Linux host jobs, unless it
touches the paths those jobs exist to protect. Skipped required jobs report as
skipped, which GitHub counts as passing, so the light tier never blocks a merge.

Usage: change-scope.py --event pull_request [--labels a,b] < changed-files.txt
Prints `key=value` lines for $GITHUB_OUTPUT.
"""
import argparse
import fnmatch
import sys

FULL_LABEL = 'ci:full'

# Any change here changes what the checks mean, so run all of them.
INFRASTRUCTURE = (
    '.github/**', 'scripts/ci/**', 'requirements*.lock', 'requirements*.txt',
    'pyproject.toml', 'packaging/runtime-*.lock', 'media-formats.json', 'app_version.py',
)

# Changes that need no test job at all; the fast help/localization job still runs.
DOCS_ONLY = (
    'docs/**', '*.md', '**/*.md', 'LICENSE', '.gitignore', '.gitattributes', '.claude/**',
    'web/locales/**', 'demo-assets/**', 'examples/**', 'scripts/screenshots/**',
    'scripts/visual-review/**', 'scripts/ui-audit/**', 'bench/**',
)

# The one edit contract: NumPy, WebGL, Metal, and Rust must keep agreeing.
PIXEL = (
    'grade.py', 'edits.py', 'soft_proof.py', 'mask_raster.py', 'color_pipeline.py',
    'film_pipeline.py', 'film_tuning.py', 'camera_profile.py', 'platform_image.py',
    'raw_decode_cache.py', 'merge_workflow.py', 'merge_acceleration.py', 'enhance_workflow.py',
    'export_workflow.py', 'export_surface.py', 'geometry_auto.py', 'gpu_compute.py',
    'calibration_target.py', 'calibration/**', 'profiles/**', 'presets/**', 'engine/**',
    'rust-engine/**', 'web/gl.js', 'web/mask-*.js', 'web/*.glsl', 'app/**',
    'tests/goldens/**', 'tests/fixtures/**', 'tests/processing_*', 'tests/control_*',
    'tests/film_semantics.py', 'tests/engine_*', 'tests/native_processing_preview.*',
    'tests/test_processing_*.py', 'tests/test_control_*.py', 'tests/test_export_parity.py',
    'tests/test_gpu_grade.py', 'tests/test_grade.py', 'tests/test_edits.py',
    'scripts/check-processing.py',
)

# The Windows host, its installer, and the Python modules its quick check runs.
WINDOWS = (
    'windows-shell/**', 'scripts/windows/**', 'packaging/**', 'rust-engine/**',
    'desktop_updater.py', 'launcher_control.py', 'durable_io.py', 'platform_paths.py',
    'bounded_logging.py', 'fatal_diagnostics.py', 'lighttable_cli/**', 'lighttable',
    'tests/test_windows*.py', 'tests/windows_*', 'tests/test_process_liveness.py',
    'tests/test_launcher_control.py', 'tests/test_durable_io.py',
    'tests/test_no_hardlink_filesystem.py', 'tests/test_startup_reporting.py',
    'tests/test_webview2_runtime.py',
)

# The Linux host build, its storage and decoding tests, and software-Vulkan parity.
LINUX = (
    'windows-shell/**', 'scripts/linux/**', 'packaging/**', 'rust-engine/**',
    'desktop_updater.py', 'linux_theme.py', 'platform_paths.py', 'platform_image.py',
    'raw_decode_cache.py', 'gpu_compute.py', 'grade.py', 'merge_acceleration.py',
    'merge_workflow.py', 'durable_io.py', 'lighttable_cli/**', 'lighttable',
    'tests/test_linux*.py', 'tests/test_desktop_update*.py', 'tests/test_platform_*.py',
    'tests/test_raw_decode_cache.py', 'tests/test_windows_precision_cache.py',
    'tests/test_cli_*.py', 'tests/test_first_run.py', 'tests/test_gpu_grade.py',
    'tests/test_merge_acceleration.py', 'tests/*.test.mjs',
)

# Installer manifests, release selection, and the npm launcher.
INSTALLERS = (
    'packaging/**', 'scripts/linux/**', 'scripts/release/**', 'lighttable_cli/**',
    'lighttable', 'desktop_updater.py', 'durable_io.py', 'platform_paths.py',
    'tests/test_installer_manifests.py', 'tests/test_release_*.py',
    'tests/test_linux_aur_packaging.py', 'tests/test_linux_snap_packaging.py',
    'tests/test_cli_installation.py', 'tests/test_no_hardlink_filesystem.py',
)

KEYS = ('full', 'docs_only', 'pixel', 'windows', 'linux', 'installers')


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def scope(files, event='pull_request', labels=()):
    """Return the check scope for a change as a dict of booleans."""
    files = [line.strip() for line in files if line.strip()]
    full = (event != 'pull_request' or not files or FULL_LABEL in labels
            or any(matches(path, INFRASTRUCTURE) for path in files))
    if full:
        return {'full': True, 'docs_only': False, 'pixel': True, 'windows': True,
                'linux': True, 'installers': True}
    return {
        'full': False,
        'docs_only': all(matches(path, DOCS_ONLY) for path in files),
        'pixel': any(matches(path, PIXEL) for path in files),
        'windows': any(matches(path, WINDOWS) for path in files),
        'linux': any(matches(path, LINUX) for path in files),
        'installers': any(matches(path, INSTALLERS) for path in files),
    }


def format_outputs(result):
    return ''.join(f'{key}={"true" if result[key] else "false"}\n' for key in KEYS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', default='pull_request', help='GitHub event name')
    parser.add_argument('--labels', default='', help='Comma-separated pull request labels')
    args = parser.parse_args(argv)
    labels = {label.strip() for label in args.labels.split(',') if label.strip()}
    result = scope(sys.stdin.readlines(), args.event, labels)
    sys.stdout.write(format_outputs(result))
    tier = 'full' if result['full'] else ('none' if result['docs_only'] else 'light')
    print(f'Check scope: {tier}; ' + ', '.join(f'{key}={str(result[key]).lower()}' for key in KEYS[1:]),
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
