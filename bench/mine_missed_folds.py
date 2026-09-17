#!/usr/bin/env python3
"""

Usage:
    python3 bench/mine_missed_folds.py --corpus bench/corpus
    python3 bench/mine_missed_folds.py file.ll [file2.ll ...]
"""
import argparse
import collections
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import z3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpora import CORPORA, resolve

ROOT = Path(__file__).resolve().parents[1]
INT = re.compile(r"^i(\d+)$")


# --------------------------------------------------------------------- parsing

class Function:
    def __init__(self, name):
        self.name = name
        self.blocks = []                     # ordered label list
        self.insts = collections.OrderedDict()   # label -> [(dest, text)]
        self.term = {}                       # label -> terminator text
        self.types = {}                      # ssa name -> bit width
        self.defs = {}                       # ssa name -> instruction text


def parse_module(text):
    """Parse printed LLVM IR.  Unrecognised constructs are left for the encoder
    to turn into unconstrained variables, so parsing is intentionally loose."""
    functions = []
    current = None
    label = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.match(r"^define\b.*?@([\w.$\-]+|\"[^\"]+\")\s*\((.*)$", line)
        if header:
            current = Function(header.group(1).strip('"'))
            functions.append(current)
            label = "%entry"
            current.blocks.append(label)
            current.insts[label] = []
            # Record argument widths.  Without these an argument defaults to 64
            # bits, which inflates the apparent input space and pushes
            # verification from exhaustive proof down to sampling.
            for parameter in split_operands(header.group(2).rsplit(")", 1)[0]):
                name = re.search(r"(%[\w.$\-]+)\s*$", parameter)
                kind = re.match(r"^\s*i(\d+)\b", parameter)
                if name and kind:
                    current.types[name.group(1)] = int(kind.group(1))
            continue
        if current is None:
            continue
        if stripped == "}":
            current = None
            continue
        block = re.match(r"^([\w.$\-]+):", line)
        if block:
            label = "%" + block.group(1)
            if label not in current.insts:
                current.blocks.append(label)
                current.insts[label] = []
            continue
        if not stripped or stripped.startswith(";"):
            continue
        if re.match(r"^(br|ret|switch|unreachable|indirectbr|resume)\b", stripped):
            current.term[label] = stripped
            continue
        assign = re.match(r"^(%[\w.$\-]+)\s*=\s*(.*)$", stripped)
        if assign:
            dest, body = assign.group(1), assign.group(2)
            current.insts[label].append((dest, body))
            current.defs[dest] = body
            width = re.search(r"\bi(\d+)\b", body)
            if re.match(r"^icmp\b", body):
                current.types[dest] = 1
            elif re.match(r"^(zext|sext|trunc)\b", body):
                to = re.search(r"\bto\s+i(\d+)", body)
                current.types[dest] = int(to.group(1)) if to else None
            elif width:
                current.types[dest] = int(width.group(1))
        else:
            current.insts[label].append((None, stripped))
    for function in functions:
        # drop blocks that never received a terminator (parse fell out of sync)
        function.blocks = [b for b in function.blocks if b in function.term]
    return functions


def successors(term):
    if term.startswith("br i1"):
        labels = re.findall(r"label (%[\w.$\-]+)", term)
        return labels if len(labels) == 2 else []
    if term.startswith("br label"):
        return re.findall(r"label (%[\w.$\-]+)", term)
    if term.startswith("switch"):
        return re.findall(r"label (%[\w.$\-]+)", term)
    return []


def dominators(function):
    """Iterative dominator computation. Returns label -> set of dominators."""
    blocks = function.blocks
    if not blocks:
        return {}
    index = {b: i for i, b in enumerate(blocks)}
    preds = collections.defaultdict(list)
    for block in blocks:
        for successor in successors(function.term[block]):
            if successor in index:
                preds[successor].append(block)
    entry = blocks[0]
    dom = {b: set(blocks) for b in blocks}
    dom[entry] = {entry}
    changed = True
    guard = 0
    while changed and guard < 1000:
        changed, guard = False, guard + 1
        for block in blocks[1:]:
            if not preds[block]:
                new = {block}
            else:
                new = set(blocks)
                for predecessor in preds[block]:
                    new &= dom[predecessor]
                new = new | {block}
            if new != dom[block]:
                dom[block] = new
                changed = True
    return dom, preds


