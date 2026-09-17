/* Small API/workload checks, not SQLite's upstream test suite or a benchmark. */
#include "sqlite3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned blob_calls, queries;
static char rows[4096];

/* SQL function arguments are protected sqlite3_value objects. Calling the
 * actual public API here exercises the function changed by our pass. */
static void probe_blob(sqlite3_context *ctx, int argc, sqlite3_value **argv) {
  (void)argc;
  ++blob_calls;
  if (sqlite3_value_type(argv[0]) == SQLITE_NULL) {
    (void)sqlite3_value_blob(argv[0]);
    sqlite3_result_null(ctx);
    return;
  }
  const void *data = sqlite3_value_blob(argv[0]);
  int size = sqlite3_value_bytes(argv[0]);
  if (size == 0)
    sqlite3_result_zeroblob(ctx, 0);
  else if (!data)
    sqlite3_result_error_nomem(ctx);
  else
    sqlite3_result_blob(ctx, data, size, SQLITE_TRANSIENT);
}

static int collect(void *unused, int count, char **values, char **names) {
  (void)unused;
  (void)names;
  for (int i = 0; i < count; ++i) {
    const char *value = values[i] ? values[i] : "<NULL>";
    if (strlen(rows) + strlen(value) + 2 >= sizeof(rows)) return 1;
    strcat(rows, value);
    strcat(rows, i + 1 == count ? "\n" : "|");
  }
  return 0;
}

static void check(sqlite3 *db, const char *sql, const char *expected) {
  char *error = NULL;
  rows[0] = 0;
  int rc = sqlite3_exec(db, sql, collect, NULL, &error);
  if (rc != SQLITE_OK || strcmp(rows, expected)) {
    fprintf(stderr, "SQL: %s\nError: %s\nExpected: %sGot: %s\n", sql,
            error ? error : "none", expected, rows);
    sqlite3_free(error);
    exit(1);
  }
  printf("check %u: %s", ++queries, *rows ? rows : "OK\n");
}

int main(void) {
  sqlite3 *db = NULL;
  if (sqlite3_open(":memory:", &db) != SQLITE_OK) return 1;
  if (sqlite3_create_function(db, "probe_blob", 1, SQLITE_UTF8, NULL,
                             probe_blob, NULL, NULL) != SQLITE_OK) return 1;
  check(db, "SELECT typeof(probe_blob(NULL)),length(probe_blob(NULL))", "null|<NULL>\n");
  check(db, "SELECT typeof(probe_blob('')),hex(probe_blob('')),length(probe_blob(''))", "blob||0\n");
  check(db, "SELECT typeof(probe_blob(x'')),hex(probe_blob(x'')),length(probe_blob(x''))", "blob||0\n");
  check(db, "SELECT hex(probe_blob('hello'))", "68656C6C6F\n");
  check(db, "SELECT hex(probe_blob(x'00FF1080')),length(probe_blob(x'00FF1080'))", "00FF1080|4\n");
  check(db, "SELECT hex(probe_blob(CAST(x'610062' AS TEXT))),length(probe_blob(CAST(x'610062' AS TEXT)))", "610062|3\n");
  check(db, "SELECT hex(probe_blob(0)),hex(probe_blob(-7)),hex(probe_blob(9223372036854775807))", "30|2D37|39323233333732303336383534373735383037\n");
  check(db, "SELECT hex(probe_blob(1.5))", "312E35\n");
  check(db, "SELECT hex(probe_blob(zeroblob(4))),length(probe_blob(zeroblob(4)))", "00000000|4\n");
  check(db, "SELECT hex(probe_blob(char(955,128578)))", "CEBBF09F9982\n");
  check(db, "CREATE TABLE t(v); WITH RECURSIVE n(v) AS (VALUES(0) UNION ALL SELECT v+1 FROM n WHERE v<1023) INSERT INTO t SELECT v FROM n", "");
  check(db, "SELECT count(*),sum(length(probe_blob(v))) FROM t", "1024|2986\n");
  check(db, "BEGIN; UPDATE t SET v=v+1; ROLLBACK; SELECT sum(v) FROM t", "523776\n");
  check(db, "CREATE INDEX t_v ON t(v); SELECT sum(v) FROM t WHERE v<100", "4950\n");
  check(db, "PRAGMA integrity_check", "ok\n");
  if (sqlite3_close(db) != SQLITE_OK) return 1;
  printf("PASS: %u checked SQL groups; %u sqlite3_value_blob calls\n", queries, blob_calls);
  return 0;
}

