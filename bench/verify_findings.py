#!/usr/bin/env python3
"""

Usage:
    python3 bench/verify_findings.py                     # re-check results/before.json
    python3 bench/verify_findings.py --mining other.json --samples 400000
"""
import argparse
import json
from pathlib import Path
import random
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mine_missed_folds import parse_module            # parsing only, not encoding

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = object()


# ------------------------------------------------------------------ evaluation

def mask_to(value, width):
    return value & ((1 << width) - 1)


def signed(value, width):
    value = mask_to(value, width)
    return value - (1 << width) if value >> (width - 1) else value


class Evaluator:
    """Concrete LLVM integer semantics.  Deliberately written independently of
    the miner's symbolic encoder so that the two do not share a mistake."""

    def __init__(self, function):
        self.function = function
        self.width_cache = {}

    def width(self, name):
        if name in self.width_cache:
            return self.width_cache[name]
        body = self.function.defs.get(name, "")
        width = self.function.types.get(name)
        if not width:
            found = re.search(r"\bi(\d+)\b", body)
            width = int(found.group(1)) if found else 64
        self.width_cache[name] = width
        return width

    def operand(self, token, env, width, depth):
        token = token.strip().rstrip(",")
        if re.match(r"^-?\d+$", token):
            return mask_to(int(token), width)
        if token == "true":
            return 1
        if token in ("false", "null", "zeroinitializer"):
            return 0
        if token.startswith("%"):
            return self.value(token, env, depth + 1)
        return UNKNOWN                         # globals, undef, poison, exprs

    def value(self, name, env, depth=0):
        if name in env:
            return env[name]
        if depth > 64:
            return UNKNOWN
        body = self.function.defs.get(name)
        if body is None:
            return UNKNOWN
        result = self.compute(body, env, depth)
        return result

    def compute(self, body, env, depth):
        head = body.split()[0] if body.split() else ""
        width_match = re.search(r"\bi(\d+)\b", body)

        binops = {"and", "or", "xor", "add", "sub", "mul", "shl", "lshr", "ashr"}
        if head in binops:
            match = re.match(r"^\w+\b[^%\-\d]*?\bi(\d+)\s+(.+)$", body)
            if not match:
                return UNKNOWN
            width = int(match.group(1))
            parts = split_top(match.group(2))
            if len(parts) < 2:
                return UNKNOWN
            left = self.operand(parts[0], env, width, depth)
            right = self.operand(parts[1], env, width, depth)
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            if head == "and":
                return left & right
            if head == "or":
                return left | right
            if head == "xor":
                return left ^ right
            if head == "add":
                return mask_to(left + right, width)
            if head == "sub":
                return mask_to(left - right, width)
            if head == "mul":
                return mask_to(left * right, width)
            if right >= width:
                return UNKNOWN                 # poison: abstain, never refute
            if head == "shl":
                return mask_to(left << right, width)
            if head == "lshr":
                return left >> right
            if head == "ashr":
                return mask_to(signed(left, width) >> right, width)

        if head == "icmp":
            match = re.match(r"^icmp\s+(?:samesign\s+)?(\w+)\s+i(\d+)\s+(.+)$", body)
            if not match:
                return UNKNOWN
            predicate, width = match.group(1), int(match.group(2))
            parts = split_top(match.group(3))
            if len(parts) < 2:
                return UNKNOWN
            left = self.operand(parts[0], env, width, depth)
            right = self.operand(parts[1], env, width, depth)
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            sleft, sright = signed(left, width), signed(right, width)
            table = {"eq": left == right, "ne": left != right,
                     "ugt": left > right, "uge": left >= right,
                     "ult": left < right, "ule": left <= right,
                     "sgt": sleft > sright, "sge": sleft >= sright,
                     "slt": sleft < sright, "sle": sleft <= sright}
            if predicate not in table:
                return UNKNOWN
            return 1 if table[predicate] else 0

        cast = re.match(r"^(zext|sext|trunc)\b.*?\bi(\d+)\s+(\S+)\s+to\s+i(\d+)", body)
        if cast:
            kind, from_width = cast.group(1), int(cast.group(2))
            to_width = int(cast.group(4))
            source = self.operand(cast.group(3), env, from_width, depth)
            if source is UNKNOWN:
                return UNKNOWN
            if kind == "trunc":
                return mask_to(source, to_width)
            if kind == "zext":
                return source
            return mask_to(signed(source, from_width), to_width)

        if head == "select":
            match = re.match(r"^select\s+i1\s+(\S+)\s*,\s*i(\d+)\s+(\S+)\s*,\s*i(\d+)\s+(\S+)",
                             body)
            if match:
                condition = self.operand(match.group(1), env, 1, depth)
                if condition is UNKNOWN:
                    return UNKNOWN
                if condition:
                    return self.operand(match.group(3), env, int(match.group(2)), depth)
                return self.operand(match.group(5), env, int(match.group(4)), depth)
        return UNKNOWN


