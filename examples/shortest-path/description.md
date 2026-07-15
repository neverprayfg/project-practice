## Problem Statement

You are given a directed graph with $N$ vertices numbered $1$ to $N$ and $M$ directed edges.  
The $i$-th edge goes from vertex $U_i$ to $V_i$ with a weight of $C_i$.

You have **one** discount coupon. You can choose to use this coupon on at most one edge during your travel.  
If you use the coupon on the $i$-th edge, the weight of that edge becomes $\lfloor C_i / 2 \rfloor$ (the value rounded down to the nearest integer).

For each vertex $v \in \{1, 2, \ldots, N\}$, find the minimum cost to travel from vertex 1 to vertex $v$. If vertex $v$ is unreachable from vertex 1, output `-1`.

## Constraints

- $2 \leq N \leq 10^5$
- $1 \leq M \leq 2 \times 10^5$
- $1 \leq U_i, V_i \leq N$
- $U_i \neq V_i$
- $1 \leq C_i \leq 10^9$
- All input values are integers.

## Input

The input is given from Standard Input in the following format:

```
N M
U_1 V_1 C_1
U_2 V_2 C_2
\vdots
U_M V_M C_M
```

## Output

Print $N$ lines. The $i$-th line should contain the minimum cost to travel from vertex 1 to vertex $i$ using at most one coupon. If vertex $i$ is unreachable from vertex 1, print `-1`.

## Sample Input 1

```
3 3
1 2 8
2 3 6
1 3 10
```

## Sample Output 1

```
0
4
5
```

- For vertex 1: The starting cost is $0$.
- For vertex 2: We can travel $1 \to 2$. Using the coupon on this edge reduces the cost from $8$ to $\lfloor 8/2 \rfloor = 4$.
- For vertex 3: We can travel directly $1 \to 3$. Using the coupon on this edge reduces the cost from $10$ to $\lfloor 10/2 \rfloor = 5$. (If we travelled via $1 \to 2 \to 3$, the cost would be $8 + \lfloor 6/2 \rfloor = 11$ or $\lfloor 8/2 \rfloor + 6 = 10$, which are both larger than $5$).

## Sample Input 2

```
3 1
1 2 10
```

## Sample Output 2

```
0
5
-1
```

Vertex 3 cannot be reached from vertex 1, so we output `-1` for the third line.