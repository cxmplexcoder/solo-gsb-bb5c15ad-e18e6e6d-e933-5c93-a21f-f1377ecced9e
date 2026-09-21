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
* 每个容器持有持久化 ``version``（创建为 0）。一批转移**成功提交**时，
  每个被触碰容器的版本恰好 +1（同批多次出现也只 +1），并随响应返回。
  请求可携带 ``expected_versions`` 做乐观并发控制：必须恰好覆盖本批全部
  源和目标容器（否则 422），且在当前事务内逐一比对，任一不符返回
  409 并整批回滚；预演复用同一套校验但不递增、不写库。
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
def _load_container(conn: sqlite3.Connection, cid: str) -> tuple[State, int] | None:
    """读取容器当前来源构成与持久化版本；容器不存在返回 None。"""
    row = conn.execute(
        "SELECT version FROM containers WHERE id = ?", (cid,)
    ).fetchone()
    if row is None:
        return None
    state: State = {}
    for r in conn.execute(
        "SELECT source_id, amount FROM components WHERE container_id = ?", (cid,)
    ):
        state[r["source_id"]] = Decimal(r["amount"])
    return state, int(row["version"])


def _persist_states(
    conn: sqlite3.Connection,
    states: Mapping[str, State],
    versions: Mapping[str, int],
) -> None:
    """把本批被触碰过的容器整体写回（版本与组分一起，处在同一事务内）。"""
    for cid, state in states.items():
        conn.execute(
            "UPDATE containers SET version = ? WHERE id = ?", (versions[cid], cid)
        )
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


def _check_expected_versions_coverage(
    prepared: list[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None,
) -> None:
    """expected_versions 提供时必须恰好覆盖本批全部源与目标容器。

    纯请求级校验（不依赖数据库状态），在事务开启前完成；
    缺少或多余都返回 422 invalid_expected_versions。
    """
    if expected_versions is None:
        return
    involved = {cid for src, dst, _ in prepared for cid in (src, dst)}
    provided = set(expected_versions)
    if provided == involved:
        return
    parts = []
    missing = sorted(involved - provided)
    extra = sorted(provided - involved)
    if missing:
        parts.append(f"缺少: {', '.join(missing)}")
    if extra:
        parts.append(f"多余: {', '.join(extra)}")
    raise LineageError(
        422,
        "invalid_expected_versions",
        "expected_versions 必须恰好覆盖本批全部源和目标容器（"
        + "；".join(parts)
        + "），整批拒绝",
    )


def _simulate_transfers(
    conn: sqlite3.Connection,
    prepared: list[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
) -> tuple[dict[str, State], dict[str, int], list[dict[str, Decimal]]]:
    """按数组顺序在内存中依次模拟一批转移（链式转移看到本批前序结果）。

    只从数据库读取、在内存状态上演算，**不写库**；返回
    ``(被触碰容器的最终状态, 被触碰容器的当前版本, 每一步从各祖先来源实际扣取的份额)``。
    提交与预演共用本函数，校验规则、错误码与条目序号因此完全一致。

    ``expected_versions`` 提供时（覆盖范围已由
    :func:`_check_expected_versions_coverage` 校验），在读取每个容器状态的
    同一事务内比对其版本，任一不符抛 409 stale_container。
    """
    touched: dict[str, State] = {}
    versions: dict[str, int] = {}
    taken_steps: list[dict[str, Decimal]] = []

    for index, (src, dst, volume) in enumerate(prepared, start=1):
        # 未知容器（初始样品与空容器都在 containers 表中）
        for cid in (src, dst):
            if cid not in touched:
                loaded = _load_container(conn, cid)
                if loaded is None:
                    raise LineageError(
                        404,
                        "unknown_container",
                        f"第 {index} 条转移中的容器 '{cid}' 不存在，整批拒绝",
                    )
                state, version = loaded
                # 乐观并发校验：与状态读取处于同一事务，版本不符整批拒绝
                if (
                    expected_versions is not None
                    and expected_versions[cid] != version
                ):
                    raise LineageError(
                        409,
                        "stale_container",
                        (
                            f"容器 '{cid}' 的版本已变化：期望 "
                            f"{expected_versions[cid]}，当前 {version}，整批拒绝"
                        ),
                    )
                touched[cid] = state
                versions[cid] = version

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

    return touched, versions, taken_steps


def apply_transfers(
    conn: sqlite3.Connection,
    transfers: Iterable[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
) -> tuple[int, dict[str, int]]:
    """依次执行一批转移，全部成功才提交，任一失败整批回滚。

    成功时每个被触碰容器的持久化版本恰好 +1（同批多次出现也只 +1）。
    返回 ``(执行的转移条数, {被触碰容器ID: 提交后的新版本})``。
    """
    prepared = _prepare_transfers(transfers)
    _check_expected_versions_coverage(prepared, expected_versions)

    conn.execute("BEGIN")
    try:
        touched, versions, _taken = _simulate_transfers(
            conn, prepared, expected_versions
        )
        new_versions = {cid: versions[cid] + 1 for cid in sorted(touched)}
        _persist_states(conn, touched, new_versions)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return len(prepared), new_versions


def preview_transfers(
    conn: sqlite3.Connection,
    transfers: Iterable[tuple[str, str, Decimal]],
    expected_versions: Mapping[str, int] | None = None,
) -> dict:
    """在数据库当前状态上预演一批转移，绝不写入数据库。

    与 :func:`apply_transfers` 共享全部校验与最大余额分摊逻辑
    （错误码、条目序号一致）；模拟在事务内进行，结束即回滚，
    成功或失败数据库都完全不变。

    返回每一步从各祖先来源实际扣取的精确份额，以及全部受影响
    （被本批触碰的）容器在预演结束时的最终体积与来源构成；容器的
    ``version`` 为**预测提交后**的版本（当前版本 +1），版本本身不递增。
    """
    prepared = _prepare_transfers(transfers)
    _check_expected_versions_coverage(prepared, expected_versions)

    conn.execute("BEGIN")
    try:
        touched, versions, taken_steps = _simulate_transfers(
            conn, prepared, expected_versions
        )
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
            # version 为预测提交后的版本（当前 +1），预演本身不递增、不写库
            _format_container_status(cid, touched[cid], versions[cid] + 1)
            for cid in sorted(touched)
        ],
    }


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _format_container_status(cid: str, state: State, version: int) -> dict:
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
        "version": version,
        "volume": str(total),
        "sources": sources,
        "proportion_scale": settings.proportion_scale,
    }


def get_container_status(conn: sqlite3.Connection, cid: str) -> dict:
    """返回容器当前体积、各祖先来源占比与持久化版本。"""
    loaded = _load_container(conn, cid)
    if loaded is None:
        raise LineageError(404, "unknown_container", f"容器 '{cid}' 不存在")
    state, version = loaded
    return _format_container_status(cid, state, version)
