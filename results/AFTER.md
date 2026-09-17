# After: the same benchmarks with both PathWitness passes

Each branch edge is handed to Z3 together with the facts carried by the edges
that dominate it. A branch is counted only when the solver proves no input can
reach it. Unmodelled operations become free variables, so the count is a lower
bound rather than an estimate.

- Compiler: clang version 23.1.1 (https://github.com/llvm/llvm-project 6dfe1677ab8dffbc6ec13d53a1e0215d75147689)
- Host: Linux-6.12.107+deb13-amd64-x86_64-with-glibc2.41
- Benchmarks: 8 libraries, 3034 functions, 44556 branch edges
- Solver timeouts (never counted as a finding): 1
- Independent re-check: 9 proved dead over the whole input space, 20 found no counterexample in a sample,
  1 refuted and excluded from the table below
- Run time: 2 min 33 s

## Branches that can never be taken, and that still survive

| Benchmark | Version | Unreachable branches kept |
|---|---|---:|
| sqlite | 3.45.1 | 20 |
| zlib | 1.3.1 | 0 |
| lua | 5.4.6 | 2 |
| cjson | 1.7.18 | 0 |
| bzip2 | 1.0.8 | 0 |
| lz4 | 1.9.4 | 0 |
| miniz | 3.0.2 | 0 |
| zip | 0.3.2 | 7 |
| **Total** | | **29** |

### What proving them dead requires

| Reasoning needed to prove it dead | Sites |
|---|---:|
| equality | 12 |
| bitmask, equality | 8 |
| equality, ordering | 4 |
| ordering, width-cast | 1 |
| bitmask, equality, width-cast **(in scope for these passes)** | 1 |
| bitmask, disequality, equality | 1 |
| bitmask, equality, ordering, width-cast **(in scope for these passes)** | 1 |
| equality, width-cast | 1 |
| **In scope for these passes** | **2** |

### Where they are

- `luaH_resize` in lua - needs bitmask, equality, ordering, width-cast
- `str_find_aux` in lua - needs equality, ordering
- `compoundHasDifferentAffinities` in sqlite - needs bitmask, equality, width-cast
- `csv_read_one_field` in sqlite - needs bitmask, disequality, equality
- `decimalNewFromText` in sqlite - needs equality, ordering
- `seriesFilter` in sqlite - needs equality, ordering
- `sqlite3AddColumn` in sqlite - needs equality, ordering
- `sqlite3BtreeInsert` in sqlite - needs ordering, width-cast
- `sqlite3JoinType` in sqlite - needs equality
- `sqlite3Prepare` in sqlite (11 sites) - needs equality
- `sqlite3VdbeMemCopy` in sqlite - needs bitmask, equality
- `sqlite3_value_dup` in sqlite - needs bitmask, equality
- `mz_zip_reader_extract_iter_new` in zip (6 sites) - needs bitmask, equality
- `mz_zip_reader_locate_file_v2` in zip - needs equality, width-cast

## Does it still work?

Real SQLite 3.45.1 was compiled both ways and driven through its public
API. Both builds produced byte-identical output:

    PASS: 15 checked SQL groups; 1044 sqlite3_value_blob calls

The passes fired 545 times while compiling it.

### Machine code

- `sqlite3_value_blob`: 150 -> 125 bytes
- Across the whole module 14 functions shrank and 7 grew, for a net change of -244 bytes.
  Some functions grow because inlining decisions shift once a branch disappears. This is reported, not claimed as a gain.
