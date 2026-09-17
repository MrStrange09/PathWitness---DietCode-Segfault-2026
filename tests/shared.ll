; Shared extension: narrowing both consumers makes the extension dead.
; A synthetic mechanism control, not an independent benchmark occurrence.
define i32 @f(i16 %flags) {
entry:
  %wide = zext i16 %flags to i32
  %a = and i32 %wide, 18
  %b = and i32 %wide, 514
  %p = icmp eq i32 %a, 0
  %q = icmp eq i32 %b, 512
  %pi = zext i1 %p to i32
  %qi = zext i1 %q to i32
  %shift = shl i32 %qi, 1
  %result = or i32 %pi, %shift
  ret i32 %result
}
