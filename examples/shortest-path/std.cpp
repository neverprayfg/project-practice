#include <iostream>
#include <vector>
#include <queue>
#include <algorithm>

using namespace std;

// 定义无穷大常量，防止加法溢出
const long long INF = 1e18;

struct Edge {
    int to;
    long long cost;
};

struct State {
    long long dist;
    int u;
    int used; // 0 表示未使用优惠券，1 表示已使用

    // 优先队列默认是大顶堆，我们需要小顶堆，因而重载大于号
    bool operator>(const State& other) const {
        return dist > other.dist;
    }
};

int main() {
    // 优化标准输入输出流的性能
    ios_base::sync_with_stdio(false);
    cin.tie(NULL);

    int N, M;
    if (!(cin >> N >> M)) return 0;

    vector<vector<Edge>> adj(N + 1);
    for (int i = 0; i < M; ++i) {
        int u, v;
        long long c;
        cin >> u >> v >> c;
        adj[u].push_back({v, c});
    }

    // dist[i][0] 表示到 i 未用券的最短路
    // dist[i][1] 表示到 i 已用券的最短路
    vector<vector<long long>> dist(N + 1, vector<long long>(2, INF));
    priority_queue<State, vector<State>, greater<State>> pq;

    // 初始化起点
    dist[1][0] = 0;
    pq.push({0, 1, 0});

    while (!pq.empty()) {
        State curr = pq.top();
        pq.pop();

        int u = curr.u;
        int used = curr.used;
        long long d = curr.dist;

        // 如果当前取出的距离大于记录的最小值，说明已被更新过，直接跳过
        if (d > dist[u][used]) {
            continue;
        }

        for (const auto& edge : adj[u]) {
            int v = edge.to;
            long long w = edge.cost;

            // 情况1：不使用优惠券
            if (dist[u][used] + w < dist[v][used]) {
                dist[v][used] = dist[u][used] + w;
                pq.push({dist[v][used], v, used});
            }

            // 情况2：使用优惠券 (仅当之前没有使用过时)
            if (used == 0) {
                long long discounted_w = w / 2;
                if (dist[u][0] + discounted_w < dist[v][1]) {
                    dist[v][1] = dist[u][0] + discounted_w;
                    pq.push({dist[v][1], v, 1});
                }
            }
        }
    }

    // 输出每个节点的最短距离
    for (int i = 1; i <= N; ++i) {
        long long ans = min(dist[i][0], dist[i][1]);
        if (ans == INF) {
            cout << -1 << "\n";
        } else {
            cout << ans << "\n";
        }
    }

    return 0;
}