# -------------------------------------------------------------------- encoding

class Encoder:
    """Maps SSA values to Z3 bitvectors.  One variable per SSA name, always."""

    def __init__(self, function):
        self.function = function
        self.cache = {}
        self.unknown = 0
        self.modelled = set()

    def width_of(self, name):
        width = self.function.types.get(name)
        return width if width else 64

    def fresh(self, width):
        self.unknown += 1
        return z3.BitVec(f"unknown.{self.unknown}", width)

    def operand(self, token, width):
        token = token.strip().rstrip(",")
        if token in ("true", "1") and width == 1:
            return z3.BitVecVal(1, 1)
        if token in ("false", "0") and width == 1:
            return z3.BitVecVal(0, 1)
        if re.match(r"^-?\d+$", token):
            return z3.BitVecVal(int(token), width)
        if token in ("null", "zeroinitializer"):
            return z3.BitVecVal(0, width)
        if token.startswith("%"):
            return self.value(token, width)
        return self.fresh(width)                 # globals, undef, poison, exprs

    def value(self, name, width=None):
        if name in self.cache:
            return self.cache[name]
        width = width or self.width_of(name)
        # Break cycles (phi nodes referring to themselves) with a placeholder.
        placeholder = self.fresh(width)
        self.cache[name] = placeholder
        body = self.function.defs.get(name)
        if body is None:
            return placeholder
        result = self.encode(name, body, width)
        if result is not None:
            if result.size() != width:
                result = placeholder
            else:
                self.cache[name] = result
                self.modelled.add(name)
                return result
        return placeholder

    BINOPS = {"and": lambda a, b: a & b, "or": lambda a, b: a | b,
              "xor": lambda a, b: a ^ b, "add": lambda a, b: a + b,
              "sub": lambda a, b: a - b, "mul": lambda a, b: a * b,
              "shl": lambda a, b: a << b, "lshr": z3.LShR,
              "ashr": lambda a, b: a >> b}

    PREDS = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
             "ugt": z3.UGT, "uge": z3.UGE, "ult": z3.ULT, "ule": z3.ULE,
             "sgt": lambda a, b: a > b, "sge": lambda a, b: a >= b,
             "slt": lambda a, b: a < b, "sle": lambda a, b: a <= b}

    def encode(self, name, body, width):
        binop = re.match(r"^(\w+)\b[^%\-\d]*?\bi(\d+)\s+(.+)$", body)
        head = body.split()[0]

        if head in self.BINOPS and binop:
            operand_width = int(binop.group(2))
            parts = split_operands(binop.group(3))
            if len(parts) < 2:
                return None
            left = self.operand(parts[0], operand_width)
            right = self.operand(parts[1], operand_width)
            return self.BINOPS[head](left, right)

        if head == "icmp":
            match = re.match(r"^icmp\s+(?:samesign\s+)?(\w+)\s+i(\d+)\s+(.+)$", body)
            if not match:
                return None
            predicate, operand_width = match.group(1), int(match.group(2))
            if predicate not in self.PREDS:
                return None
            parts = split_operands(match.group(3))
            if len(parts) < 2:
                return None
            left = self.operand(parts[0], operand_width)
            right = self.operand(parts[1], operand_width)
            return z3.If(self.PREDS[predicate](left, right),
                         z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))

        cast = re.match(r"^(zext|sext|trunc)\b.*?\bi(\d+)\s+(\S+)\s+to\s+i(\d+)", body)
        if cast:
            kind, from_width = cast.group(1), int(cast.group(2))
            to_width = int(cast.group(4))
            source = self.operand(cast.group(3), from_width)
            if kind == "trunc":
                return z3.Extract(to_width - 1, 0, source) if to_width <= from_width else None
            if to_width < from_width:
                return None
            pad = to_width - from_width
            return z3.ZeroExt(pad, source) if kind == "zext" else z3.SignExt(pad, source)

        if head == "select":
            match = re.match(r"^select\s+i1\s+(\S+)\s*,\s*i(\d+)\s+(\S+)\s*,\s*i(\d+)\s+(\S+)",
                             body)
            if match:
                condition = self.operand(match.group(1), 1)
                true_value = self.operand(match.group(3), int(match.group(2)))
                false_value = self.operand(match.group(5), int(match.group(4)))
                return z3.If(condition == 1, true_value, false_value)
        return None                                   # phi, load, call, ...


