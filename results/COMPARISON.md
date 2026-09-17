# Before and after

Both columns come from the same sources, compiled with the same flags by
clang version 23.1.1, and given the same local cleanup afterwards. The only
difference is whether the two PathWitness passes ran first.

| Benchmark | Before | After | Removed |
|---|---:|---:|---:|
| sqlite | 28 | 20 | 8 |
| lua | 2 | 2 | 0 |
| zip | 7 | 7 | 0 |
| **Total** | **37** | **29** | **8** |

Both runs exclude findings an independent, solver-free re-check could
refute (1 before, 1 after). BEFORE.md and AFTER.md give that breakdown.

## What was removed

Of the 37 unreachable branches stock `-O3` keeps, 12 need both bitmask
reasoning and a width cast to prove dead. That combination is what LLVM loses,
and it is the only thing these two passes address. A branch whose proof needs
ordering reasoning is out of scope and we do not count it.

**8 of those 12 sites are gone, across 6 of 10 functions:**

- `isLikeOrGlob` in sqlite - 1 site
- `jsonInsertIntoBlob` in sqlite - 1 site
- `sqlite3ExprCompare` in sqlite - 1 site
- `sqlite3RunVacuum` in sqlite - 1 site
- `sqlite3_value_blob` in sqlite - 3 sites
- `trimFunc` in sqlite - 1 site

The other 4 remain. 2 of them (`sqlite3VdbeMemCopy`, `sqlite3_value_dup`) no longer
need a width cast to prove dead, because the cast is what our pass removed,
but the branch itself survives. Those count as not fixed.

## What this is not

These branches were never taken at run time, so removing them is dead-code
removal, not a speedup. AFTER.md records that real SQLite compiled with the
passes answers its API checks byte-identically. FINDINGS.md gives the full
scope.
