"""样品分装谱系的核心逻辑：登记容器/初始样品、批量转移、转移预演、查询状态。

约定
----
* 体积一律使用 :class:`decimal.Decimal`，数据库中以字符串存储，全程无浮点。
* 容器状态表示为 ``{祖先初始样品ID: 绝对体积份额}``，某容器内所有份额之和
  即当前体积。转移时按源容器当时的混合比例传播 *全部* 祖先来源。
* 一批转移严格“依次”发生（后面的转移能看到前面转移的结果）。
* 任一转移失败（未知容器 / 自转移 / 源液不足 / 参数非法），整批拒绝，
  数据库状态完全不变（单个事务回滚）。
* 预演（preview）与真实提交共享同一份校验与分摊实现（见
  :func:`_simulate_transfers`），错误码与条目序号完全一致；
  预演只读不写，无论成功失败都不改变数据库。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

from .config import VOLUME_QUANTUM, settings

# 组成：source_id（祖先初始样品）-> 绝对体积份额
State = dict[str, Decimal]


class LineageError(Exception):
    """业务错误，映射为 HTTP 4xx。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Decimal 辅助
# --------------------------------------------------------------------------- #
def parse_volume(raw: Decimal, field: str) -> Decimal:
    """校验入参体积：有限、非负、且不超过配置精度。"""
    if not raw.is_finite():
        raise LineageError(422, "invalid_volume", f"{field} 必须是有限数")
    if raw < 0:
        raise LineageError(422, "invalid_volume", f"{field} 不能为负")
    if raw != raw.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_EVEN):
        raise LineageError(
            422,
            "invalid_volume",
            f"{field} 的小数位不能超过 {settings.decimal_scale} 位",
        )
    return raw.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_EVEN)


