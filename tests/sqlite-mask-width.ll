; Hand-distilled from the mask/cast structure in sqlite3_value_blob.
; This is a small IR reproducer, not a mechanical reduction of the whole function.
; A straightforward equivalent C example already optimizes away on this compiler.
; %flags is a defined 16-bit value. Another use keeps its 32-bit extension live.
declare void @side_effect()
declare void @consume(i32)

define void @f(i16 noundef %flags) {
entry:
  %wide = zext i16 %flags to i32
  %m = and i32 %wide, 18
  %outer = icmp eq i32 %m, 0
  br i1 %outer, label %check, label %other
check:
  %n = and i16 %flags, 514
  %inner = icmp eq i16 %n, 514
  br i1 %inner, label %dead, label %exit
dead:
  call void @side_effect()
  br label %exit
other:
  %use = and i32 %wide, 1024
  call void @consume(i32 %use)
  br label %exit
exit:
  ret void
}
