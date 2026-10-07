"""通联日志配对核心算法。

配对规则：
  1. 两条日志必须相互指认（A 本台/对方 == B 对方/本台）；
  2. 波段、模式相同；
  3. 两条带时区时间的绝对时差不超过演练允许时差；
  4. 每条记录最多参与一次配对。

优化目标（字典序优先级）：
  1. 最大化配对数量；
  2. 在配对数最大的前提下最小化总时差；
  3. 仍并列时取字典序最小的配对列表：每对的两个记录号先内部升序，
     再把所有配对整体升序后按列表字典序比较。

锁定（复核员人工互证）：
  ``locked_keys`` 中的记录对强制入选结果，即使因此拉低全场配对数也不
  替换；锁定对占用的记录从自由匹配池中剔除，剩余记录再按上述三级
  目标求解。调用方须保证锁定对合法（现存、相互指认、波段/模式一致、
  时差不超限），否则抛 :class:`InvalidLockError`。

实现方式：
  相互指认关系天然构成二分图——按 ``(min(本台,对方), max(本台,对方),
  波段, 模式)`` 分边，本台呼号较小的记录在左部 L，另一条在右部 R，
  所有候选边只可能跨部存在，因此是二分图。在此图上做最小费用最大流
  （min-cost max-flow, Dijkstra + potential）：
    * 每条流对应一对配对，最大流 => 配对数最大；
    * 费用 = 时差（微秒）* SCALE + OFFSET - 2**(E-1-rank)，
      其中边按配对键 (rec_a, rec_b) 升序赋予 rank 0..E-1，SCALE = 2**E。
      同一配对数下，时差之和的差至少为 SCALE，严格压倒字典序奖励
      （奖励之差的绝对值 <= 2**E - 1），因此最小费用依次满足
      “时差和最小”、“配对列表字典序最小”。OFFSET = 2**(E-1)
      保证所有边费用非负，初始 potential 为 0 即可用 Dijkstra。

复核唯一性核查（:func:`review_pair_uniqueness`）：
  在“保持全部锁定对、排除隔离记录”的同一组日志上，枚举满足
  “配对数全场最大、总时差在该配对数下最小”的**全部**合法完整方案
  （不施加第三级字典序决胜——字典序只是当前接口的选解规则，并非
  合法性条件），逐条核查当前结果中的非锁定配对是否出现在所有这类
  方案中。判定利用最小费用流最优解的经典结构：以流结束后的可行位势
  取残量网络中的**零费用弧（紧弧）**，一条入选边可被等优方案替换，
  当且仅当它的两端在紧弧残量子图中处于同一强连通分量；沿该分量中的
  有向环增广即得到同配对数、同总时差的替代完整方案。不含指定对的
  字典序最小替代方案则直接在删除该边后的图上重跑三级费用流得到。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import heapq
from typing import Sequence

# 时差与微秒互相换算。
MICROS_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class Record:
    """一条台站通联日志（算法所需字段）。"""

    record_no: str
    station: str       # 本台呼号（已规范化）
    counterpart: str   # 对方呼号（已规范化）
    ts_micros: int     # 带时区时间归一到 UTC 的微秒时间戳
    band: str          # 波段（已规范化）
    mode: str          # 模式（已规范化）


@dataclass(frozen=True)
class Pair:
    """一对互证成功的日志。"""

    record_a: str
    record_b: str
    time_diff_seconds: float

    def as_tuple(self) -> tuple[str, str]:
        return (self.record_a, self.record_b)


@dataclass(frozen=True)
class MatchResult:
    pairs: tuple[Pair, ...]
    unpaired: tuple[str, ...]
    total_time_diff_seconds: float


class InvalidLockError(ValueError):
    """锁定对在当前日志集合上不再合法（缺失/不互证/波段模式/时差超限）。"""


def _pair_key(r1: Record, r2: Record) -> tuple[str, str]:
    a, b = r1.record_no, r2.record_no
    return (a, b) if a <= b else (b, a)


def pair_eligibility(
    record_map: dict[str, Record], allowed_micros: int, a: str, b: str
) -> tuple[str, str] | None:
    """检查 ``(a, b)`` 是否为可锁定的互证候选。

    合法返回 ``None``；否则返回 ``(原因码, 中文说明)``，原因码取值：
    ``same_record`` / ``record_missing`` / ``not_mutual`` /
    ``band_mode`` / ``time_diff``。
    """
    if a == b:
        return "same_record", f"两条记录号不能相同: {a}"
    ra = record_map.get(a)
    rb = record_map.get(b)
    if ra is None or rb is None:
        missing = a if ra is None else b
        return "record_missing", f"记录不存在: {missing}"
    if ra.station != rb.counterpart or ra.counterpart != rb.station:
        return "not_mutual", f"记录 {a} 与 {b} 未相互指认，不能锁定"
    if ra.band != rb.band or ra.mode != rb.mode:
        return "band_mode", f"记录 {a} 与 {b} 波段或模式不一致，不能锁定"
    diff_micros = abs(ra.ts_micros - rb.ts_micros)
    if diff_micros > allowed_micros:
        return (
            "time_diff",
            f"记录 {a} 与 {b} 时差 {diff_micros / MICROS_PER_SECOND:g} 秒"
            f"超过允许值 {allowed_micros / MICROS_PER_SECOND:g} 秒，不能锁定",
        )
    return None


def _build_candidate_edges(
    records: Sequence[Record], allowed_micros: int
) -> list[tuple[int, int, int, tuple[str, str]]]:
    """构造候选边列表 ``(左记录下标, 右记录下标, 时差微秒, 配对键)``。

    用右部索引加速相互指认查找；调用方负责传入已剔除锁定/隔离成员的
    记录序列。
    """
    # 右部索引：(本台, 对方, band, mode) -> [记录...]，
    # 左部记录 r 以 (counterpart, station, band, mode) 反查即可找到
    # 所有“本台==r.对方、对方==r.本台”的候选记录。
    right_index: dict[tuple[str, str, str, str], list[int]] = {}
    left_flags: list[bool] = []
    for i, r in enumerate(records):
        is_left = (r.station, r.counterpart) < (r.counterpart, r.station)
        left_flags.append(is_left)
        if not is_left:
            key = (r.station, r.counterpart, r.band, r.mode)
            right_index.setdefault(key, []).append(i)

    edges: list[tuple[int, int, int, tuple[str, str]]] = []
    for i, r in enumerate(records):
        if not left_flags[i]:
            continue
        for j in right_index.get(
            (r.counterpart, r.station, r.band, r.mode), []
        ):
            diff_micros = abs(records[j].ts_micros - r.ts_micros)
            if diff_micros <= allowed_micros:
                edges.append((i, j, diff_micros, _pair_key(r, records[j])))

    # 边按配对键升序定等级：等级越小（记录号组合越小）字典序优先级越高。
    edges.sort(key=lambda e: e[3])
    return edges


@dataclass(frozen=True)
class _FlowNetwork:
    """自由匹配子图上的费用流网络与其解。

    ``graph`` 为前向星残余网络，每条残余边为
    ``[to, cap, cost, rev]``（就地修改容量）；其余字段为节点映射与
    候选边信息，供唯一性核查复用同一张图。
    """

    graph: list[list[list[int]]]
    edges: tuple[tuple[int, int, int, tuple[str, str]], ...]
    records: tuple[Record, ...]
    source: int
    sink: int
    lidx: dict[int, int]           # 左记录下标 -> 网络节点
    ridx: dict[int, int]           # 右记录下标 -> 网络节点
    match_arc: tuple[tuple[int, int], ...]  # 与 edges 对齐：(左节点, L->R 边下标)
    flow: int
    total_micros: int


def _solve_flow_network(
    records: Sequence[Record],
    edges: Sequence[tuple[int, int, int, tuple[str, str]]],
) -> _FlowNetwork:
    """在给定候选边上建最小费用最大流网络并求流（三级目标一次完成）。"""
    e_count = len(edges)
    scale = 1 << e_count          # 2**E，时差主项的放大倍数
    # 入选 rank 边的字典序奖励为 2**(E-1-rank)：rank 0 为最高位，
    # 最大化奖励之和等价于最大化“从 rank 0 起的含边掩码”的字典序。
    bonus = [1 << (e_count - 1 - rank) for rank in range(e_count)]
    # 费用 = 时差*SCALE + 偏移 - 奖励；最小化费用即最大化奖励。
    # 最大奖励为 2**(E-1)，减去奖励后该项恒 >= 0，故费用恒非负，
    # 初始 potential 全为 0 即可直接使用 Dijkstra。
    offset = 1 << (e_count - 1) if e_count else 0

    left_nodes = sorted({i for (i, _j, _w, _k) in edges})
    right_nodes = sorted({j for (_i, j, _w, _k) in edges})
    lidx = {ri: k + 1 for k, ri in enumerate(left_nodes)}
    ridx = {rj: k + 1 + len(left_nodes) for k, rj in enumerate(right_nodes)}
    source = 0
    sink = 1 + len(left_nodes) + len(right_nodes)
    n_nodes = sink + 1

    # 前向星：每条残余边 [to, cap, cost, rev]
    graph: list[list[list[int]]] = [[] for _ in range(n_nodes)]

    def add_edge(u: int, v: int, cap: int, cost: int) -> int:
        # 返回“u 的前向边”下标。注意：该下标只在之后不再依赖
        # 其他向 u 追加边产生的位置时才稳定——因此每条 L->R 边
        # 追加后立刻记录其下标（向同一 u 继续追加不会改变旧下标）。
        fwd = len(graph[u])
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, fwd])
        return fwd

    match_arc: list[tuple[int, int]] = []  # 与 edges 对齐：(左部节点, L->R 边下标)
    # 每个左/右节点只连一条容量 1 的源/汇边，保证每条记录最多用一次。
    for u in range(1, 1 + len(left_nodes)):
        add_edge(source, u, 1, 0)
    for k in range(len(right_nodes)):
        v = 1 + len(left_nodes) + k
        add_edge(v, sink, 1, 0)
    for rank, (i, j, weight, _key) in enumerate(edges):
        cost = weight * scale + offset - bonus[rank]
        u = lidx[i]
        v = ridx[j]
        fwd = add_edge(u, v, 1, cost)
        match_arc.append((u, fwd))

    # --- 最小费用最大流（Dijkstra + potential） ---
    potential = [0] * n_nodes
    flow = 0
    while True:
        inf = None  # Python 大整数无溢出风险，用 None 表示不可达
        dist = [inf] * n_nodes
        prev_v = [-1] * n_nodes
        prev_e = [-1] * n_nodes
        dist[source] = 0
        heap: list[tuple[int, int]] = [(0, source)]
        while heap:
            d, u = heapq.heappop(heap)
            if d != dist[u]:
                continue
            for ei, arc in enumerate(graph[u]):
                v, cap, cost, _rev = arc
                if cap <= 0:
                    continue
                nd = d + cost + potential[u] - potential[v]
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    prev_v[v] = u
                    prev_e[v] = ei
                    heapq.heappush(heap, (nd, v))
        if dist[sink] is None:
            break
        for v in range(n_nodes):
            if dist[v] is not None:
                potential[v] += dist[v]
        # 所有容量均为 1，每次增广 1 条流。
        v = sink
        while v != source:
            u = prev_v[v]
            ei = prev_e[v]
            arc = graph[u][ei]
            arc[1] -= 1
            rev = graph[v][arc[3]]
            rev[1] += 1
            v = u
        flow += 1

    total_micros = 0
    for rank, (_i, _j, weight, _key) in enumerate(edges):
        u, fwd = match_arc[rank]
        if graph[u][fwd][1] == 0:
            total_micros += weight

    return _FlowNetwork(
        graph=graph,
        edges=tuple(edges),
        records=tuple(records),
        source=source,
        sink=sink,
        lidx=lidx,
        ridx=ridx,
        match_arc=tuple(match_arc),
        flow=flow,
        total_micros=total_micros,
    )


def _extract_flow_pairs(network: _FlowNetwork) -> list[Pair]:
    """从求流后的网络提取入选配对（按配对键升序）。"""
    pairs: list[Pair] = []
    for rank, (_i, _j, weight, key) in enumerate(network.edges):
        # L->R 的前向边容量被用尽 => 该边入选匹配。
        u, fwd = network.match_arc[rank]
        if network.graph[u][fwd][1] == 0:
            pairs.append(
                Pair(
                    record_a=key[0],
                    record_b=key[1],
                    time_diff_seconds=weight / MICROS_PER_SECOND,
                )
            )
    return pairs


def _match_free(
    records: Sequence[Record],
    allowed_micros: int,
    excluded_keys: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[Pair], set[str], int]:
    """在无锁定记录的集合上求全场最优匹配。

    返回 ``(配对列表, 已配对记录号集合, 总时差微秒)``。``excluded_keys``
    中的候选边（配对键）不参与求解，供唯一性核查在删边图上重算替代方案。
    """
    edges = [
        e for e in _build_candidate_edges(records, allowed_micros)
        if e[3] not in excluded_keys
    ]
    if not edges:
        return [], set(), 0
    network = _solve_flow_network(records, edges)
    pairs = _extract_flow_pairs(network)
    matched: set[str] = set()
    for rank, (i, j, _w, _key) in enumerate(network.edges):
        u, fwd = network.match_arc[rank]
        if network.graph[u][fwd][1] == 0:
            matched.add(records[i].record_no)
            matched.add(records[j].record_no)
    return pairs, matched, network.total_micros


def match_records(
    records: Sequence[Record],
    allowed_diff_seconds: float,
    locked_keys: Sequence[tuple[str, str]] = (),
) -> MatchResult:
    """计算全场最优互证配对。

    ``records`` 内的记录号须唯一（服务层已保证）。``locked_keys`` 中的
    记录对强制入选（服务层已校验合法且互不占用），其记录不参与自由
    匹配；即使锁定使全场配对数下降，锁定对也保留不替换。
    """
    allowed_micros = int(round(allowed_diff_seconds * MICROS_PER_SECOND))

    # --- 先固定全部锁定对，并把其记录移出自由匹配池 ---
    pairs, locked_ids, total_micros, free_records = _split_locked_records(
        records, allowed_micros, locked_keys
    )
    free_pairs, free_matched, free_total = _match_free(
        free_records, allowed_micros
    )
    pairs.extend(free_pairs)
    total_micros += free_total

    pairs.sort(key=lambda p: p.as_tuple())
    matched = locked_ids | free_matched
    unpaired = tuple(
        r.record_no for r in records if r.record_no not in matched
    )
    return MatchResult(
        pairs=tuple(pairs),
        unpaired=unpaired,
        total_time_diff_seconds=total_micros / MICROS_PER_SECOND,
    )


# --------------------------------------------------------------------------
# 配对唯一性核查（复核员核查当前结果中哪些配对并非唯一选择）
#
# 合法完整方案的口径：保持全部锁定对、排除隔离记录后，在同一组日志上
# “配对数全场最大、总时差在该配对数下最小”的全部匹配（第三级字典序
# 只是原结果接口的选解规则，不是方案合法性条件，故不作为筛选条件）。
#
# 结构判定（最小费用流最优解的经典性质）：求流结束后残量网络中存在
# 可行位势 h 使所有残量弧的约化费用非负；约化费用为 0 的残量弧称
# “紧弧”。沿紧弧构成的有向环增广环流，流量（配对数）与费用（总时差）
# 均不变，即得到另一个等优合法完整方案。一条入选边 e=(u,v) 能被等优
# 方案替换，当且仅当：
#   1. 其费用弧本身紧（h[u] + cost == h[v]，反向退流弧才也是紧弧）；
#   2. 紧弧残量子图中 u 可沿残量弧到达 v（与反向弧 v->u 合成零费用环）。
# 等价地，u、v 在紧弧残量子图中处于同一强连通分量且费用弧紧。
# 判定为可替换后，直接在删除该边的图上重跑三级费用流：删边不改变最大
# 配对数时所得即为“不含该对的字典序最小合法完整方案”；若删边会降低
# 配对数或增大总时差（防御性兜底，理论上不应发生），仍按不可替换处理。
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PairAlternative:
    """不含被核查配对的一份完整替代方案及其相对当前结果的增删。"""

    pairs: tuple[Pair, ...]
    unpaired: tuple[str, ...]
    total_time_diff_seconds: float
    added_pairs: tuple[Pair, ...]
    removed_pairs: tuple[Pair, ...]


@dataclass(frozen=True)
class PairReview:
    """当前结果中一对非锁定配对的唯一性核查结论。

    ``universal`` 为真表示该对出现在所有“最大配对数 + 最小总时差”的
    合法完整方案中；否则 ``alternative`` 给出一份不含该对、字典序最小
    的完整替代方案（含相对当前结果的增删）。
    """

    pair: Pair
    universal: bool
    alternative: PairAlternative | None


@dataclass(frozen=True)
class UniquenessReview:
    """整场唯一性核查结论（当前版本的非锁定配对逐条给出）。"""

    pairs: tuple[PairReview, ...]
    locked_pairs: tuple[Pair, ...]
    optimal_pair_count: int
    total_time_diff_seconds: float


def _feasible_potential(
    graph: Sequence[Sequence[Sequence[int]]],
    arc_cost: Sequence[Sequence[int]],
) -> list[int]:
    """在残量网络上求一组可行位势（全部残量弧约化费用 >= 0）。

    最优流不存在负费用残量环；以全 0 初值（等价于增设超级源连所有点）
    跑 Bellman-Ford/SPFA 即可收敛，且紧弧（约化费用为 0 的残量弧）不
    依赖增广过程中的历史位势，判定等优方案时不会出现伪零弧。
    ``arc_cost`` 与 ``graph`` 平行给出每条残余弧的**实际费用**：唯一性
    核查须用真实时差费用（不含字典序奖励），否则等价时差方案会被奖励
    项误判为费用更高。
    """
    n_nodes = len(graph)
    dist = [0] * n_nodes
    in_queue = [True] * n_nodes
    queue: deque[int] = deque(range(n_nodes))
    while queue:
        u = queue.popleft()
        in_queue[u] = False
        du = dist[u]
        for ei, arc in enumerate(graph[u]):
            v, cap, _cost, _rev = arc
            cost = arc_cost[u][ei]
            if cap > 0 and dist[v] > du + cost:
                dist[v] = du + cost
                if not in_queue[v]:
                    in_queue[v] = True
                    queue.append(v)
    return dist


def _true_residual_costs(network: _FlowNetwork) -> list[list[int]]:
    """构造与残余网络平行的“真实时差费用”表。

    建网费用含字典序奖励（``时差*SCALE + OFFSET - 奖励``），只能选出
    第三级字典序唯一解；核查“同配对数、同总时差”的等优方案时须改用
    配对的真实时差（微秒）：L->R 正向弧费用为时差，其反向弧为负时差，
    源/汇相关弧费用为 0。当前流在真实费用下仍是最小费用最大流
    （前两级目标一致），故其残量网络存在可行位势。
    """
    costs: list[list[int]] = [
        [0] * len(arcs) for arcs in network.graph
    ]
    for rank, (_i, _j, weight, _key) in enumerate(network.edges):
        u, fwd = network.match_arc[rank]
        v, _cap, _cost, rev = network.graph[u][fwd]
        costs[u][fwd] = weight
        costs[v][rev] = -weight
    return costs


def _strongly_connected_components(
    adjacency: Sequence[Sequence[int]],
) -> list[int]:
    """迭代版 Kosaraju：返回每个节点的强连通分量编号（两遍 DFS）。"""
    n_nodes = len(adjacency)
    reverse: list[list[int]] = [[] for _ in range(n_nodes)]
    for u, neighbors in enumerate(adjacency):
        for v in neighbors:
            reverse[v].append(u)

    visited = [False] * n_nodes
    order: list[int] = []
    for start in range(n_nodes):
        if visited[start]:
            continue
        visited[start] = True
        stack: list[tuple[int, int]] = [(start, 0)]
        while stack:
            u, i = stack[-1]
            if i < len(adjacency[u]):
                v = adjacency[u][i]
                stack[-1] = (u, i + 1)
                if not visited[v]:
                    visited[v] = True
                    stack.append((v, 0))
            else:
                order.append(u)
                stack.pop()

    component = [-1] * n_nodes
    cid = 0
    for start in reversed(order):
        if component[start] != -1:
            continue
        component[start] = cid
        stack = [start]
        while stack:
            u = stack.pop()
            for v in reverse[u]:
                if component[v] == -1:
                    component[v] = cid
                    stack.append(v)
        cid += 1
    return component


def _split_locked_records(
    records: Sequence[Record],
    allowed_micros: int,
    locked_keys: Sequence[tuple[str, str]],
) -> tuple[list[Pair], set[str], int, list[Record]]:
    """校验并固定锁定对，返回 (锁定配对, 锁定记录号, 锁定总时差, 自由记录)。"""
    record_map = {r.record_no: r for r in records}
    locked: list[Pair] = []
    locked_ids: set[str] = set()
    locked_total = 0
    for raw_a, raw_b in locked_keys:
        a, b = (raw_a, raw_b) if raw_a <= raw_b else (raw_b, raw_a)
        problem = pair_eligibility(record_map, allowed_micros, a, b)
        if problem is not None:
            raise InvalidLockError(f"锁定 ({a}, {b}) 已失效: {problem[1]}")
        if a in locked_ids or b in locked_ids:
            raise InvalidLockError(f"记录被多个锁定对重复占用: {a}, {b}")
        locked_ids.add(a)
        locked_ids.add(b)
        weight = abs(record_map[a].ts_micros - record_map[b].ts_micros)
        locked_total += weight
        locked.append(
            Pair(
                record_a=a,
                record_b=b,
                time_diff_seconds=weight / MICROS_PER_SECOND,
            )
        )
    free_records = [r for r in records if r.record_no not in locked_ids]
    return locked, locked_ids, locked_total, free_records


def review_pair_uniqueness(
    records: Sequence[Record],
    allowed_diff_seconds: float,
    locked_keys: Sequence[tuple[str, str]] = (),
) -> UniquenessReview:
    """核查当前最优结果中每个非锁定配对是否为等优方案中的唯一选择。

    ``records`` 应为当前版本下**未隔离**的全部日志（隔离由服务层剔除）；
    ``locked_keys`` 为当前人工锁定（服务层已保证合法）。返回的条目按
    配对键（记录号字典序）升序，锁定对不在条目中，仅以 ``locked_pairs``
    原样回显（锁定对强制入选，不可替换）。
    """
    allowed_micros = int(round(allowed_diff_seconds * MICROS_PER_SECOND))
    locked, _locked_ids, locked_total, free_records = _split_locked_records(
        records, allowed_micros, locked_keys
    )

    candidate_edges = _build_candidate_edges(free_records, allowed_micros)
    reviews: list[PairReview] = []
    free_total = 0
    free_flow = 0
    if candidate_edges:
        network = _solve_flow_network(free_records, candidate_edges)
        free_total = network.total_micros
        free_flow = network.flow

        # 紧弧残量子图与其强连通分量（紧弧按真实时差费用判定）。
        true_cost = _true_residual_costs(network)
        potential = _feasible_potential(network.graph, true_cost)
        n_nodes = len(network.graph)
        tight_adj: list[list[int]] = [[] for _ in range(n_nodes)]
        for u, arcs in enumerate(network.graph):
            for ei, arc in enumerate(arcs):
                v, cap, _cost, _rev = arc
                if cap > 0 and potential[u] + true_cost[u][ei] == potential[v]:
                    tight_adj[u].append(v)
        component = _strongly_connected_components(tight_adj)

        current_free_pairs = _extract_flow_pairs(network)
        current_full = sorted(locked + current_free_pairs, key=lambda p: p.as_tuple())
        current_map = {p.as_tuple(): p for p in current_full}

        for rank, (i, j, weight, key) in enumerate(network.edges):
            u, fwd = network.match_arc[rank]
            if network.graph[u][fwd][1] != 0:
                continue  # 仅核查入选当前结果的配对。
            v = network.graph[u][fwd][0]
            arc_tight = potential[u] + true_cost[u][fwd] == potential[v]
            replaceable = arc_tight and component[u] == component[v]

            pair = Pair(
                record_a=key[0],
                record_b=key[1],
                time_diff_seconds=weight / MICROS_PER_SECOND,
            )
            if not replaceable:
                reviews.append(PairReview(pair, True, None))
                continue

            # 删除该边后重跑三级费用流：所得即不含该对的字典序最小
            # 合法完整方案；删边导致配对数下降/总时差增大时按不可替换。
            alt_pairs, alt_matched, alt_total = _match_free(
                free_records,
                allowed_micros,
                excluded_keys=frozenset({key}),
            )
            alt_full = sorted(locked + alt_pairs, key=lambda p: p.as_tuple())
            if (
                len(alt_pairs) != network.flow
                or alt_total != network.total_micros
                or key in {p.as_tuple() for p in alt_pairs}
            ):
                reviews.append(PairReview(pair, True, None))
                continue

            alt_map = {p.as_tuple(): p for p in alt_full}
            removed = [
                current_map[k] for k in sorted(set(current_map) - set(alt_map))
            ]
            added = [alt_map[k] for k in sorted(set(alt_map) - set(current_map))]
            unpaired = tuple(
                sorted(
                    r.record_no
                    for r in free_records
                    if r.record_no not in alt_matched
                )
            )
            reviews.append(
                PairReview(
                    pair=pair,
                    universal=False,
                    alternative=PairAlternative(
                        pairs=tuple(alt_full),
                        unpaired=unpaired,
                        total_time_diff_seconds=(
                            locked_total + alt_total
                        ) / MICROS_PER_SECOND,
                        added_pairs=tuple(added),
                        removed_pairs=tuple(removed),
                    ),
                )
            )

    return UniquenessReview(
        pairs=tuple(reviews),
        locked_pairs=tuple(sorted(locked, key=lambda p: p.as_tuple())),
        optimal_pair_count=len(locked) + free_flow,
        total_time_diff_seconds=(locked_total + free_total) / MICROS_PER_SECOND,
    )


# --------------------------------------------------------------------------
# 未互证日志缺口诊断（复核员按演练查看未配对记录的缺口归因）
#
# 对每条当前结果中的未配对记录，按当前日志依次归因（前者命中即不再下探）：
#   1. candidate_occupied（候选已占用）：存在完全满足互指、波段、模式、
#      时差条件的对方记录。最大基数匹配下该候选必然已被别的配对占用
#      （否则增广这条边即可多配一对），故未入选的原因是候选被占用；
#   2. time_diff_exceeded（时差超限）：否则存在互指且波段、模式相同的
#      对方记录（时差必然都超过允许值，否则会落入上一类）；
#   3. band_or_mode_mismatch（波段或模式不符）：否则存在互指的对方记录；
#   4. missing_counterpart_log（缺少对方日志）：没有任何互指记录。
# 前三类返回该类候选中时差最小、再按记录号最小的对方记录及实际时差。
# --------------------------------------------------------------------------
GAP_CANDIDATE_OCCUPIED = "candidate_occupied"
GAP_TIME_DIFF_EXCEEDED = "time_diff_exceeded"
GAP_BAND_OR_MODE_MISMATCH = "band_or_mode_mismatch"
GAP_MISSING_COUNTERPART = "missing_counterpart_log"

GAP_REASON_LABELS: dict[str, str] = {
    GAP_CANDIDATE_OCCUPIED: "候选已占用",
    GAP_TIME_DIFF_EXCEEDED: "时差超限",
    GAP_BAND_OR_MODE_MISMATCH: "波段或模式不符",
    GAP_MISSING_COUNTERPART: "缺少对方日志",
}


@dataclass(frozen=True)
class GapItem:
    """一条未配对记录的缺口诊断结论。

    末类（缺少对方日志）``counterpart_record_no`` 与
    ``time_diff_seconds`` 均为 ``None``。
    """

    record_no: str
    reason: str
    counterpart_record_no: str | None
    time_diff_seconds: float | None


def _is_mutual(r1: Record, r2: Record) -> bool:
    """两条日志是否相互指认（本台/对方呼号互换）。"""
    return r1.station == r2.counterpart and r1.counterpart == r2.station


def diagnose_gaps(
    records: Sequence[Record],
    allowed_diff_seconds: float,
    unpaired: Sequence[str],
) -> list[GapItem]:
    """按当前日志为每条未配对记录归因缺口原因。

    ``unpaired`` 取当前互证结果的未配对记录（其记录必现存于
    ``records``）；结果按记录号升序返回。前三类的对方记录在各自候选
    集合内按“时差最小、再记录号最小”择一返回。
    """
    allowed_micros = int(round(allowed_diff_seconds * MICROS_PER_SECOND))
    record_map = {r.record_no: r for r in records}
    items: list[GapItem] = []
    for no in sorted(unpaired):
        u = record_map[no]
        # 三个逐层收窄的候选集（均含 (时差微秒, 对方记录号)）：
        # mutual ⊇ band_mode ⊇ eligible。
        mutual: list[tuple[int, str]] = []
        band_mode: list[tuple[int, str]] = []
        eligible: list[tuple[int, str]] = []
        for v in records:
            if v.record_no == no or not _is_mutual(u, v):
                continue
            diff_micros = abs(u.ts_micros - v.ts_micros)
            mutual.append((diff_micros, v.record_no))
            if v.band == u.band and v.mode == u.mode:
                band_mode.append((diff_micros, v.record_no))
                if diff_micros <= allowed_micros:
                    eligible.append((diff_micros, v.record_no))

        if eligible:
            reason, pool = GAP_CANDIDATE_OCCUPIED, eligible
        elif band_mode:
            reason, pool = GAP_TIME_DIFF_EXCEEDED, band_mode
        elif mutual:
            reason, pool = GAP_BAND_OR_MODE_MISMATCH, mutual
        else:
            items.append(GapItem(no, GAP_MISSING_COUNTERPART, None, None))
            continue
        diff_micros, other_no = min(pool, key=lambda x: (x[0], x[1]))
        items.append(
            GapItem(
                record_no=no,
                reason=reason,
                counterpart_record_no=other_no,
                time_diff_seconds=diff_micros / MICROS_PER_SECOND,
            )
        )
    return items
