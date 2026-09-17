#!/usr/bin/env python3
"""PathWitness: build the passes, then measure the same benchmarks with and without them.

    python3 run.py setup     build both passes and check them against the controls
    python3 run.py before    measure stock clang  -> results/BEFORE.md
    python3 run.py after     measure with our passes -> results/AFTER.md
    python3 run.py compare   -> results/COMPARISON.md

`before` and `after` compile the same sources with the same flags and apply the
same local cleanup.  The only difference is whether the two PathWitness passes
run before that cleanup.
"""
import argparse
import json
import platform
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from common import (ROOT, build, command, fetch_llvm, object_file, optimize,
                    symbol_sizes, toolchain)

RESULTS = ROOT / 'results'
CORPUS_DEFAULT = Path.home() / 'corpus'

# Reported in this order so the two tables line up line for line.
BENCHMARKS = ['sqlite', 'zlib', 'lua', 'cjson', 'bzip2', 'lz4', 'miniz', 'zip']

VERSIONS = {'sqlite': '3.45.1', 'zlib': '1.3.1', 'lua': '5.4.6', 'cjson': '1.7.18',
            'bzip2': '1.0.8', 'lz4': '1.9.4', 'miniz': '3.0.2', 'zip': '0.3.2'}


def clang_version(llvm):
    return command([llvm / 'clang', '--version']).stdout.splitlines()[0].strip()


def mine(llvm, corpus, work, out, plugins=None):
    """Compile the corpus, optionally run our passes, then prove which branches are dead."""
    args = [sys.executable, ROOT / 'bench' / 'mine_missed_folds.py',
            '--corpus', corpus, '--llvm-bin', llvm, '--work', work, '--out', out]
    if plugins:
        args += ['--plugin-dir', plugins]
    subprocess.run(list(map(str, args)), check=True)
    return json.loads(Path(out).read_text())


def recheck(report, path, ir_dir, out):
    """Drop findings an independent checker can refute.

    The miner uses Z3; this re-evaluates each finding by walking the IR with
    concrete inputs and no solver at all. If it can produce an input that
    reaches the branch, the miner was wrong and the finding does not count.
    """
    # A non-zero exit means it refuted something, which is the case we handle
    # here rather than an error, so the return code is deliberately ignored.
    subprocess.run([sys.executable, str(ROOT / 'bench' / 'verify_findings.py'),
                    '--mining', str(path), '--ir-dir', str(ir_dir),
                    '--out', str(out)])
    if not Path(out).is_file():
        raise RuntimeError(f'The re-check produced no report at {out}')
    verdicts = json.loads(Path(out).read_text())['results']
    refuted = {(r['module'], r['function'], r['block'], r['edge'], r['condition'])
               for r in verdicts if r['verdict'] == 'REFUTED'}
    kept = [f for f in report['findings']
            if (f['module'], f['function'], f['block'], f['edge'],
                f['condition']) not in refuted]
    tally = json.loads(Path(out).read_text())['tally']
    report['refuted'] = len(report['findings']) - len(kept)
    report['confirmed'] = tally.get('CONFIRMED', 0)
    report['sampled'] = tally.get('NO-COUNTEREXAMPLE', 0)
    report['findings'] = kept
    Path(path).write_text(json.dumps(report, indent=2))
    return report


def per_benchmark(report):
    """Findings grouped by benchmark, keyed off the compiled module's file name."""
    counts = defaultdict(int)
    for finding in report['findings']:
        name = Path(finding['module']).name.split('-', 1)[0]
        counts[name] += 1
    return counts


def table(report):
    counts = per_benchmark(report)
    rows = ['| Benchmark | Version | Unreachable branches kept |',
            '|---|---|---:|']
    for name in BENCHMARKS:
        rows.append(f'| {name} | {VERSIONS[name]} | {counts.get(name, 0)} |')
    rows.append(f'| **Total** | | **{len(report["findings"])}** |')
    return '\n'.join(rows)


def header(title, note, llvm, report, seconds):
    return (f'# {title}\n\n{note}\n\n'
            f'- Compiler: {clang_version(llvm)}\n'
            f'- Host: {platform.platform()}\n'
            f'- Benchmarks: {len(BENCHMARKS)} libraries, '
            f'{report["functions"]} functions, {report["branch_edges"]} branch edges\n'
            f'- Solver timeouts (never counted as a finding): {report["timeouts"]}\n'
            f'- Independent re-check: {report.get("confirmed", 0)} proved dead over '
            f'the whole input space, {report.get("sampled", 0)} found no '
            f'counterexample in a sample,\n  {report.get("refuted", 0)} refuted and '
            f'excluded from the table below\n'
            f'- Run time: {seconds // 60} min {seconds % 60} s\n')


