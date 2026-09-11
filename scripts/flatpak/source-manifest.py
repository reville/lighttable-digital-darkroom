#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate the experimental source-only build graph from immutable source pins.

Generation is offline. Fetching sources belongs to flatpak-builder --download-only.
The generated full graph is not a verified build: see source-status.json for the
required native build and runtime validation gates.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / 'packaging/flatpak'
PYTHON = '/app/LightTable/Python/bin/python3'
HELPER = '/app/share/lighttable/build/source-wheel.py'
APP_ID = 'app.lighttable.LightTable'


def read_revision(revision: str, path: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', revision + ':' + path])


def pinned_file(path: Path, output: Path, **extra) -> dict:
    return {'type': 'file', 'path': os.path.relpath(path, output.parent),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **extra}


def cargo_sources(lockfiles: list[bytes]) -> list[dict]:
    crates = {}
    for lock in lockfiles:
        for package in tomllib.loads(lock.decode())['package']:
            origin = package.get('source')
            if not origin:
                continue
            if origin != 'registry+https://github.com/rust-lang/crates.io-index':
                raise ValueError(f'Unimplemented Cargo source kind: {origin}')
            key = package['name'], package['version']
            checksum = package['checksum']
            if key in crates and crates[key] != checksum:
                raise ValueError(f'Conflicting checksums for {key}')
            crates[key] = checksum
    result = []
    for (name, version), checksum in sorted(crates.items()):
        destination = f'cargo/vendor/{name}-{version}'
        result.extend([
            {'type': 'archive', 'archive-type': 'tar-gzip',
             'url': f'https://static.crates.io/crates/{name}/{name}-{version}.crate',
             'sha256': checksum, 'dest': destination},
            {'type': 'inline', 'dest': destination, 'dest-filename': '.cargo-checksum.json',
             'contents': json.dumps({'package': checksum, 'files': {}})},
        ])
    return result


def generate(revision: str, version: str, output: Path, *, allow_incomplete=False) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('An exact 40-character application source commit is required')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z.-]+)?', version):
        raise ValueError('Invalid package version')
    dependencies = json.loads((PACKAGING / 'source-dependencies.json').read_text())
    audit = json.loads((PACKAGING / 'python-source-audit.json').read_text())
    runtime_lock = read_revision(revision, 'requirements-runtime.lock')
    if hashlib.sha256(runtime_lock).hexdigest() != audit['requirements_sha256']:
        raise ValueError('Source revision does not match the audited Python runtime lock')
    pins = json.loads(read_revision(revision, 'packaging/linux/runtime.json'))
    if pins['rust_source_revision'] != dependencies['rust_cli']['revision']:
        raise ValueError('The pinned upstream CLI Cargo.lock needs to be refreshed')
    cli_lock = (PACKAGING / 'source-cli-Cargo.lock').read_bytes()
    if hashlib.sha256(cli_lock).hexdigest() != dependencies['rust_cli']['lock_sha256']:
        raise ValueError('The upstream CLI Cargo.lock checksum changed')
    build = dependencies['build_python']
    runtime = {r['name']: r for r in audit['packages']}
    native = dependencies['native_sources']
    modules = []

    def simple(name, commands, sources, **kwargs):
        module = {'name': name, 'buildsystem': 'simple', 'build-commands': commands,
                  'sources': sources, **kwargs}
        modules.append(module)
        return module

    def cmake(name, options, source, subdir=None):
        module = {'name': name, 'buildsystem': 'cmake-ninja', 'builddir': True,
                  'config-opts': ['-DFETCHCONTENT_FULLY_DISCONNECTED=ON', *options],
                  'sources': [source]}
        if subdir:
            module['subdir'] = subdir
        modules.append(module)

    if not allow_incomplete:
        simple('source-closure-check', ['python3 source-preflight.py source-status.json --check-tools'], [
            pinned_file(ROOT / 'scripts/flatpak/source-preflight.py', output),
            pinned_file(PACKAGING / 'source-status.json', output),
        ])
    else:
        simple('source-build-tools-check', ['command -v gcc g++ gfortran cmake ninja pkg-config cargo'], [])
    simple('cpython', [
        './configure --prefix=/app/LightTable/Python --enable-shared --with-ensurepip=no',
        'make -j${FLATPAK_BUILDER_N_JOBS}', 'make install',
    ], [native['cpython']],
       **{'build-options': {'env': {'LDFLAGS': '-Wl,-rpath,/app/LightTable/Python/lib'}}})
    simple('source-build-helpers', [
        'install -Dm644 source-wheel.py /app/share/lighttable/build/source-wheel.py',
    ], [pinned_file(ROOT / 'scripts/flatpak/source-wheel.py', output)])
    simple('python-flit-core', [
        PYTHON + ' -m flit_core.wheel',
        PYTHON + ' bootstrap_install.py dist/flit_core-*.whl --installdir /app/LightTable/Python/lib/python3.13/site-packages',
    ], [build['flit_core']['source']])
    for name in ('installer', 'pip'):
        simple('python-' + name, [f'{PYTHON} {HELPER} --bootstrap'], [build[name]['source']])

    def python(name, *, source=None, build_only=False, overrides=(), settings=(), environment=None):
        if source is None:
            row = build.get(name) or runtime[name]
            source = copy.deepcopy(row.get('source') or row['sources'][0])
            if source['type'] == 'file':
                source['type'] = 'archive'
        args = [PYTHON, HELPER]
        if build_only:
            args.append('--build-only')
        for override in overrides:
            args.extend(['--override', override])
        for setting in settings:
            args.extend(['--config', setting])
        module = simple('python-' + name.lower(), [shlex.join(args)], [source])
        if environment:
            module['build-options'] = {'env': environment}

    # The early source-built tooling bootstraps itself. No ensurepip wheel,
    # downloaded wheel, build-isolation resolver or host site-packages is used.
    for name in ('setuptools', 'wheel', 'packaging', 'typing-extensions', 'toml',
                 'setuptools-scm', 'calver', 'trove-classifiers', 'pathspec', 'pluggy',
                 'hatchling', 'hatch-vcs', 'hatch-fancy-pypi-readme', 'meson',
                 'pyproject-metadata', 'meson-python', 'scikit-build-core', 'pybind11',
                 'Cython', 'cppy', 'distro', 'scikit-build', 'ply', 'gast', 'beniget'):
        python(name)
    python('setuptools-old', build_only=True)
    python('setuptools-scm-old', build_only=True)

    blas = copy.deepcopy(json.loads((PACKAGING / 'direct-bundle.json.in').read_text())['modules'][0])
    blas.pop('cleanup', None)
    # SciPy needs the LAPACK implementation as well as the engine's CBLAS API.
    blas['build-commands'] = [c.replace(' NOFORTRAN=1', '') for c in blas['build-commands']]
    modules.append(blas)
    for precision, switch in (('double', ''), ('float', '--enable-float'), ('long-double', '--enable-long-double')):
        simple('fftw-' + precision, [
            './configure --prefix=/app --enable-shared --disable-static --enable-threads ' + switch,
            'make -j${FLATPAK_BUILDER_N_JOBS}', 'make install',
        ], [native['fftw']])
    cmake('llvm22', ['-DCMAKE_INSTALL_PREFIX=/app/llvm22', '-DCMAKE_BUILD_TYPE=Release',
                    '-DLLVM_PARALLEL_LINK_JOBS=1', '-DLLVM_PARALLEL_COMPILE_JOBS=2',
                    '-DLLVM_TARGETS_TO_BUILD=X86;AArch64', '-DLLVM_ENABLE_PROJECTS=',
                    '-DLLVM_ENABLE_RTTI=ON', '-DLLVM_INCLUDE_TESTS=OFF',
                    '-DLLVM_INCLUDE_BENCHMARKS=OFF', '-DLLVM_INCLUDE_EXAMPLES=OFF'], native['llvm'], 'llvm')
    modules.extend(copy.deepcopy(dependencies['native_recipes']))
    # Explicit source recipes retain every extension in the upstream Linux
    # imagecodecs wheel. Rust C libraries get the same offline vendoring as the
    # application, with separately pinned upstream lockfiles.
    codec_graph = json.loads((PACKAGING / 'source-native-codecs.json').read_text())
    for recipe in copy.deepcopy(codec_graph['modules']):
        lock_name = recipe.pop('x-cargo-lock', None)
        if lock_name:
            lock_path = PACKAGING / lock_name
            recipe['sources'].extend(cargo_sources([lock_path.read_bytes()]))
            recipe['sources'].append(pinned_file(lock_path, output, **{'dest-filename': 'Cargo.lock'}))
        modules.append(recipe)
    for name, options in (
        ('fmt', ['-DFMT_TEST=OFF', '-DFMT_DOC=OFF', '-DBUILD_SHARED_LIBS=ON']),
        ('robin-map', ['-DROBIN_MAP_BUILD_TESTS=OFF']),
        ('qhull', ['-DBUILD_TESTING=OFF']),
        ('lensfun', ['-DBUILD_TESTS=OFF', '-DBUILD_DOCUMENTATION=OFF', '-DBUILD_AUXFUN=OFF']),
    ):
        cmake(name, options, native[name])
    modules.append({'name': 'libraqm', 'buildsystem': 'meson',
                    'config-opts': ['--wrap-mode=nodownload', '-Ddocs=false', '-Dtests=false'],
                    'sources': [native['libraqm']]})
    simple('libraw', ['autoreconf -fi', './configure --prefix=/app --disable-static --enable-openmp',
                     'make -j${FLATPAK_BUILDER_N_JOBS}', 'make install'], [native['libraw']])
    modules.append(copy.deepcopy(json.loads((PACKAGING / 'direct-bundle.json.in').read_text())['modules'][1]))

    python('numpy', settings=('setup-args=--wrap-mode=nodownload',))
    python('numpy-opencv', build_only=True, settings=('setup-args=--wrap-mode=nodownload',))
    python('pythran')
    python('scipy', settings=('setup-args=--wrap-mode=nodownload',))
    python('llvmlite', environment={'LLVM_DIR': '/app/llvm22/lib/cmake/llvm'})
    python('numba')
    for name in ('six', 'cycler', 'fonttools', 'kiwisolver', 'lazy-loader', 'markdown',
                 'networkx', 'pillow', 'pyparsing', 'imageio', 'opt-einsum', 'pyfftw',
                 'tifffile', 'colour-science'):
        python(name)
    python('python-dateutil', overrides=('setuptools-scm==7.1.0',))
    python('contourpy', settings=('setup-args=--wrap-mode=nodownload',))
    python('matplotlib', settings=('setup-args=--wrap-mode=nodownload',
                                  'setup-args=-Dsystem-freetype=true', 'setup-args=-Dsystem-qhull=true',
                                  'setup-args=-Dsystem-libraqm=true'))
    python('scikit-image', settings=('setup-args=--wrap-mode=nodownload',))
    python('exiv2')
    python('lensfunpy')
    python('rawpy', environment={'RAWPY_USE_SYSTEM_LIBRAW': '1'})
    python('opencv-python-headless', overrides=('setuptools==69.5.1', 'numpy==2.1.3'), environment={
        'ENABLE_HEADLESS': '1', 'CMAKE_ARGS': '-DFETCHCONTENT_FULLY_DISCONNECTED=ON -DOPENCV_DOWNLOAD_PATH=/nonexistent '
        '-DWITH_IPP=OFF -DWITH_ITT=OFF -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_opencv_gapi=OFF',
    })
    python('openimageio', settings=('cmake.define.OpenImageIO_BUILD_MISSING_DEPS=none',
        'cmake.define.FETCHCONTENT_FULLY_DISCONNECTED=ON', 'cmake.define.USE_OPENCOLORIO=OFF',
        'cmake.define.OIIO_BUILD_TESTS=OFF', 'cmake.define.OIIO_BUILD_TOOLS=OFF'))

    # The full codec source closure is intentionally not silently reduced to
    # the imagecodecs "lite" feature set. Missing native headers fail this build.
    python('imagecodecs')
    modules[-1]['sources'].append(pinned_file(ROOT / 'scripts/flatpak/source-imagecodecs.py', output,
                                              **{'dest-filename': 'imagecodecs_distributor_setup.py'}))
    modules[-1]['build-commands'].append(PYTHON + ' source-codec-check.py')
    modules[-1]['sources'].append(pinned_file(ROOT / 'scripts/flatpak/source-codec-check.py', output))
    locks = [read_revision(revision, p) for p in ('rust-engine/Cargo.lock', 'windows-shell/Cargo.lock')]
    source_list = [
        {'type': 'git', 'url': 'https://github.com/reville/lighttable-digital-darkroom.git',
         'commit': revision, 'dest': 'application'},
        {'type': 'git', 'url': 'https://github.com/andreavolpato/agx-emulsion.git',
         'commit': pins['python_source_revision'], 'dest': 'python-engine'},
        {'type': 'git', 'url': 'https://github.com/turbasvin/spektrafilm-rs.git',
         'commit': pins['rust_source_revision'], 'dest': 'rust-cli'},
        *cargo_sources([*locks, cli_lock]),
    ]
    spec = {}
    exec(read_revision(revision, 'scripts/fetch-color-profiles.py'), spec)
    for name, (filename, sha256) in spec['ASSETS'].items():
        source_list.append({'type': 'file', 'url': spec['BASE_URL'] + name,
                            'sha256': sha256, 'dest': 'icc', 'dest-filename': filename})
    for path in ('scripts/flatpak/source-install.py', 'scripts/flatpak/lighttable-desktop',
                 'scripts/flatpak/lighttable-cli', f'packaging/flatpak/{APP_ID}.desktop',
                 f'packaging/flatpak/{APP_ID}.metainfo.xml'):
        source_list.append(pinned_file(ROOT / path, output))
    provenance = {'version': version, 'source_revision': revision, 'source_dirty': False,
                  'platform': 'linux', 'architecture': 'x86_64', 'runtime': pins,
                  'distribution': 'experimental-source-flatpak', 'source_build_verified': False,
                  'runtime_requirements_sha256': audit['requirements_sha256']}
    source_list.append({'type': 'inline', 'dest-filename': 'source-provenance.json',
                        'contents': json.dumps(provenance, indent=2)})
    commands = [
        "mkdir -p cargo; printf '[source.crates-io]\\nreplace-with=\"vendored-sources\"\\n[source.vendored-sources]\\ndirectory=\"%s/cargo/vendor\"\\n[net]\\noffline=true\\n' \"$PWD\" > cargo/config.toml",
    ]
    for path, target, binary in (('application/rust-engine', 'resident', 'lighttable-engine'),
                                 ('application/windows-shell', 'desktop', 'lighttable-desktop-shell'),
                                 ('rust-cli', 'cli', 'spektrafilm')):
        package = ' -p spektrafilm-cli' if target == 'cli' else ''
        commands.append(f'CARGO_HOME=$PWD/cargo CARGO_TARGET_DIR=$PWD/target/{target} cargo build --offline --locked --release --manifest-path {path}/Cargo.toml --bin {binary}{package}')
    commands.extend([f'{PYTHON} source-install.py', f'{PYTHON} -m pip check',
                     f'{PYTHON} -B /app/LightTable/runtime-smoke.py /app/LightTable'])
    simple('lighttable-source', commands, source_list)
    # Keep notice-only changes after every compiled module so completed build
    # stages remain reusable when the license inventory is corrected.
    modules.append(json.loads((PACKAGING / 'source-native-licenses.json').read_text()))
    manifest = {
        'app-id': APP_ID, 'runtime': 'org.gnome.Platform', 'runtime-version': '50',
        'sdk': 'org.gnome.Sdk', 'sdk-extensions': ['org.freedesktop.Sdk.Extension.rust-stable'],
        'command': 'lighttable-desktop',
        'finish-args': json.loads((PACKAGING / 'direct-bundle.json.in').read_text())['finish-args'],
        'build-options': {'env': {
            'PATH': '/app/LightTable/Python/bin:/usr/lib/sdk/rust-stable/bin:/app/bin:/usr/bin',
            'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'CARGO_NET_OFFLINE': 'true',
            'CMAKE_PREFIX_PATH': '/app:/app/LightTable/Python',
            'SPEKTRAFILM_BACKEND': 'cpu', 'PYTHONNOUSERSITE': '1',
        }},
        'cleanup': ['/source-wheels', '/share/lighttable/build'],
        'modules': modules,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--version', default='0.5.0')
    parser.add_argument('--output', type=Path, default=PACKAGING / 'source-candidate.json')
    parser.add_argument('--allow-incomplete', action='store_true',
                        help='For bounded recipe experiments only: omit the initial known-closure-failure gate')
    args = parser.parse_args()
    manifest = generate(args.source_revision, args.version, args.output.resolve(), allow_incomplete=args.allow_incomplete)
    print(f"Generated {len(manifest['modules'])} source modules; native build and runtime parity verification remain pending")


if __name__ == '__main__':
    main()