class Program:
    """A slice compiled once into straight-line steps.

    The evaluator above re-parses instruction text on every assignment, which
    caps exhaustive search at a few million.  Parsing each instruction once and
    then running a tight loop is enough to prove several more findings over
    their entire input space instead of merely sampling them.
    """

    def __init__(self, function, evaluator, roots):
        self.steps = []                 # (dest, op, args, width, extra)
        self.leaves = {}
        order, seen = [], set()

        def visit(name):
            if name in seen:
                return
            seen.add(name)
            body = function.defs.get(name)
            parsed = parse_inst(body) if body else None
            if parsed is None:
                self.leaves[name] = evaluator.width(name)
                return
            for kind, value in parsed[1]:
                if kind == "ssa":
                    visit(value)
            order.append((name, parsed))

        for root in roots:
            visit(root)
        for name, (op, args, width, extra) in order:
            self.steps.append((name, op, args, width, extra))

    def run(self, env):
        for dest, op, args, width, extra in self.steps:
            values = []
            for kind, value in args:
                if kind == "const":
                    values.append(mask_to(value, width))
                else:
                    got = env.get(value, UNKNOWN)
                    if got is UNKNOWN:
                        env[dest] = UNKNOWN
                        break
                    values.append(got)
            else:
                env[dest] = apply_op(op, values, width, extra)
        return env


def parse_inst(body):
    """(op, [(kind, value)], width, extra) or None when not modelled."""
    if not body:
        return None
    head = body.split()[0] if body.split() else ""
    binops = {"and", "or", "xor", "add", "sub", "mul", "shl", "lshr", "ashr"}

    def operands(text, count):
        """`count` matters: a cast has one operand, and demanding two silently
        turned every cast into a free variable, which fabricates
        counterexamples by letting a value vary independently of its source."""
        result = []
        for token in split_top(text)[:count]:
            token = token.strip().rstrip(",")
            if re.match(r"^-?\d+$", token):
                result.append(("const", int(token)))
            elif token == "true":
                result.append(("const", 1))
            elif token in ("false", "null", "zeroinitializer"):
                result.append(("const", 0))
            elif token.startswith("%"):
                result.append(("ssa", token))
            else:
                return None
        return result if len(result) == count else None

    if head in binops:
        match = re.match(r"^\w+\b[^%\-\d]*?\bi(\d+)\s+(.+)$", body)
        if match:
            args = operands(match.group(2), 2)
            if args:
                return (head, args, int(match.group(1)), None)
        return None

    if head == "icmp":
        match = re.match(r"^icmp\s+(?:samesign\s+)?(\w+)\s+i(\d+)\s+(.+)$", body)
        if match:
            args = operands(match.group(3), 2)
            if args:
                return ("icmp", args, int(match.group(2)), match.group(1))
        return None

    cast = re.match(r"^(zext|sext|trunc)\b.*?\bi(\d+)\s+(\S+)\s+to\s+i(\d+)", body)
    if cast:
        args = operands(cast.group(3), 1)
        if args:
            return (cast.group(1), args, int(cast.group(4)), int(cast.group(2)))
    return None


def apply_op(op, values, width, extra):
    if op == "and":
        return values[0] & values[1]
    if op == "or":
        return values[0] | values[1]
    if op == "xor":
        return values[0] ^ values[1]
    if op == "add":
        return mask_to(values[0] + values[1], width)
    if op == "sub":
        return mask_to(values[0] - values[1], width)
    if op == "mul":
        return mask_to(values[0] * values[1], width)
    if op in ("shl", "lshr", "ashr"):
        if values[1] >= width:
            return UNKNOWN                    # poison: abstain
        if op == "shl":
            return mask_to(values[0] << values[1], width)
        if op == "lshr":
            return mask_to(values[0], width) >> values[1]
        return mask_to(signed(values[0], width) >> values[1], width)
    if op == "icmp":
        left, right = mask_to(values[0], width), mask_to(values[1], width)
        sleft, sright = signed(left, width), signed(right, width)
        table = {"eq": left == right, "ne": left != right,
                 "ugt": left > right, "uge": left >= right,
                 "ult": left < right, "ule": left <= right,
                 "sgt": sleft > sright, "sge": sleft >= sright,
                 "slt": sleft < sright, "sle": sleft <= sright}
        if extra not in table:
            return UNKNOWN
        return 1 if table[extra] else 0
    if op == "zext":
        return mask_to(values[0], extra)
    if op == "trunc":
        return mask_to(values[0], width)
    if op == "sext":
        return mask_to(signed(mask_to(values[0], extra), extra), width)
    return UNKNOWN


