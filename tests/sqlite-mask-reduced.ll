target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target triple = "x86_64-unknown-linux-gnu"

define ptr @sqlite3_value_blob(ptr %0) {
  %2 = load i16, ptr %0, align 4
  %3 = zext i16 %2 to i32
  %4 = and i32 %3, 18
  %5 = icmp eq i32 %4, 0
  br i1 %5, label %10, label %6

6:                                                ; preds = %1
  %7 = and i32 %3, 1
  %8 = icmp eq i32 %7, 0
  br i1 %8, label %9, label %common.ret1

common.ret1:                                      ; preds = %10, %9, %6
  %common.ret1.op = phi ptr [ null, %6 ], [ null, %10 ], [ null, %9 ]
  ret ptr %common.ret1.op

9:                                                ; preds = %6
  store i16 0, ptr %0, align 4
  br label %common.ret1

10:                                               ; preds = %1
  %11 = and i16 %2, 514
  %12 = icmp eq i16 %11, 514
  br i1 %12, label %13, label %common.ret1

13:                                               ; preds = %10
  %14 = load ptr, ptr %0, align 8
  ret ptr %14
}
