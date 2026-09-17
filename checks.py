"""Scope controls for the generalized selector; synthetic, not benchmark wins."""
import json
import re
from common import ROOT, canonical, command as run, optimize

SEED = "; Hand-distilled from the mask/cast structure in sqlite3_value_blob.\n; This is a small IR reproducer, not a mechanical reduction of the whole function.\n; A straightforward equivalent C example already optimizes away on this compiler.\n; %flags is a defined 16-bit value. Another use keeps its 32-bit extension live.\ndeclare void @side_effect()\ndeclare void @consume(i32)\n\ndefine void @f(i16 noundef %flags) {\nentry:\n  %wide = zext i16 %flags to i32\n  %m = and i32 %wide, 18\n  %outer = icmp eq i32 %m, 0\n  br i1 %outer, label %check, label %other\ncheck:\n  %n = and i16 %flags, 514\n  %inner = icmp eq i16 %n, 514\n  br i1 %inner, label %dead, label %exit\ndead:\n  call void @side_effect()\n  br label %exit\nother:\n  %use = and i32 %wide, 1024\n  call void @consume(i32 %use)\n  br label %exit\nexit:\n  ret void\n}\n"

def fixtures(seed):
    # model = (side_effect count, consume count, final consumed value).
    base = ("0", "(flags & 18) != 0", "flags & 1024")
    yield "small", seed, 1, False, base
    yield "false-edge", seed.replace("%outer = icmp eq", "%outer = icmp ne").replace(
        "label %check, label %other", "label %other, label %check"), 1, False, base
    yield "commuted", seed.replace("and i32 %wide, 18", "and i32 18, %wide").replace(
        "icmp eq i32 %m, 0", "icmp eq i32 0, %m").replace(
        "and i16 %flags, 514", "and i16 514, %flags").replace(
        "icmp eq i16 %n, 514", "icmp eq i16 514, %n"), 1, False, base
    yield "high-mask-bits", seed.replace("and i32 %wide, 18", "and i32 %wide, 2147483666"), 1, False, base
    yield "query-ne", seed.replace("%inner = icmp eq", "%inner = icmp ne"), 1, True, (
        "(flags & 18) == 0", base[1], base[2])
    yield "reachable-mask", seed.replace("514", "512"), 0, True, (
        "(flags & 18) == 0 && (flags & 512) == 512", base[1], base[2])
    yield "after-merge", seed.replace("call void @consume(i32 %use)\n  br label %exit",
        "call void @consume(i32 %use)\n  br label %check"), 0, True, (
        "(flags & 514) == 514", base[1], base[2])
    yield "unrelated-source", seed.replace("%flags) {", "%flags, i16 noundef %second) {").replace(
        "%n = and i16 %flags", "%n = and i16 %second"), 0, True, (
        "(flags & 18) == 0 && (second & 514) == 514", base[1], base[2])
    yield "same-successor", seed.replace("label %check, label %other", "label %check, label %check"), 0, True, (
        "(flags & 514) == 514", "0", "0")
    yield "shared-mask", seed.replace("  br i1 %outer", "  call void @consume(i32 %m)\n  br i1 %outer"), 0, True, (
        "0", "1 + ((flags & 18) != 0)", "(flags & 18) != 0 ? (flags & 1024) : 0")
    frozen = seed.replace("  %wide = zext i16 %flags", "  %frozen = freeze i16 %flags\n  %wide = zext i16 %frozen")
    yield "freeze-boundary", frozen, 0, True, base
    yield "shared-frozen-source", frozen.replace("%n = and i16 %flags", "%n = and i16 %frozen"), 1, False, base
    # Unsupported instruction shapes: verify and check no rewrite. No execution
    # for nneg/undef/poison: enumerating normal integers would not test their semantics.
    yield "sign-extension", seed.replace("zext i16", "sext i16"), 0, None, None
    yield "nneg-extension", seed.replace("zext i16", "zext nneg i16"), 0, None, None
    for value in ["undef", "poison"]:
        yield "explicit-" + value, seed.replace("zext i16 %flags", "zext i16 " + value).replace(
            "and i16 %flags, 514", "and i16 " + value + ", 514"), 0, None, None
    yield "wide-128", seed.replace("to i32", "to i128").replace("and i32", "and i128").replace(
        "icmp eq i32", "icmp eq i128").replace("call void @consume(i32 %use)",
        "%use32 = trunc i128 %use to i32\n  call void @consume(i32 %use32)"), 0, None, None
    yield "vector", """declare void @side_effect()
define void @f(<2 x i16> noundef %flags) {
entry:
  %wide = zext <2 x i16> %flags to <2 x i32>
  %mask = and <2 x i32> %wide, <i32 18, i32 18>
  %eq = icmp eq <2 x i32> %mask, zeroinitializer
  %outer = extractelement <2 x i1> %eq, i32 0
  br i1 %outer, label %check, label %exit
check:
  %n = and <2 x i16> %flags, <i16 514, i16 514>
  %cmp = icmp eq <2 x i16> %n, <i16 514, i16 514>
  %inner = extractelement <2 x i1> %cmp, i32 0
  br i1 %inner, label %dead, label %exit
dead:
  call void @side_effect()
  br label %exit
exit:
  ret void
}
""", 0, None, None