def split_top(text):
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


# ----------------------------------------------------------------- free inputs

def free_inputs(function, evaluator, roots, limit=8):
    """SSA names the slice depends on that have no computable definition."""
    seen, leaves, frontier = set(), {}, list(roots)
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        body = function.defs.get(name)
        if body is None:
            leaves[name] = evaluator.width(name)
            continue
        head = body.split()[0] if body.split() else ""
        known = {"and", "or", "xor", "add", "sub", "mul", "shl", "lshr", "ashr",
                 "icmp", "zext", "sext", "trunc", "select"}
        if head not in known:
            leaves[name] = evaluator.width(name)
            continue
        frontier.extend(re.findall(r"%[\w.$\-]+", body))
        if len(leaves) > limit:
            break
    return leaves


def facts_and_query(function, finding):
    """Rebuild (fact conditions with polarity, query condition, dead value)."""
    facts = []
    for edge in finding["necessary_edges"]:
        parent, target = [part.strip() for part in edge.split("->")]
        term = function.term.get(parent, "")
        match = re.match(r"^br i1 (\S+?),\s*label (%[\w.$\-]+),\s*label (%[\w.$\-]+)",
                         term)
        if not match:
            return None
        condition, true_label, _ = match.groups()
        facts.append((condition, target == true_label))
    term = function.term.get(finding["block"], "")
    match = re.match(r"^br i1 (\S+?),\s*label (%[\w.$\-]+),\s*label (%[\w.$\-]+)", term)
    if not match:
        return None
    query, true_label, _ = match.groups()
    return facts, query, 1 if finding["edge"] == true_label else 0


def verify(function, finding, samples, exhaustive_limit, rng):
    rebuilt = facts_and_query(function, finding)
    if rebuilt is None:
        return "UNPARSED", "could not rebuild the branch structure", 0
    facts, query, dead_value = rebuilt

    evaluator = Evaluator(function)
    roots = [name for name, _ in facts] + [query]
    program = Program(function, evaluator, roots)
    leaves = program.leaves
    if not leaves:
        return "NO-INPUTS", "slice has no free inputs", 0

    names = sorted(leaves)
    widths = [leaves[n] for n in names]
    space = 1
    for width in widths:
        space *= 1 << width
        if space > exhaustive_limit:
            break

    def attempt(assignment):
        env = program.run(dict(zip(names, assignment)))
        for name, polarity in facts:
            got = env.get(name, UNKNOWN)
            if got is UNKNOWN:
                return None                       # abstain
            if bool(got) != polarity:
                return False                      # facts do not hold
        got = env.get(query, UNKNOWN)
        if got is UNKNOWN:
            return None
        return got == dead_value                  # True => counterexample

    # A name treated as a free input while the tree-walking evaluator can
    # actually compute it means the slice was cut in the wrong place, and the
    # search would vary a value independently of what defines it - which
    # fabricates counterexamples.  Refuse to report anything in that case.
    for leaf in leaves:
        if function.defs.get(leaf) and evaluator.compute(
                function.defs[leaf], {}, 0) is not UNKNOWN:
            return "INCONSISTENT", f"{leaf} treated as free but is computable", 0

    # The compiled slice and the tree-walking evaluator are separate code
    # paths; make sure they agree before trusting the fast one.
    for _ in range(64):
        probe = tuple(rng.getrandbits(w) for w in widths)
        env = program.run(dict(zip(names, probe)))
        for root in roots:
            fast, slow = env.get(root, UNKNOWN), evaluator.value(
                root, dict(zip(names, probe)))
            if fast is not UNKNOWN and slow is not UNKNOWN and fast != slow:
                return "INCONSISTENT", f"evaluators disagree on {root}", 0

    checked = 0
    if space <= exhaustive_limit:
        totals = [1 << w for w in widths]
        index = [0] * len(names)
        while True:
            outcome = attempt(tuple(index))
            checked += 1
            if outcome:
                return "REFUTED", f"counterexample {dict(zip(names, index))}", checked
            position = len(index) - 1
            while position >= 0:
                index[position] += 1
                if index[position] < totals[position]:
                    break
                index[position] = 0
                position -= 1
            if position < 0:
                break
        return "CONFIRMED", f"exhaustive over {checked} assignments", checked

    interesting = []
    for width in widths:
        top = (1 << width) - 1
        interesting.append([0, 1, 2, 3, top, top - 1, top >> 1, (top >> 1) + 1])
    for _ in range(samples):
        if rng.random() < 0.25:
            assignment = tuple(rng.choice(values) for values in interesting)
        else:
            assignment = tuple(rng.getrandbits(w) for w in widths)
        outcome = attempt(assignment)
        checked += 1
        if outcome:
            return "REFUTED", f"counterexample {dict(zip(names, assignment))}", checked
    return "NO-COUNTEREXAMPLE", f"{checked} sampled assignments, space ~2^{sum(widths)}", checked