def split_operands(text):
    depth, current, parts = 0, "", []
    for char in text:
        if char in "([<":
            depth += 1
        elif char in ")]>":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return parts


# --------------------------------------------------------------------- mining

class Fact:
    __slots__ = ("expr", "source", "text")

    def __init__(self, expr, source, text):
        self.expr, self.source, self.text = expr, source, text


def dominating_facts(function, dom, preds, block, encoder):
    """Facts from edges that dominate `block` and are the sole entry to their
    target.  Anything weaker is dropped rather than approximated."""
    facts = []
    # `block` itself is included: the edge entering it carries a fact that
    # holds throughout it.  The entry block is excluded naturally, having no
    # predecessor.
    for dominator in dom[block]:
        if len(preds[dominator]) != 1:
            continue                                  # not a single edge
        parent = preds[dominator][0]
        term = function.term.get(parent, "")
        if not term.startswith("br i1"):
            continue
        match = re.match(r"^br i1 (\S+?),\s*label (%[\w.$\-]+),\s*label (%[\w.$\-]+)",
                         term)
        if not match:
            continue
        condition, true_label, false_label = match.groups()
        if dominator == true_label:
            taken = True
        elif dominator == false_label:
            taken = False
        else:
            continue
        value = encoder.operand(condition, 1)
        facts.append(Fact(value == 1 if taken else value == 0,
                          condition, f"{parent} -> {dominator}"))
    return facts


def slice_back(function, roots, depth=6):
    """Instruction texts reachable backwards from `roots` through SSA operands.

    The reasoning a fold needs is spread over the whole def chain, not just the
    comparison: the mask is one instruction down and the width cast another.
    Bounded so a long chain cannot dominate the classification.
    """
    seen, texts, frontier = set(), [], list(roots)
    for _ in range(depth):
        following = []
        for name in frontier:
            if name in seen:
                continue
            seen.add(name)
            body = function.defs.get(name, "")
            if not body:
                continue
            texts.append(body)
            following.extend(re.findall(r"%[\w.$\-]+", body))
        frontier = following
    return texts


def classify(function, necessary, query_condition):
    """Read the required reasoning off the minimal fact set and the query.

    A fold relates a fact to a query, so both sides are inspected.  Only facts
    that ablation proved necessary contribute.
    """
    roots = [fact.source for fact in necessary] + [query_condition]
    joined = " ; ".join(slice_back(function, roots))
    tags = set()
    if re.search(r"\band\b.*\b\d+\b", joined):
        tags.add("bitmask")
    if re.search(r"\b(zext|sext|trunc)\b", joined):
        tags.add("width-cast")
    if re.search(r"\bicmp\s+(?:samesign\s+)?eq\b", joined):
        tags.add("equality")
    if re.search(r"\bicmp\s+(?:samesign\s+)?ne\b", joined):
        tags.add("disequality")
    if re.search(r"\bicmp\s+(?:samesign\s+)?[us](?:lt|le|gt|ge)\b", joined):
        tags.add("ordering")
    return sorted(tags) or ["other"]


