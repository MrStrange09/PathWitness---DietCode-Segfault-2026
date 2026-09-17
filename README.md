# PathWitness

`clang -O3` keeps branches that can never be taken, because a fact it proved
about a widened copy of a value is never consulted when something asks about the
original. Two bounded LLVM passes remove the mismatch; LLVM's own cleanup then
removes the branch.

On eight libraries that ship in production, compiled by clang 23.1.1, 37 branch
edges are unreachable and survive `-O3`. Twelve of them need the reasoning LLVM
loses; our passes remove eight, in six functions of SQLite, and SQLite still
answers its API checks byte-identically.

## Requirements

Linux x86-64, Python 3.11+, CMake, Ninja, and a C compiler.

```sh
sudo apt-get install cmake ninja-build build-essential
pip install -r requirements.txt
```

The toolchain is taken from `--llvm-bin`, then `$LLVM_BIN`, then whatever `opt`
is on your PATH. If none of those exist, `setup` downloads the pinned LLVM
23.1.1 it was measured on: a 2 GB archive that expands to about 12 GB, so allow
20 GB free and a slow first run. It must be LLVM 23.x; earlier releases predate
the move of `PassPlugin.h` into `llvm/Plugins/` and will not build the passes.

Scratch output goes to `../.pathwitness-work/`. Benchmark sources go to
`~/corpus`, or wherever `--corpus DIR` points. Nothing is installed
system-wide.

## Running it

```sh
python3 run.py setup     # fetch LLVM and the benchmarks, build both passes, check them
python3 run.py before    # measure stock clang           -> results/BEFORE.md
python3 run.py after     # measure with our passes       -> results/AFTER.md
python3 run.py compare   # the difference                -> results/COMPARISON.md
```

| Command | What it does | Time |
|---|---|---|
| `setup` | Downloads LLVM 23.1.1 (~2 GB, once) and eight benchmark libraries, verifies every checksum, builds both passes, and runs 37 scope controls with 5.8 M native executions | 15-25 min first time, seconds after |
| `before` | Compiles the eight libraries at `-O3`, applies local cleanup, asks Z3 which branch edges no input can reach, then re-checks each answer without a solver | about 3 min |
| `after` | The same, with both passes running first, then builds real SQLite both ways and checks it answers identically | about 3 min |
| `compare` | Reads the two runs and writes the delta | instant |

Both runs use the same sources, flags and cleanup, so the two reports share a
structure and the delta is readable directly:

```sh
diff results/BEFORE.md results/AFTER.md
```

`results/` holds a recorded run, so the numbers can be read without running
anything. Re-running overwrites it. Beside the three Markdown files sit
`before.json` and `after.json`, the `verification-*.json` verdict on each
finding, and `alive2.json`, the verdict on every rewrite we submitted to Alive2.

## Benchmarks

Eight libraries that ship in production, pinned by SHA-256 in
[bench/corpora.py](bench/corpora.py): SQLite 3.45.1, zlib 1.3.1, Lua 5.4.6,
cJSON 1.7.18, bzip2 1.0.8, lz4 1.9.4, miniz 3.0.2, zip 0.3.2.


## Layout

```
run.py           setup | before | after | compare
common.py        toolchain, pinned download, plugin build
checks.py        the scope controls run by setup
src/             PathWitnessMaskWidth.cpp, PathWitnessShiftMask.cpp
bench/           benchmark definitions, the Z3 detector, an independent re-checker
tests/           fixtures, including the mechanically reduced real function
results/         BEFORE.md, AFTER.md, COMPARISON.md and the raw JSON behind them
```

Everything in `src/`, `bench/`, `tests/` and the runner is written for this
project, under Apache-2.0 with the LLVM exception (see LICENSE), the same terms
as LLVM itself so the passes can be offered upstream unchanged.

Two things are not ours and are marked as such. `tests/sqlite-mask-reduced.ll`
is unedited `llvm-reduce` output taken from SQLite 3.45.1, which is public
domain; `tests/sqlite-mask-reduced.provenance.json` records the source hash, the
tool and the output hash so the reduction can be repeated. The eight benchmark
libraries are downloaded at their own upstream URLs, pinned by SHA-256 in
[bench/corpora.py](bench/corpora.py), and none of their source is copied into
this repository.

The rewrite identity our first pass applies is not new and we do not claim it; report cites the closest
upstream work.

## Checking the detector

Both tools ship with fixtures whose answers are known:

```sh
python3 bench/mine_missed_folds.py --self-test   # 1 finding, 1 finding, 0 findings
python3 bench/verify_findings.py --self-test     # must refute both reachable controls
```