def self_test(samples, exhaustive_limit, seed):
    """A checker that cannot refute anything would report every finding as
    confirmed.  Feed it branches that are genuinely reachable, asserted to be
    dead, and require it to produce a counterexample for each."""
    path = ROOT / "tests/miner-controls.ll"
    if not path.is_file():
        print("missing", path)
        return 1
    functions = {f.name: f for f in parse_module(path.read_text())}
    rng = random.Random(seed)
    failures = 0
    for name in ("reachable_no_conflict", "reachable_unrelated"):
        function = functions[name]
        for block, term in function.term.items():
            match = re.match(r"^br i1 (\S+?),\s*label (%[\w.$\-]+),\s*label (%[\w.$\-]+)",
                             term)
            if not match or block == "%entry":
                continue
            claim = dict(module=path.name, function=name, block=block,
                         edge=match.group(2), condition=match.group(1),
                         condition_ir=function.defs.get(match.group(1), ""),
                         necessary_edges=["%entry -> " + block])
            verdict, detail, _ = verify(function, claim, samples,
                                        exhaustive_limit, rng)
            ok = verdict == "REFUTED"
            failures += 0 if ok else 1
            print(f"  [{'PASS' if ok else 'FAIL'}] {name:<26} expected REFUTED, "
                  f"got {verdict} - {detail[:40]}")
    print("\nverifier self-test:", "PASS" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mining", type=Path, default=ROOT / "results/before.json")
    parser.add_argument("--ir-dir", type=Path, default=ROOT.parent / ".pathwitness-work/before")
    parser.add_argument("--samples", type=int, default=200000)
    parser.add_argument("--exhaustive-limit", type=int, default=1 << 22)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--out", type=Path, default=ROOT / "results/verification.json")
    parser.add_argument("--self-test", action="store_true",
                        help="require the checker to refute known-reachable branches")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.samples, args.exhaustive_limit, args.seed)

    data = json.loads(args.mining.read_text())
    findings = data["findings"]
    rng = random.Random(args.seed)

    modules, results, tally = {}, [], {}
    for finding in findings:
        path = args.ir_dir / finding["module"]
        if not path.is_file():
            path = ROOT / "fixtures" / finding["module"]
        if finding["module"] not in modules:
            if not path.is_file():
                modules[finding["module"]] = None
            else:
                modules[finding["module"]] = {f.name: f for f in
                                              parse_module(path.read_text())}
        table = modules[finding["module"]]
        if table is None or finding["function"] not in table:
            verdict, detail, checked = "NO-MODULE", "IR not available", 0
        else:
            verdict, detail, checked = verify(table[finding["function"]], finding,
                                              args.samples, args.exhaustive_limit, rng)
        tally[verdict] = tally.get(verdict, 0) + 1
        results.append(dict(finding, verdict=verdict, detail=detail, checked=checked))
        flag = {"CONFIRMED": "PASS", "REFUTED": "FAIL"}.get(verdict, "....")
        print(f"  [{flag}] {finding['function'][:26]:26s} {finding['condition_ir'][:30]:30s} "
              f"{verdict} - {detail[:46]}", flush=True)

    print("\n" + "=" * 70)
    for verdict, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}  {verdict}")
    refuted = tally.get("REFUTED", 0)
    print("=" * 70)
    if refuted:
        print(f"{refuted} finding(s) REFUTED - the miner has a false positive.")
    else:
        print("No finding refuted by independent concrete evaluation.")
    print("CONFIRMED = no counterexample exists over the entire input space.")
    print("NO-COUNTEREXAMPLE = none found in a sample; weaker, not a proof.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dict(tally=tally, results=results), indent=2))
    print(f"report {args.out}")
    return 1 if refuted else 0


if __name__ == "__main__":
    sys.exit(main())