def mine_function(function, timeout_ms, max_blocks):
    if len(function.blocks) > max_blocks:
        return [], 0, 1
    dom, preds = dominators(function)
    findings, queries, timeouts = [], 0, 0
    encoder = Encoder(function)

    for block in function.blocks:
        term = function.term.get(block, "")
        if not term.startswith("br i1"):
            continue
        match = re.match(r"^br i1 (\S+?),\s*label (%[\w.$\-]+),\s*label (%[\w.$\-]+)",
                         term)
        if not match:
            continue
        condition = match.group(1)
        if not condition.startswith("%"):
            continue
        facts = dominating_facts(function, dom, preds, block, encoder)
        if not facts:
            continue
        value = encoder.operand(condition, 1)
        if condition not in encoder.modelled:
            continue                          # condition itself is opaque

        for taken, label in ((True, match.group(2)), (False, match.group(3))):
            queries += 1
            goal = value == (1 if taken else 0)
            solver = z3.Solver()
            solver.set("timeout", timeout_ms)
            solver.add(*[f.expr for f in facts], goal)
            verdict = solver.check()
            if verdict == z3.unknown:
                timeouts += 1
                continue
            if verdict != z3.unsat:
                continue

            # Ablation: keep only facts whose removal restores satisfiability.
            necessary = []
            for candidate in facts:
                reduced = [f.expr for f in facts if f is not candidate]
                probe = z3.Solver()
                probe.set("timeout", timeout_ms)
                probe.add(*reduced, goal)
                if probe.check() != z3.unsat:
                    necessary.append(candidate)
            if not necessary:
                continue                       # contradiction without any fact
            findings.append(dict(
                function=function.name, block=block, edge=label,
                condition=condition,
                condition_ir=function.defs.get(condition, "").strip(),
                facts_available=len(facts), facts_necessary=len(necessary),
                necessary_edges=[f.text for f in necessary],
                needs=classify(function, necessary, condition)))
    return findings, queries, timeouts


# --------------------------------------------------------------------- driver

def compile_to_ir(clang, source, flags, out, include, opt="O3"):
    args = [clang, f"-{opt}", "-S", "-emit-llvm", *flags]
    for path in include:
        args += ["-I", str(path)]
    args += [str(source), "-o", str(out)]
    return subprocess.run(args, capture_output=True, text=True).returncode == 0


CLEANUP = "instcombine,simplifycfg"
PASSES = "path-witness-mask-width,path-witness-shift-mask"


def cleanup(opt_tool, module, plugin_dir):
    """Apply the same local cleanup to both arms.

    Without this the comparison would be unfair: our passes are followed by
    instcombine and simplifycfg, so the baseline must get them too.  The only
    difference between the two arms is whether the two PathWitness passes run
    first.
    """
    arm = "after" if plugin_dir else "before"
    out = module.with_name(module.stem + f"-{arm}.ll")
    if out.exists():
        return out
    loads, pipeline = [], CLEANUP
    if plugin_dir:
        loads = ["-load-pass-plugin=" + str(plugin_dir / f"PathWitness{n}.so")
                 for n in ("MaskWidth", "ShiftMask")]
        pipeline = PASSES + "," + CLEANUP
    done = subprocess.run([opt_tool, *loads, f"-passes=function({pipeline})",
                           "-S", str(module), "-o", str(out)],
                          capture_output=True, text=True)
    return out if done.returncode == 0 else module


