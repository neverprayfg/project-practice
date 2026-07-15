# 结构标签目录 v1

| 分类 | 已支持标签 | 人工处理标签 |
| --- | --- | --- |
| 基本类型 | `primitive.integer` `primitive.real` `primitive.character` `primitive.string` | - |
| 集合 | `collection.array` `collection.permutation` `collection.set` `collection.query_sequence` | `collection.matrix` |
| 图 | `graph` `graph.directed` `graph.undirected` `graph.weighted` `graph.directed.weighted` `graph.simple` `graph.multigraph` `graph.connected` `graph.disconnected` `graph.dag` `graph.bipartite` | - |
| 树 | `tree` `tree.rooted` | `tree.forest` |
| 几何 | `geometry.point_set` `geometry.polygon` | `geometry.segment_set` `geometry.grid` |
| 数值 | `number.range` `number.prime_related` | `number.modulo` `number.coordinate_compressed` |
| 关系 | `relationship.interval` `relationship.parent_child` `relationship.edge_list` | `relationship.adjacency_matrix` |

`supported` 只表示本地 jngen 文档能够证明所需构造能力。`manual_only` 标签可以被识别，但必须在阶段三向用户暴露 `needs_tag_review`，不能自动映射成无关 API。
