# AtCoder ABC466 G - Segment Sum Constraints

- Contest: [ABC466](https://atcoder.jp/contests/abc466)
- Task: [abc466_g](https://atcoder.jp/contests/abc466/tasks/abc466_g)
- Score: 600 points

## Problem Statement

You are given $M$ triples of integers $(L_i, R_i, S_i)$.

Consider tuples $A = (A_1, A_2, \ldots, A_N)$ of $N$ **positive** integers satisfying all of the following conditions.

- The sum of $A_{L_i}, A_{L_i+1}, \ldots, A_{R_i}$ is $S_i$.

If there are infinitely many tuples satisfying the conditions, output `Infinity`; otherwise, output the number of such tuples, modulo $998244353$.

## Constraints

- $1 \leq N \leq 8$
- $1 \leq M \leq 36$
- $1 \leq L_i \leq R_i \leq N$
- $1 \leq S_i \leq 10^9$
- All $(L_i, R_i)$ are distinct.
- All input values are integers.

## Input

The input is given from Standard Input in the following format:

```
N M
L_1 R_1 S_1
L_2 R_2 S_2
\vdots
L_M R_M S_M
```

## Output

If there are infinitely many tuples satisfying the conditions, output `Infinity`; otherwise, output the number of such tuples, modulo $998244353$.

## Sample Input 1

```
3 2
1 2 7
2 3 10
```

## Sample Output 1

```
6
```

We have the following two conditions.

- $A_1 + A_2 = 7$
- $A_2 + A_3 = 10$

The six tuples $A = (1,6,4), (2,5,5), (3,4,6), (4,3,7), (5,2,8), (6,1,9)$ satisfy the conditions, so output $6$ modulo $998244353$, that is, output $6$.

## Sample Input 2

```
2 1
1 1 10
```

## Sample Output 2

```
Infinity
```

There are infinitely many tuples $A$ satisfying the conditions.

## Sample Input 3

```
2 2
1 1 10
1 2 1
```

## Sample Output 3

```
0
```

There is no tuple $A$ satisfying the conditions.