def self_test(timeout_ms, max_blocks):
    """A measurement tool is only as good as its false-positive rate.

    Positive fixtures contain a branch that is genuinely never taken and must
    be found.  Control fixtures contain branches that ARE reachable - no
    conflict, an unrelated value, and a query the guard does not dominate -
    and must produce nothing at all.
    """
    expectations = [
        (ROOT / "tests/sqlite-mask-width.ll", 1, "known dead branch"),
        (ROOT / "tests/sqlite-mask-reduced.ll", 1, "reduced real function"),
        (ROOT / "tests/miner-controls.ll", 0, "three reachable branches"),
    ]
    failures = 0
    for path, expected, note in expectations:
        if not path.is_file():
            print(f"  [----] {path.name:<28} missing")
            continue
        found = []
        for function in parse_module(path.read_text()):
            results, _, _ = mine_function(function, timeout_ms, max_blocks)
            found.extend(results)
        ok = len(found) == expected
        failures += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {path.name:<28} "
              f"{len(found)} finding(s), expected {expected} ({note})")
    print("\nminer self-test:", "PASS" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", type=Path, help="pre-built .ll files")
    parser.add_argument("--corpus", type=Path,
                        help="directory of application sources (compiles them first)")
    parser.add_argument("--opt", default="O3", choices=["O1", "O2", "O3", "Os"],
                        help="optimisation level the corpus is compiled at")
    parser.add_argument("--timeout-ms", type=int, default=4000)
    parser.add_argument("--llvm-bin", type=Path,
                        help="toolchain directory; default is clang on PATH")
    parser.add_argument("--plugin-dir", type=Path,
                        help="run the PathWitness passes before mining (the AFTER arm)")
    parser.add_argument("--max-blocks", type=int, default=400,
                        help="skip functions larger than this")
    parser.add_argument("--work", type=Path, default=ROOT.parent / ".pathwitness-work/mining",
                        help="scratch directory for compiled IR, kept outside the repo")
    parser.add_argument("--out", type=Path, default=ROOT / "results/before.json")
    parser.add_argument("--self-test", action="store_true",
                        help="check the miner against known-dead and known-reachable "
                             "fixtures, then exit")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.timeout_ms, args.max_blocks)

    inputs = list(args.files)
    if args.corpus:
        clang = str(args.llvm_bin / "clang") if args.llvm_bin else (
            shutil.which("clang") or "clang")
        opt_tool = str(args.llvm_bin / "opt") if args.llvm_bin else (
            shutil.which("opt") or "opt")
        work = args.work
        work.mkdir(parents=True, exist_ok=True)
        for name in CORPORA:
            base, sources, flags = resolve(args.corpus, name)
            if not sources:
                print(f"  skip {name}: not present under {args.corpus}")
                continue
            for source in sources:
                target = work / f"{name}-{source.stem}-{args.opt}.ll"
                if target.exists() or compile_to_ir(clang, source, flags, target,
                                                    [base, source.parent], args.opt):
                    inputs.append(cleanup(opt_tool, target, args.plugin_dir))
                else:
                    print(f"  skip {source.name}: did not compile")

    if not inputs:
        raise SystemExit("nothing to analyse: pass .ll files or --corpus DIR")

    all_findings = []
    functions = branches = timeouts = skipped = 0
    for path in inputs:
        try:
            module = parse_module(path.read_text(errors="replace"))
        except Exception as error:
            print(f"  skip {path.name}: {error}")
            continue
        for function in module:
            functions += 1
            try:
                found, queries, skip = mine_function(function, args.timeout_ms,
                                                     args.max_blocks)
            except (RecursionError, z3.Z3Exception, MemoryError) as error:
                # One pathological function must not discard the whole run.
                # Skipped functions are reported, never silently counted as
                # "analysed with no finding".
                print(f"    skip {function.name}: {type(error).__name__}")
                skipped += 1
                continue
            branches += queries
            timeouts += skip if queries else 0
            skipped += 1 if (skip and not queries) else 0
            for item in found:
                item["module"] = path.name
            all_findings.extend(found)
        print(f"  {path.name}: {len(all_findings)} finding(s) so far", flush=True)

    need = collections.Counter()
    for finding in all_findings:
        need[", ".join(finding["needs"])] += 1

    print("\n" + "=" * 68)
    print(f"functions analysed      {functions}")
    print(f"functions skipped (big) {skipped}")
    print(f"branch edges queried    {branches}")
    print(f"solver timeouts         {timeouts}")
    print(f"provably-dead edges     {len(all_findings)}")
    if need:
        print("\nreasoning required (from the minimal necessary fact set):")
        for kind, count in need.most_common():
            print(f"  {count:5d}  {kind}")
    print("=" * 68)
    print("Counts are lower bounds: unmodelled operations become unconstrained")
    print("variables, and timeouts are never counted as findings.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dict(
        functions=functions, branch_edges=branches, timeouts=timeouts,
        skipped_functions=skipped, findings=all_findings,
        needs=dict(need)), indent=2))
    print(f"report {args.out}")


if __name__ == "__main__":
    sys.exit(main() or 0)
