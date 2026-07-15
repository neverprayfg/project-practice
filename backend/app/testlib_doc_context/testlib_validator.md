Source: testlib commit 1e4e8a24c79c.

- Include `testlib.h` and call `registerValidation(argc, argv)` exactly once.
- Read from `inf` in the confirmed field order. Name every scalar read.
- Use bounded reads such as `inf.readInt(min, max, "n")`, `readLong`,
  `readInts`, or `readLongs`; enforce structural relations explicitly.
- Check separators with `readSpace` where required, terminate lines with
  `readEoln`, and finish with `readEof`.
- Reject invalid relations with testlib assertions or failure helpers. Do not
  replace testlib parsing with regexes, streams, or ad hoc token splitting.

