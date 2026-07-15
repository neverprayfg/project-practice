Source: testlib commit 1e4e8a24c79c.

- For a pure testlib generator, include `testlib.h` and call
  `registerGen(argc, argv, 1)` exactly once.
- If the agent selects jngen after browsing its documentation, include `jngen.h`
  instead, call jngen's `registerGen(argc, argv)` and `parseArgs(argc, argv)`, and
  read `seed` / `subtask` with jngen's documented option API.
- The runner supplies `-seed` and `-subtask`. The subtask id selects the confirmed
  constraint set; do not require any other command-line option.
- Use `rnd.next`, `rnd.wnext`, `rnd.partition`, and testlib `println` helpers.
- Write one complete test to standard output. Do not open arbitrary files.
- The backend supplies the subtask and seed arguments and captures standard output.
- The pure testlib and jngen entry points are mutually exclusive; never call both.
