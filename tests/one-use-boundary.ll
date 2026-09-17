; Demonstrates the InstCombine one-use boundary behind the mixed-width guard.
declare void @consume(i32)

; The AND itself reads %wide once, even without a following comparison.
; The return type stays i32; LLVM can move the extension after a narrow AND.
define i32 @mask_only(i16 %flags) {
  %wide = zext i16 %flags to i32
  %masked = and i32 %wide, 18
  ret i32 %masked
}

define i1 @one_use(i16 %flags) {
  %wide = zext i16 %flags to i32
  %masked = and i32 %wide, 18
  %check = icmp eq i32 %masked, 0
  ret i1 %check
}

define i1 @two_uses(i16 %flags) {
  %wide = zext i16 %flags to i32
  %masked = and i32 %wide, 18
  %check = icmp eq i32 %masked, 0
  call void @consume(i32 %wide)
  ret i1 %check
}
