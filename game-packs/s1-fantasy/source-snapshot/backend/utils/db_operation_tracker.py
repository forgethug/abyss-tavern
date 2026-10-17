"""数据库操作追踪器。

把一次 db_repository 方法调用（一般是 @monitor_performance 修饰的方法）期间的所有
SQL 事件聚合到一个 OperationTracker，方法结束时由 monitor_performance 一次性输出
total / sql_total / sql_max / non_sql / 最慢 N 条 SQL 文本，避免高并发场景下"方法
慢日志"和"逐条 SQL 慢日志"互相交错对不上号。

协程隔离用 contextvars.ContextVar 实现，gevent 已经原生兼容 contextvars，无需特殊处理。

与 utils/db_slow_query_logger 的关系：
- 那边只挂 SQL 事件钩子，把数据 push 进 current_tracker()，不再单独打日志
- 没有活动 tracker 时（SQL 跑在 @monitor_performance 之外的路径），SQL 不被记录
"""
import contextvars
import time
from contextlib import contextmanager


_current_tracker = contextvars.ContextVar('db_op_tracker', default=None)


class OperationTracker:
    """单次操作内累积所有 SQL 事件，输出时一次性 dump。"""

    __slots__ = ('op_name', '_start_ts', '_sql_events', '_phases')

    def __init__(self, op_name):
        self.op_name = op_name
        self._start_ts = time.perf_counter()
        self._sql_events = []
        self._phases = {}

    def record_sql(self, elapsed, sql, params_repr):
        self._sql_events.append({
            'elapsed': elapsed,
            'sql': sql,
            'params': params_repr,
        })

    @property
    def total_elapsed(self):
        return time.perf_counter() - self._start_ts

    @property
    def sql_count(self):
        return len(self._sql_events)

    @property
    def sql_total_time(self):
        return sum(e['elapsed'] for e in self._sql_events)

    @property
    def sql_max_time(self):
        return max((e['elapsed'] for e in self._sql_events), default=0.0)

    def format_breakdown(self, total_elapsed=None, top_n=3, sql_max_chars=200):
        """拼成一段单行摘要：sql_count / sql_total / sql_max / non_sql / 最慢 N 条 SQL。"""
        total = self.total_elapsed if total_elapsed is None else total_elapsed
        sql_total = self.sql_total_time
        non_sql = total - sql_total
        summary = (
            f'sql_count={self.sql_count} '
            f'sql_total={sql_total:.3f}s '
            f'sql_max={self.sql_max_time:.3f}s '
            f'non_sql={non_sql:.3f}s'
        )
        if self._phases:
            summary += ' db_phases=[' + ', '.join(
                f'{name}={elapsed:.3f}s' for name, elapsed in self._phases.items()
            ) + ']'
        if not self._sql_events:
            return summary + ' sql_breakdown=-'
        sorted_events = sorted(self._sql_events, key=lambda e: -e['elapsed'])[:top_n]
        parts = []
        for i, event in enumerate(sorted_events, 1):
            sql_text = event['sql'][:sql_max_chars]
            parts.append(f'#{i}({event["elapsed"]:.3f}s){sql_text}')
        return summary + ' sql_breakdown=[' + ' || '.join(parts) + ']'


@contextmanager
def track_operation(op_name):
    """绑定一个新 tracker 到当前协程上下文，离开时自动还原（支持嵌套）。"""
    tracker = OperationTracker(op_name)
    token = _current_tracker.set(tracker)
    try:
        yield tracker
    finally:
        _current_tracker.reset(token)


def current_tracker():
    return _current_tracker.get()


@contextmanager
def track_db_phase(name):
    """Measure a named client-side phase, including failures; never record values.

    Phases are a separate wall-time view. execute_fetch includes SQL time and
    must not be added to sql_total/non_sql to calculate the operation total.
    """
    tracker = current_tracker()
    started = time.perf_counter()
    try:
        yield
    finally:
        if tracker is not None:
            tracker._phases[name] = tracker._phases.get(name, 0.0) + time.perf_counter() - started
