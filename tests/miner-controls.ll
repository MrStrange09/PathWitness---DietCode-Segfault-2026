declare void @side_effect()
declare void @consume(i32)

; REACHABLE: guard clears bits 1,4 ; query needs bit 9 only. No conflict.
define void @reachable_no_conflict(i16 noundef %flags) {
entry:
  %wide = zext i16 %flags to i32
  %m = and i32 %wide, 18
  %outer = icmp eq i32 %m, 0
  br i1 %outer, label %check, label %other
check:
  %n = and i16 %flags, 512
  %inner = icmp eq i16 %n, 512
  br i1 %inner, label %live, label %exit
live:
  call void @side_effect()
  br label %exit
other:
  %use = and i32 %wide, 1024
  call void @consume(i32 %use)
  br label %exit
exit:
  ret void
}

; REACHABLE: query reads a DIFFERENT value. Nothing is implied.
define void @reachable_unrelated(i16 noundef %flags, i16 noundef %second) {
entry:
  %wide = zext i16 %flags to i32
  %m = and i32 %wide, 18
  %outer = icmp eq i32 %m, 0
  br i1 %outer, label %check, label %exit
check:
  %n = and i16 %second, 514
  %inner = icmp eq i16 %n, 514
  br i1 %inner, label %live, label %exit
live:
  call void @side_effect()
  br label %exit
exit:
  ret void
}

; REACHABLE: the guard edge does not dominate the query (two predecessors).
define void @reachable_not_dominated(i16 noundef %flags, i1 %other_path) {
entry:
  %wide = zext i16 %flags to i32
  %m = and i32 %wide, 18
  %outer = icmp eq i32 %m, 0
  br i1 %outer, label %check, label %maybe
maybe:
  br i1 %other_path, label %check, label %exit
check:
  %n = and i16 %flags, 514
  %inner = icmp eq i16 %n, 514
  br i1 %inner, label %live, label %exit
live:
  call void @side_effect()
  br label %exit
exit:
  ret void
}