# A finding is in scope for these passes when proving the branch dead needs
# both bitmask reasoning and a width cast - the combination LLVM loses.
OURS = {'bitmask', 'width-cast'}


def in_scope(finding):
    return OURS <= set(finding['needs'])


def reasoning(report):
    """What kind of reasoning each dead branch actually needs.

    This is what bounds the work: a pass that fixes width-cast bitmask facts
    cannot be expected to touch a branch that needs ordering reasoning.
    """
    counts = defaultdict(int)
    for finding in report['findings']:
        counts[', '.join(finding['needs'])] += 1
    rows = ['| Reasoning needed to prove it dead | Sites |', '|---|---:|']
    for needs, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        mark = ' **(in scope for these passes)**' if OURS <= set(needs.split(', ')) else ''
        rows.append(f'| {needs}{mark} | {n} |')
    total = sum(1 for f in report['findings'] if in_scope(f))
    rows.append(f'| **In scope for these passes** | **{total}** |')
    return '\n'.join(rows)


def sites(report):
    """One line per function, counted, so the two files diff to what changed."""
    counts = defaultdict(int)
    detail = {}
    for f in report['findings']:
        key = (Path(f['module']).name.split('-', 1)[0], f['function'])
        counts[key] += 1
        detail[key] = ', '.join(f['needs'])
    lines = []
    for (bench, function), n in sorted(counts.items()):
        times = '' if n == 1 else f' ({n} sites)'
        lines.append(f'- `{function}` in {bench}{times} - needs {detail[(bench, function)]}')
    return '\n'.join(lines) if lines else '_none_'


def stage_before(args, llvm):
    started = time.time()
    report = mine(llvm, args.corpus, args.work / 'before', RESULTS / 'before.json')
    report = recheck(report, RESULTS / 'before.json', args.work / 'before',
                     RESULTS / 'verification-before.json')
    elapsed = int(time.time() - started)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / 'BEFORE.md').write_text(
        header('Before: stock clang', METHOD, llvm, report, elapsed) +
        '\n## Branches that can never be taken, and that -O3 keeps\n\n' +
        table(report) + '\n\n### What proving them dead requires\n\n' +
        reasoning(report) + '\n\n### Where they are\n\n' + sites(report) + '\n')
    print(f'\nwrote {RESULTS / "BEFORE.md"}')


def functionality(llvm, plugins, corpus, work):
    """Build real SQLite both ways and make it answer the same questions.

    The scope controls are synthetic.  This links the actually-modified function
    into a real library and drives it through the public API, which is the check
    a reviewer will ask for.
    """
    source = corpus / 'sqlite-amalgamation-3450100'
    out = work / 'functionality'
    out.mkdir(parents=True, exist_ok=True)
    command([llvm / 'clang', '-O3', '-std=gnu17', '-emit-llvm', '-c',
             source / 'sqlite3.c', '-o', out / 'input.bc'], log=out / 'compile.log')
    arms = {}
    for arm, pipeline in [('stock', 'function(instcombine,simplifycfg)'),
                          ('ours', 'function(path-witness-mask-width,'
                                   'path-witness-shift-mask,instcombine,simplifycfg)')]:
        ir = out / (arm + '.ll')
        notes = optimize(llvm, plugins if arm == 'ours' else None,
                         out / 'input.bc', ir, pipeline).stderr
        object_file(llvm, ir, out / (arm + '.o'))
        command([llvm / 'clang', '-O3', '-I', source, ROOT / 'tests/sqlite.c',
                 out / (arm + '.o'), '-lm', '-ldl', '-pthread', '-o', out / arm])
        arms[arm] = dict(
            transcript=command([out / arm], timeout=120).stdout,
            rewrites=notes.count('PW_MASK_NORMALIZED') + notes.count('PW_SHIFT_MASK_ASSUME'),
            sizes=symbol_sizes(llvm, out / (arm + '.o')))
    if arms['stock']['transcript'] != arms['ours']['transcript']:
        raise RuntimeError('SQLite answered differently with the passes enabled')
    before, after = arms['stock']['sizes'], arms['ours']['sizes']
    moved = {n: (s, after[n]) for n, s in before.items() if n in after and after[n] != s}
    return dict(
        transcript=arms['stock']['transcript'].strip().splitlines()[-1],
        rewrites=arms['ours']['rewrites'],
        target=(before['sqlite3_value_blob'], after['sqlite3_value_blob']),
        shrank=sum(1 for a, b in moved.values() if b < a),
        grew=sum(1 for a, b in moved.values() if b > a),
        net=sum(b - a for a, b in moved.values()))


