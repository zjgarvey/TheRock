// Copyright Advanced Micro Devices, Inc.
// SPDX-License-Identifier: MIT

#define _GNU_SOURCE
#include <fcntl.h>
#include <limits.h>
#include <link.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

unsigned int la_version(unsigned int version) { return LAV_CURRENT; }

// Every child inherits this audit module. Check natural loader resolution;
// forcing an HSA preload would conceal SONAME or dependency errors.
unsigned int la_objopen(struct link_map *map, Lmid_t namespace_id,
                        uintptr_t *cookie) {
  if (!strstr(map->l_name, "libhsa-runtime64.so"))
    return 0;
  const char *expected = getenv("RUNTIME_TRANSITION_HSA");
  const char *log = getenv("RUNTIME_TRANSITION_AUDIT_LOG");
  char resolved[PATH_MAX];
  if (!expected || !log || !realpath(map->l_name, resolved) ||
      strcmp(expected, resolved))
    _exit(125);
  int descriptor = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
  if (descriptor < 0)
    _exit(125);
  char record[PATH_MAX + 64];
  int length = snprintf(record, sizeof(record), "%ld %s\n", (long)getpid(), resolved);
  if (length < 0 || length >= sizeof(record) ||
      write(descriptor, record, length) != length)
    _exit(125);
  close(descriptor);
  return 0;
}
