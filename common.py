"""Shared process and LLVM helpers; no dependency on the research workspace."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent

# The exact toolchain every number in results/ was measured on.  The passes use
# llvm/Plugins/PassPlugin.h and so need LLVM 23.x; older releases will not build.
LLVM = dict(version='23.1.1', url='https://github.com/llvm/llvm-project/releases/download/llvmorg-23.1.1/LLVM-23.1.1-Linux-X64.tar.xz', archive='LLVM-23.1.1-Linux-X64.tar.xz', root='LLVM-23.1.1-Linux-X64', sha256='832aeb58d105de1cabc7b982dd2c65de0610f7377df48ae8fc2dd8e97420a15c')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def command(args, cwd=None, log=None, timeout=600):
    args = list(map(str, args))
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(json.dumps(args) + '\n' + result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'Command failed: {args}\n{result.stdout[-2000:]}\n{result.stderr[-3000:]}')
    return result


def toolchain(value):
    path = value or os.environ.get('LLVM_BIN')
    if not path:
        opt = shutil.which('opt')
        path = str(Path(opt).parent) if opt else None
    if not path:
        raise RuntimeError('Set LLVM_BIN or supply --llvm-bin /path/to/LLVM/bin.')
    path = Path(path).resolve()
    for name in ['clang', 'clang++', 'opt', 'llc', 'llvm-config', 'llvm-objdump', 'llvm-nm']:
        if not (path / name).is_file():
            raise RuntimeError(f'Missing tool: {path / name}')
    return path


def build(llvm, work):
    directory = work / 'plugin'
    # A CMake cache records the absolute source path it was generated from, so a
    # cache left by a copy of this folder at a different path is unusable.
    cache = directory / 'CMakeCache.txt'
    if cache.is_file() and f'CMAKE_HOME_DIRECTORY:INTERNAL={ROOT / "src"}\n' not in cache.read_text():
        shutil.rmtree(directory)
    command(['cmake', '-S', ROOT / 'src', '-B', directory, '-G', 'Ninja',
             '-DCMAKE_BUILD_TYPE=Release',
             '-DLLVM_DIR=' + command([llvm / 'llvm-config', '--cmakedir']).stdout.strip(),
             '-DCMAKE_CXX_COMPILER=' + str(llvm / 'clang++')], log=work / 'configure.log')
    command(['cmake', '--build', directory], log=work / 'build.log')
    return [directory / 'PathWitnessMaskWidth.so', directory / 'PathWitnessShiftMask.so']


def functions(ir):
    return {m[1]: m[0] for m in re.finditer(
        r'^define\b[^\n]*@([\w.$-]+|"[^"\n]+")\([^\n]*\n.*?^}', ir, re.M | re.S)}


def canonical(ir):
    return re.sub(r'^; ModuleID = .*\n', '', ir)


def instruction_count(body):
    # Do not count switch-case operands, closing brackets or block comments.
    return len(re.findall(r'^  (?:%[^=\n]+ = |(?:ret|br|switch|indirectbr|invoke|resume|unreachable|store|fence|call|tail call|musttail call|notail call|catchret|cleanupret)\b)', body, re.M))


def symbol_sizes(llvm, obj):
    text = command([llvm / 'llvm-nm', '-S', '--defined-only', obj]).stdout
    return {p[3]: int(p[1], 16) for line in text.splitlines()
            if len(p := line.split()) == 4 and p[2] in ['T', 't']}


def optimize(llvm, plugins, source, output, passes):
    # plugins may be a single path or the list build() returns.
    if plugins is None:
        plugins = []
    elif not isinstance(plugins, (list, tuple)):
        plugins = [plugins]
    loads = ['-load-pass-plugin=' + str(p) for p in plugins]
    return command([llvm / 'opt', *loads,
                    '-passes=' + passes, '-verify-each', '-S', source, '-o', output])


def object_file(llvm, source, output):
    command([llvm / 'llc', '-O3', '-relocation-model=pic', '-filetype=obj', source, '-o', output])


def fetch_llvm(work):
    """Download the pinned LLVM release unless a toolchain was supplied."""
    import tarfile, urllib.request
    cache = work / 'sources'
    root = cache / LLVM['root']
    if (root / 'bin' / 'clang').is_file():
        return root / 'bin'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / LLVM['archive']
    if not archive.exists():
        print(f"Downloading LLVM {LLVM['version']} (about 2 GB, once)", flush=True)
        partial = archive.with_suffix(archive.suffix + '.partial')
        with urllib.request.urlopen(LLVM['url'], timeout=600) as response, partial.open('wb') as out:
            received = 0
            while block := response.read(4 << 20):
                out.write(block)
                received += len(block)
                if received % (256 << 20) < (4 << 20):
                    print(f'  {received >> 20} MiB', flush=True)
        partial.rename(archive)
    if digest(archive) != LLVM['sha256']:
        raise RuntimeError(f'LLVM archive checksum mismatch: {archive}')
    print('  extracting', flush=True)
    with tarfile.open(archive) as package:
        package.extractall(cache, filter='data')
    return root / 'bin'
