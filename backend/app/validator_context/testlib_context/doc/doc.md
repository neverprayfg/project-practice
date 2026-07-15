# Testlib

## Intro

This project contains a C++ implementation of testlib. It is already being used in many programming contests in Russia, such as the Russian National Olympiad in Informatics and different stages of ICPC. Join!

The library's C++ code is tested for compatibility with standard C++11 and higher on different versions of `g++`, `clang++`, and Microsoft Visual C++.

This code has been used many times in Codeforces contests.

## Samples

### Validator

This code reads input from the standard input and checks that it contains only one integer between 1 and 100, inclusive. It also validates that the file ends with EOLN and EOF. On Windows, it expects #13#10 as EOLN, and it expects #10 as EOLN on other platforms. It does not ignore white-spaces, so it works very strictly. It will return a non-zero code in the case of illegal input and write a message to the standard output. See more examples in the package.

```c++
#include "testlib.h"

int main(int argc, char* argv[]) {
    registerValidation(argc, argv);
    inf.readInt(1, 100, "n");
    inf.readEoln();
    inf.readEof();
}
```