def execute(llvm, directory, versions, model, second_arg=False):
    harness = directory / "harness.c"
    hits, consumed, last = model
    harness.write_text(f"""#include <stdio.h>
extern void f(unsigned short{', unsigned short' if second_arg else ''});
static unsigned hits, consumed, last;
void side_effect(void) {{ ++hits; }}
void consume(unsigned x) {{ ++consumed; last=x; }}
int main(void) {{
  _Static_assert(sizeof(unsigned short)==2, "requires 16-bit short");
  unsigned seconds[]={{0, 2, 18, 512, 514, 65535}};
  for (unsigned j=0; j<{6 if second_arg else 1}; ++j) {{
    unsigned second=seconds[j];
    for (unsigned flags=0; flags<65536; ++flags) {{
      hits=consumed=last=0;
      f((unsigned short)flags{', (unsigned short)second' if second_arg else ''});
      unsigned want_hits=({hits}), want_consumed=({consumed}), want_last=({last});
      if (hits!=want_hits || consumed!=want_consumed || (consumed && last!=want_last)) {{
        printf("Mismatch flags=%u second=%u: got %u/%u/%u expected %u/%u/%u\\n",
               flags,second,hits,consumed,last,want_hits,want_consumed,want_last);
        return 1;
      }}
    }}
  }}
  return 0;
}}
""")
    for path in versions:
        exe = directory / (path.stem + "-check")
        # Consume each recorded IR at O0 so the test does not add another O3 run.
        run([llvm / "clang", "-O0", path, harness, "-o", exe])
        run([exe])
    return 65536 * (6 if second_arg else 1) * len(versions)