def stage_after(args, llvm):
    plugin_dir = args.work / 'plugin'
    plugins = [plugin_dir / 'PathWitnessMaskWidth.so',
               plugin_dir / 'PathWitnessShiftMask.so']
    if not all(p.is_file() for p in plugins):
        raise SystemExit('Run `python3 run.py setup` first.')
    started = time.time()
    report = mine(llvm, args.corpus, args.work / 'after', RESULTS / 'after.json',
                  plugins=plugin_dir)
    report = recheck(report, RESULTS / 'after.json', args.work / 'after',
                     RESULTS / 'verification-after.json')
    elapsed = int(time.time() - started)
    print('\nChecking that SQLite still behaves identically...')
    fn = functionality(llvm, plugins, args.corpus, args.work)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / 'AFTER.md').write_text(
        header('After: the same benchmarks with both PathWitness passes', METHOD,
               llvm, report, elapsed) +
        '\n## Branches that can never be taken, and that still survive\n\n' +
        table(report) + '\n\n### What proving them dead requires\n\n' +
        reasoning(report) + '\n\n### Where they are\n\n' + sites(report) +
        '\n\n## Does it still work?\n\n'
        'Real SQLite 3.45.1 was compiled both ways and driven through its public\n'
        'API. Both builds produced byte-identical output:\n\n'
        f'    {fn["transcript"]}\n\n'
        f'The passes fired {fn["rewrites"]} times while compiling it.\n\n'
        '### Machine code\n\n'
        f'- `sqlite3_value_blob`: {fn["target"][0]} -> {fn["target"][1]} bytes\n'
        f'- Across the whole module {fn["shrank"]} functions shrank and '
        f'{fn["grew"]} grew, for a net change of {fn["net"]:+d} bytes.\n'
        '  Some functions grow because inlining decisions shift once a branch '
        'disappears. This is reported, not claimed as a gain.\n')
    print(f'\nwrote {RESULTS / "AFTER.md"}')


METHOD = ('Each branch edge is handed to Z3 together with the facts carried by the edges\n'
          'that dominate it. A branch is counted only when the solver proves no input can\n'
          'reach it. Unmodelled operations become free variables, so the count is a lower\n'
          'bound rather than an estimate.')


def where(finding):
    return (Path(finding['module']).name.split('-', 1)[0], finding['function'])