def apportion(total: Decimal, shares: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """按份额（混合比例）把待取 ``total`` 切分给各祖先来源。

    采用最大余额法（Hare quota + Largest remainder）：

    1. 每份先向下取整到体积刻度；
    2. 差额（不超过来源个数个刻度）按“被舍掉的余额”从大到小补 1 个刻度。

    因此结果之和 **精确等于** ``total``，每份满足
    ``0 <= 取出额 <= 该来源在源容器中的份额``，且比例与真实混合比例之差
    不超过 1 个体积刻度。源容器为空时调用方应先拦截。
    """
    sources = list(shares.keys())
    pool = sum(shares.values(), Decimal(0))

    # 每份先向下取整到体积刻度（ROUND_FLOOR 保证余数非负，且余数步数 < 来源数）
    raw = {
        s: (total * shares[s] / pool).quantize(VOLUME_QUANTUM, rounding=ROUND_FLOOR)
        for s in sources
    }
    allocated = sum(raw.values(), Decimal(0))
    remainder_steps = int((total - allocated) / VOLUME_QUANTUM)

    # 被舍掉的余额（按它从大到小补发），余额相同按来源 ID 打破平局，保证确定性
    leftovers = {s: total * shares[s] / pool - raw[s] for s in sources}
    order = sorted(sources, key=lambda s: (-leftovers[s], s))
    result = dict(raw)
    for i in range(remainder_steps):
        result[order[i % len(order)]] += VOLUME_QUANTUM
    return result


# --------------------------------------------------------------------------- #
# 状态读写
# --------------------------------------------------------------------------- #
def _load_state(conn: sqlite3.Connection, cid: str) -> State | None:
    row = conn.execute(
        "SELECT 1 FROM containers WHERE id = ?", (cid,)
    ).fetchone()
    if row is None:
        return None
    state: State = {}
    for r in conn.execute(
        "SELECT source_id, amount FROM components WHERE container_id = ?", (cid,)
    ):
        state[r["source_id"]] = Decimal(r["amount"])
    return state


def _persist_states(conn: sqlite3.Connection, states: Mapping[str, State]) -> None:
    """把本批被触碰过的容器整体写回（删后重插，处在同一事务内）。"""
    for cid, state in states.items():
        conn.execute("DELETE FROM components WHERE container_id = ?", (cid,))
        conn.executemany(
            "INSERT INTO components (container_id, source_id, amount) VALUES (?, ?, ?)",
            [(cid, sid, str(amount)) for sid, amount in sorted(state.items())],
        )


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #
def register_initial_sample(
    conn: sqlite3.Connection, sample_id: str, volume: Decimal
) -> Decimal:
    """登记一个带体积的初始样品：它是自身唯一的祖先来源。

    返回按配置精度规范化后的体积。
    """
    volume = parse_volume(volume, "volume")
    if volume == 0:
        raise LineageError(422, "invalid_volume", "初始样品体积必须大于 0")
    conn.execute("BEGIN")
    try:
        try:
            conn.execute("INSERT INTO containers (id) VALUES (?)", (sample_id,))
        except sqlite3.IntegrityError:
            raise LineageError(
                409, "container_exists", f"容器/样品 '{sample_id}' 已存在"
            )
        conn.execute(
            "INSERT INTO components (container_id, source_id, amount) VALUES (?, ?, ?)",
            (sample_id, sample_id, str(volume)),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return volume


def register_empty_container(conn: sqlite3.Connection, cid: str) -> None:
    """登记一个空容器（之后可作为转移目标）。"""
    conn.execute("BEGIN")
    try:
        try:
            conn.execute("INSERT INTO containers (id) VALUES (?)", (cid,))
        except sqlite3.IntegrityError:
            raise LineageError(
                409, "container_exists", f"容器/样品 '{cid}' 已存在"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- #
# 批量转移：共享的校验与模拟核心
# --------------------------------------------------------------------------- #
def _prepare_transfers(
    transfers: Iterable[tuple[str, str, Decimal]]
) -> list[tuple[str, str, Decimal]]:
    """预先校验、量化每一条转移的体积与容器参数。

    任何失败都发生在事务开启与状态读取之前，数据库必然不变。
    """
    prepared: list[tuple[str, str, Decimal]] = []
    for index, (src, dst, raw_volume) in enumerate(transfers):
        volume = parse_volume(raw_volume, f"第 {index + 1} 条转移的 volume")
        if not src:
            raise LineageError(422, "invalid_container", f"第 {index + 1} 条转移缺少源容器")
        if not dst:
            raise LineageError(422, "invalid_container", f"第 {index + 1} 条转移缺少目标容器")
        if src == dst:
            raise LineageError(
                422,
                "self_transfer",
                f"第 {index + 1} 条转移为自转移：'{src}' -> '{src}'，整批拒绝",
            )
        prepared.append((src, dst, volume))
    return prepared


def _simulate_transfers(
    conn: sqlite3.Connection, prepared: list[tuple[str, str, Decimal]]
) -> tuple[dict[str, State], list[dict[str, Decimal]]]:
    """按数组顺序在内存中依次模拟一批转移（链式转移看到本批前序结果）。

    只从数据库读取、在内存状态上演算，**不写库**；返回
    ``(被触碰容器的最终状态, 每一步从各祖先来源实际扣取的份额)``。
    提交与预演共用本函数，校验规则、错误码与条目序号因此完全一致。
    """
    touched: dict[str, State] = {}
    taken_steps: list[dict[str, Decimal]] = []

    for index, (src, dst, volume) in enumerate(prepared, start=1):
        # 未知容器（初始样品与空容器都在 containers 表中）
        for cid in (src, dst):
            if cid not in touched:
                state = _load_state(conn, cid)
                if state is None:
                    raise LineageError(
                        404,
                        "unknown_container",
                        f"第 {index} 条转移中的容器 '{cid}' 不存在，整批拒绝",
                    )
                touched[cid] = state

        src_state = touched[src]
        dst_state = touched[dst]
        src_total = sum(src_state.values(), Decimal(0))

        if volume == 0:
            raise LineageError(
                422,
                "invalid_volume",
                f"第 {index} 条转移体积必须大于 0，整批拒绝",
            )
        if volume > src_total:
            raise LineageError(
                422,
                "insufficient_volume",
                (
                    f"第 {index} 条转移源液不足：'{src}' 当前 {src_total}，"
                    f"请求 {volume}，整批拒绝"
                ),
            )

        # 按源容器“当时”的混合比例取出全部祖先来源
        with localcontext() as ctx:
            ctx.prec = settings.decimal_precision
            taken = apportion(volume, src_state)

        for sid, amount in taken.items():
            src_state[sid] -= amount
            dst_state[sid] = dst_state.get(sid, Decimal(0)) + amount
            if src_state[sid] == 0:
                del src_state[sid]
        taken_steps.append(taken)

    return touched, taken_steps


def apply_transfers(
    conn: sqlite3.Connection, transfers: Iterable[tuple[str, str, Decimal]]
) -> int:
    """依次执行一批转移，全部成功才提交，任一失败整批回滚。

    返回执行的转移条数。
    """
    prepared = _prepare_transfers(transfers)

    conn.execute("BEGIN")
    try:
        touched, _taken = _simulate_transfers(conn, prepared)
        _persist_states(conn, touched)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(prepared)


def preview_transfers(
    conn: sqlite3.Connection, transfers: Iterable[tuple[str, str, Decimal]]
) -> dict:
    """在数据库当前状态上预演一批转移，绝不写入数据库。

    与 :func:`apply_transfers` 共享全部校验与最大余额分摊逻辑
    （错误码、条目序号一致）；模拟在事务内进行，结束即回滚，
    成功或失败数据库都完全不变。

    返回每一步从各祖先来源实际扣取的精确份额，以及全部受影响
    （被本批触碰的）容器在预演结束时的最终体积与来源构成。
    """
    prepared = _prepare_transfers(transfers)

    conn.execute("BEGIN")
    try:
        touched, taken_steps = _simulate_transfers(conn, prepared)
    finally:
        # 预演只读不落库：无论成功失败都回滚，数据库状态必然不变
        conn.execute("ROLLBACK")

    steps: list[dict] = []
    for index, ((src, dst, volume), taken) in enumerate(
        zip(prepared, taken_steps), start=1
    ):
        steps.append(
            {
                "index": index,
                "source": src,
                "destination": dst,
                "volume": str(volume),
                "taken": [
                    {"source_id": sid, "amount": str(amount)}
                    for sid, amount in sorted(taken.items())
                ],
            }
        )

    return {
        "steps": steps,
        "containers": [
            _format_container_status(cid, touched[cid]) for cid in sorted(touched)
        ],
    }


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _format_container_status(cid: str, state: State) -> dict:
    """把内存中的容器状态格式化为查询/预演共用的输出结构。"""
    total = sum(state.values(), Decimal(0))
    q = Decimal(1).scaleb(-settings.proportion_scale)
    sources: list[dict] = []

    with localcontext() as ctx:
        ctx.prec = settings.decimal_precision
        for sid, amount in sorted(state.items()):
            proportion = (
                (amount / total).quantize(q, rounding=ROUND_HALF_UP)
                if total > 0
                else Decimal(0).quantize(q)
            )
            sources.append(
                {
                    "source_id": sid,
                    "amount": str(amount),
                    "proportion": str(proportion),
                }
            )

    return {
        "container_id": cid,
        "volume": str(total),
        "sources": sources,
        "proportion_scale": settings.proportion_scale,
    }


def get_container_status(conn: sqlite3.Connection, cid: str) -> dict:
    """返回容器当前体积与各祖先来源占比。"""
    state = _load_state(conn, cid)
    if state is None:
        raise LineageError(404, "unknown_container", f"容器 '{cid}' 不存在")
    return _format_container_status(cid, state)