def check(llvm, plugin, out):
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for name, ir, expected, call_kept, model in fixtures(SEED):
        directory = out / name
        directory.mkdir(exist_ok=True)
        source = directory / 'input.ll'
        source.write_text(ir)
        paths = []
        for label, passes in [('input', 'verify'), ('normalized', 'function(path-witness-mask-width)'),
                              ('ours', 'function(path-witness-mask-width),default<O3>'),
                              ('stock', 'default<O3>')]:
            path = directory / (label + '-result.ll')
            notes = optimize(llvm, plugin, source, path, passes).stderr
            if label == 'normalized' and notes.count('PW_MASK_NORMALIZED') != expected:
                raise RuntimeError(f'{name}: unexpected rewrite count')
            paths.append(path)
        kept = 'call void @side_effect(' in paths[2].read_text()
        if call_kept is not None and kept != call_kept:
            raise RuntimeError(f'{name}: wrong branch retained/removed')
        optimize(llvm, plugin, paths[1], directory / 'repeat.ll', 'function(path-witness-mask-width)')
        if canonical(paths[1].read_text()) != canonical((directory / 'repeat.ll').read_text()):
            raise RuntimeError(f'{name}: pass was not idempotent')
        count = execute(llvm, directory, paths, model, name == 'unrelated-source') if model else 0
        results.append(dict(name=name, rewrites=expected, executions=count, passed=True))
    # These positive shape tests change input width, extension width and masks.
    # They are verifier/selector tests, not native execution or extra real cases.
    for narrow, wide, mask, query in [(8, 16, 18, 66), (8, 64, 10, 130), (16, 64, 18, 514), (32, 64, 65538, 131074)]:
        source = out / f'width-{narrow}-{wide}.ll'
        ir = re.sub(r'\bi(16|32)\b', lambda m: f'i{narrow if m[1] == "16" else wide}', SEED)
        ir = ir.replace(', 18', f', {mask}').replace(', 514', f', {query}').replace(', 1024', ', 4')
        source.write_text(ir)
        note = optimize(llvm, plugin, source, source.with_suffix('.out.ll'), 'function(path-witness-mask-width)').stderr
        if note.count('PW_MASK_NORMALIZED') != 1:
            raise RuntimeError(f'Width generalization failed: {narrow}->{wide}')
        results.append(dict(name=f'width-{narrow}-{wide}', rewrites=1, executions=0, passed=True))
    results.extend(shared_checks(llvm, plugin, out))
    (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(f'  Scope: {len(results)} controls passed; {sum(r["executions"] for r in results):,} native executions', flush=True)
    return results


def shared_checks(llvm, plugin, out):
    seed = (ROOT / 'tests/shared.ll').read_text()
    cases = [('shared-consumers', seed, 2, '(x & 18) == 0', '(x & 514) == 512'),
             ('shared-samesign', seed.replace('514', '32768').replace('512', '32768')
              .replace('icmp eq', 'icmp samesign eq'), 2, '(x & 18) == 0', '(x & 32768) == 32768'),
             ('shared-high-mask', seed.replace(', 18', ', 2147483666'), 2, '(x & 18) == 0', '(x & 514) == 512'),
             ('shared-ne', seed.replace('icmp eq', 'icmp ne'), 2, '(x & 18) != 0', '(x & 514) != 512'),
             ('shared-commuted', seed.replace('and i32 %wide, 18', 'and i32 18, %wide')
              .replace('icmp eq i32 %a, 0', 'icmp eq i32 0, %a'), 2, '(x & 18) == 0', '(x & 514) == 512')]
    cases += [('shared-i8', seed.replace('i16', 'i8').replace('514', '130').replace('512', '128'),
               2, '(x & 18) == 0', '(x & 130) == 128'),
              ('shared-i64', seed.replace('to i32\n  %a', 'to i64\n  %a')
               .replace('and i32', 'and i64').replace('icmp eq i32', 'icmp eq i64'),
               2, '(x & 18) == 0', '(x & 514) == 512')]
    for name, old, new in [('nneg', 'zext i16', 'zext nneg i16'),
                           ('sext', 'zext i16', 'sext i16'),
                           ('undef', 'zext i16 %flags', 'zext i16 undef'),
                           ('poison', 'zext i16 %flags', 'zext i16 poison'),
                           ('out-of-range', '%q = icmp eq i32 %b, 512', '%q = icmp eq i32 %b, 65536'),
                           ('wide-consumer', '%result = or i32 %pi, %shift', '%result = add i32 %wide, %shift'),
                           ('shared-and', '%result = or i32 %pi, %shift', '%result = add i32 %a, %shift')]:
        cases.append(('shared-reject-' + name, seed.replace(old, new), 0, None, None))
    cases.append(('shared-freeze', seed.replace('%wide = zext i16 %flags',
                  '%frozen = freeze i16 %flags\n  %wide = zext i16 %frozen'),
                  2, '(x & 18) == 0', '(x & 514) == 512'))
    rows = []
    for name, text, wanted, p, q in cases:
        directory = out / name
        directory.mkdir(exist_ok=True)
        source = directory / 'input.ll'
        source.write_text(text)
        paths = []
        for arm, passes in [('stock', 'function(instcombine,simplifycfg)'),
                            ('normalized', 'function(path-witness-mask-width)'),
                            ('ours', 'function(path-witness-mask-width,instcombine,simplifycfg)')]:
            path = directory / (arm + '.ll')
            notes = optimize(llvm, plugin, source, path, passes).stderr
            if arm == 'normalized':
                if notes.count('reason=all-users') != wanted:
                    raise RuntimeError(f'{name}: wrong group rewrite count')
                if wanted and 'zext i16 ' in path.read_text():
                    raise RuntimeError(f'{name}: shared extension is still live')
                again = directory / 'repeat.ll'
                optimize(llvm, plugin, path, again, 'function(path-witness-mask-width)')
                if canonical(path.read_text()) != canonical(again.read_text()):
                    raise RuntimeError(f'{name}: not idempotent')
            paths.append(path)
        executions = 0
        if p:
            limit = 256 if '@f(i8 ' in text else 65536
            ctype = 'unsigned char' if limit == 256 else 'unsigned short'
            harness = directory / 'check.c'
            harness.write_text(f'#include <stdio.h>\nextern unsigned f({ctype});\n'
                              f'int main(void) {{ for(unsigned x=0;x<{limit};++x) {{\n'
                              f'unsigned want=({p}) | (({q}) << 1);\n'
                              'if(f(x)!=want) { printf("FAIL %u\\n",x); return 1; } } return 0; }\n')
            for path in paths:
                exe = path.with_suffix('.check')
                run([llvm / 'clang', '-O0', path, harness, '-o', exe])
                run([exe])
                executions += limit
        rows.append(dict(name=name, rewrites=wanted, executions=executions, passed=True))
    return rows