def stage_compare(args, llvm):
    before = json.loads((RESULTS / 'before.json').read_text())
    after = json.loads((RESULTS / 'after.json').read_text())
    b, a = per_benchmark(before), per_benchmark(after)
    rows = ['| Benchmark | Before | After | Removed |', '|---|---:|---:|---:|']
    for name in BENCHMARKS:
        if b.get(name, 0) or a.get(name, 0):
            rows.append(f'| {name} | {b.get(name, 0)} | {a.get(name, 0)} '
                        f'| {b.get(name, 0) - a.get(name, 0)} |')
    nb, na = len(before['findings']), len(after['findings'])
    rows.append(f'| **Total** | **{nb}** | **{na}** | **{nb - na}** |')

    # Our passes renumber SSA values, so a finding cannot be matched to its
    # counterpart by name.  Count per function instead, which is stable.
    total_before, total_after = defaultdict(int), defaultdict(int)
    for f in before['findings']:
        total_before[where(f)] += 1
    for f in after['findings']:
        total_after[where(f)] += 1
    scope = {where(f) for f in before['findings'] if in_scope(f)}
    scope_sites = sum(1 for f in before['findings'] if in_scope(f))
    scope_after = {where(f) for f in after['findings'] if in_scope(f)}
    fixed = {k: total_before[k] for k in scope if total_after[k] == 0}
    # Still dead, still present, but our pass removed the width cast, so the
    # proof no longer needs one.  Not a fix; worth saying so explicitly.
    reclassified = sorted(k for k in scope
                          if total_after[k] > 0 and k not in scope_after)
    partial = {k: (total_before[k], total_after[k]) for k in scope
               if 0 < total_after[k] < total_before[k]}
    removed = sum(total_before[k] - total_after[k] for k in scope)

    lines = [f'- `{fn}` in {bench} - {n} site{"s" if n > 1 else ""}'
             for (bench, fn), n in sorted(fixed.items())]
    for (bench, fn), (was, now) in sorted(partial.items()):
        lines.append(f'- `{fn}` in {bench} - {was - now} of {was} sites')

    excluded = ''
    if before.get('refuted') or after.get('refuted'):
        excluded = (
            'Both runs exclude findings an independent, solver-free re-check could\n'
            f'refute ({before.get("refuted", 0)} before, {after.get("refuted", 0)} '
            'after). BEFORE.md and AFTER.md give that breakdown.\n\n')

    leftover = f'The other {scope_sites - removed} remain.'
    if reclassified:
        names = ', '.join(f'`{fn}`' for _, fn in reclassified)
        leftover += (f' {len(reclassified)} of them ({names}) no longer\n'
                     'need a width cast to prove dead, because the cast is what our '
                     'pass removed,\nbut the branch itself survives. Those count as '
                     'not fixed.')

    parts = [
        '# Before and after\n\n',
        'Both columns come from the same sources, compiled with the same flags by\n'
        f'{clang_version(llvm).split("(")[0].strip()}, and given the same local '
        'cleanup afterwards. The only\ndifference is whether the two PathWitness '
        'passes ran first.\n\n',
        '\n'.join(rows) + '\n\n',
        excluded,
        '## What was removed\n\n',
        f'Of the {nb} unreachable branches stock `-O3` keeps, {scope_sites} need both '
        'bitmask\nreasoning and a width cast to prove dead. That combination is what '
        'LLVM loses,\nand it is the only thing these two passes address. A branch '
        'whose proof needs\nordering reasoning is out of scope and we do not count '
        'it.\n\n',
        f'**{removed} of those {scope_sites} sites are gone, across '
        f'{len(fixed) + len(partial)} of {len(scope)} functions:**\n\n',
        ('\n'.join(lines) if lines else '_none_') + '\n\n',
        leftover + '\n\n',
        '## What this is not\n\n',
        'These branches were never taken at run time, so removing them is dead-code\n'
        'removal, not a speedup. AFTER.md records that real SQLite compiled with the\n'
        'passes answers its API checks byte-identically. FINDINGS.md gives the full\n'
        'scope.\n',
    ]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / 'COMPARISON.md').write_text(''.join(parts))
    print(f'wrote {RESULTS / "COMPARISON.md"}')


def stage_setup(args, llvm):
    print(f'Toolchain: {clang_version(llvm)}')
    sys.path.insert(0, str(ROOT / 'bench'))
    from corpora import fetch
    print(f'Benchmark sources in {args.corpus}')
    for name in BENCHMARKS:
        fetch(args.corpus, name)
    print('  all eight present, checksums match')
    plugins = build(llvm, args.work)
    for path in plugins:
        print(f'  built {path.name}')
    import checks
    checks.check(llvm, plugins[0], args.work / 'controls')
    print('\nControls passed. Next: python3 run.py before')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=['setup', 'before', 'after', 'compare'])
    parser.add_argument('--llvm-bin', help='LLVM 23.x bin directory (or set LLVM_BIN)')
    parser.add_argument('--corpus', type=Path, default=CORPUS_DEFAULT,
                        help='directory holding the benchmark sources')
    parser.add_argument('--work', type=Path, default=ROOT.parent / '.pathwitness-work',
                        help='scratch directory, kept outside this folder')
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    try:
        llvm = toolchain(args.llvm_bin)
    except RuntimeError:
        if args.stage != 'setup':
            raise SystemExit('No LLVM toolchain. Run `python3 run.py setup` first, '
                             'or pass --llvm-bin.')
        llvm = toolchain(fetch_llvm(args.work))
    {'setup': stage_setup, 'before': stage_before,
     'after': stage_after, 'compare': stage_compare}[args.stage](args, llvm)


if __name__ == '__main__':
    main()
