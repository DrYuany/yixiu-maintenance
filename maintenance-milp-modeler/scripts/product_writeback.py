"""业务台账：把排程方案落成检修单／出海清单／执行反馈，并驱动告警任务状态机。

这个模块不求解，只回答一个问题：已经算出来的方案，怎么变成现场能执行、能验收的单子。

四张表各司其职，缺一张闭环就断了：
  告警任务.csv  —— 任务的身份与状态（谁还没修、谁在计划里、谁修完了）
  检修单.csv    —— 一次下发覆盖哪些任务、排在哪个窗口哪条线、审批到哪一步
  出海清单.csv  —— 每张单要带几个维修包、跟哪条作业线
  执行反馈.csv  —— 现场回填的实际开工/完工/消耗，是任务真正关闭的唯一依据

状态机（写回 告警任务.任务状态）：
  待安排/待补充窗口 --下发--> 已排程 --完工反馈--> 已完成
        ^                        |
        +---拒绝 / 下一轮作废-----+

为什么必须回写：
  不回写的话，"待安排 N 项"永远等于"收到过且没标完成/取消的全部告警"，
  同一批任务会被每一轮规划重复排进去，检修单和出海清单永远是空的。
"""
from __future__ import annotations

import csv
import json
import os
import uuid
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parents[2] / 'data' / 'phase1-demo'

TASK_PENDING = ('待安排', '待补充窗口')
TASK_SCHEDULED = '已排程'
TASK_DONE = '已完成'
TASK_CANCELLED = '已取消'

ORDER_PENDING = '待审批'
ORDER_APPROVED = '已批准'
ORDER_REJECTED = '已拒绝'
ORDER_VOID = '已作废'

ORDER_FIELDS = ['检修单号', '方案版本', '任务编号', '风机编号', '部件编号',
                '窗口编号', '作业线编号', '计划开工时间', '计划完工时间', '审批状态']
MANIFEST_FIELDS = ['清单编号', '检修单号', '窗口编号', '作业线编号', '任务编号',
                   '部件编号', '维修包数量']
FEEDBACK_FIELDS = ['反馈编号', '检修单号', '实际开工时间', '实际完工时间',
                   '实际消耗数量', '退料数量', '故障是否消除']
TASK_FIELDS = ['告警编号', '任务编号', '风机编号', '部件编号', '故障描述', '严重等级',
               '发生时间', '首次规划时间', '截止窗口编号', '截止时间（不含）',
               '任务状态', '备件需求数量', '数据来源']


# --------------------------------------------------------------------------- CSV


def read_rows(path: Path, fields: list[str]) -> list[dict]:
    """读台账。文件不存在就按给定表头当作空表，不报错。"""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return [{key: (row.get(key) or '') for key in fields} for row in csv.DictReader(handle)]


def write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    """原子写回。先写临时文件再替换，中途失败不会留下半张表。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, '') for key in fields})
    os.replace(temp, path)


def _paths(root: Path | None) -> dict[str, Path]:
    base = Path(root) if root else DEMO_DIR
    return {
        'tasks': base / '告警任务.csv',
        'orders': base / '检修单.csv',
        'manifests': base / '出海清单.csv',
        'feedback': base / '执行反馈.csv',
    }


def _short() -> str:
    return uuid.uuid4().hex[:8].upper()


# 四张台账在 JSON 输入包里的对应键。求解器只认 JSON，页面只认 CSV，
# 所以每次写盘必须两边一起写，否则一边永远看到旧状态。
_JSON_KEYS = {
    'tasks': '告警任务',
    'orders': '检修单',
    'manifests': '出海清单',
    'feedback': '执行反馈',
}
# CSV 读出来全是字符串，回写 JSON 时要还原成数值，否则求解器的类型校验会挂。
_INT_FIELDS = {'备件需求数量', '维修包数量', '实际消耗数量', '退料数量'}


def _typed(row: dict) -> dict:
    out = dict(row)
    for key in _INT_FIELDS:
        if key in out:
            try:
                out[key] = int(float(out[key]))
            except (TypeError, ValueError):
                pass
    return out


def _sync_json(folder: Path, payload: dict[str, list[dict]]) -> None:
    """把台账同步进求解器的输入包。

    真实数据包没有这个文件，那就只写 CSV——那种场景下台账由外部系统承载。
    """
    path = Path(folder) / '初版完整数据.json'
    if not path.exists():
        return
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    changed = False
    for kind, rows in payload.items():
        key = _JSON_KEYS.get(kind)
        if not key:
            continue
        raw[key] = [_typed(row) for row in rows]
        changed = True
    if not changed:
        return
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def _save(
    root: Path | None,
    *,
    tasks: list[dict] | None = None,
    orders: list[dict] | None = None,
    manifests: list[dict] | None = None,
    feedback: list[dict] | None = None,
) -> None:
    """一次写盘：CSV 给人看，JSON 给求解器读，两份必须同时落地。"""
    paths = _paths(root)
    payload: dict[str, list[dict]] = {}
    for kind, fields, rows in (
        ('tasks', TASK_FIELDS, tasks),
        ('orders', ORDER_FIELDS, orders),
        ('manifests', MANIFEST_FIELDS, manifests),
        ('feedback', FEEDBACK_FIELDS, feedback),
    ):
        if rows is None:
            continue
        write_rows(paths[kind], fields, rows)
        payload[kind] = rows
    _sync_json(paths['tasks'].parent, payload)


# ----------------------------------------------------------------------- 状态机


def _task_rows(root: Path | None) -> list[dict]:
    return read_rows(_paths(root)['tasks'], TASK_FIELDS)


def _set_task_state(rows: list[dict], task_id: str, state: str, *, only_from=()) -> bool:
    for row in rows:
        if row.get('任务编号') != task_id:
            continue
        if only_from and row.get('任务状态') not in only_from:
            return False
        row['任务状态'] = state
        return True
    return False


def scheduled_task_ids(root: Path | None = None) -> set[str]:
    """已经进入已批准检修单的任务。下一轮规划必须把它当固定占用，不能重排。"""
    return {
        row['任务编号']
        for row in read_rows(_paths(root)['orders'], ORDER_FIELDS)
        if row.get('审批状态') == ORDER_APPROVED
    }


def approved_orders(root: Path | None = None) -> list[dict]:
    return [
        row for row in read_rows(_paths(root)['orders'], ORDER_FIELDS)
        if row.get('审批状态') == ORDER_APPROVED
    ]


# --------------------------------------------------------------------------- 下发


def dispatch(
    root: Path | None,
    *,
    schedule: list[dict],
    stamp,
    plan_version: str,
    window_id: str | None = None,
    operator: str = '规划员',
    reissue: bool = False,
) -> dict:
    """把一版推荐方案下发生成待审批检修单 + 出海清单，并把任务标记为已排程。

    schedule: 推荐报告里的 '排程' 列表，每行需含
              任务编号／风机编号／部件编号／窗口编号／作业线／开始／完成（分钟数）
    stamp:    把分钟数还原成带时区字符串的函数，通常传 Snapshot.stamp
    reissue:  为 True 时，已批准的单也会被新方案覆盖（用于"排程已变更，重新下发"）

    重复调用同一 plan_version 不会重复建单：已有同版本行直接跳过。
    """
    paths = _paths(root)
    orders = read_rows(paths['orders'], ORDER_FIELDS)
    manifests = read_rows(paths['manifests'], MANIFEST_FIELDS)
    tasks = _task_rows(root)

    existing_versions = {row['方案版本'] for row in orders}
    if plan_version in existing_versions and not reissue:
        return {
            '状态': '已存在',
            '方案版本': plan_version,
            '检修单号': next(row['检修单号'] for row in orders if row['方案版本'] == plan_version),
            '新建任务数': 0,
            '说明': f'方案版本 {plan_version} 已经下发过，未重复建单。',
        }

    locked = {} if reissue else {row['任务编号']: row for row in orders if row['审批状态'] == ORDER_APPROVED}

    # 上一版还没审批的计划，遇到新一轮排程就作废——任务放回待安排重新参与竞争。
    voided: list[str] = []
    release: list[str] = []
    for row in orders:
        if row['审批状态'] != ORDER_PENDING or row['方案版本'] == plan_version:
            continue
        row['审批状态'] = ORDER_VOID
        voided.append(row['任务编号'])
        release.append(row['任务编号'])

    order_id = 'WO-' + _short()
    created: list[dict] = []
    skipped: list[dict] = []
    for item in schedule:
        task_id = str(item['任务编号'])
        if task_id in locked:
            skipped.append({'任务编号': task_id, '原因': f"已在已批准检修单 {locked[task_id]['检修单号']} 中"})
            continue
        if any(row['任务编号'] == task_id and row['方案版本'] == plan_version for row in orders):
            continue
        window = str(item.get('窗口编号') or window_id or '')
        start, finish = stamp(int(item['开始'])), stamp(int(item['完成']))
        orders.append({
            '检修单号': order_id, '方案版本': plan_version, '任务编号': task_id,
            '风机编号': item.get('风机编号', ''), '部件编号': item.get('部件编号', ''),
            '窗口编号': window, '作业线编号': str(item.get('作业线', '')),
            '计划开工时间': start, '计划完工时间': finish, '审批状态': ORDER_PENDING,
        })
        manifests.append({
            '清单编号': 'MF-' + _short(), '检修单号': order_id, '窗口编号': window,
            '作业线编号': str(item.get('作业线', '')), '任务编号': task_id,
            '部件编号': item.get('部件编号', ''), '维修包数量': 1,
        })
        created.append({'任务编号': task_id, '窗口编号': window,
                        '作业线': item.get('作业线'), '开工': start, '完工': finish})

    if not created and not voided:
        return {
            '状态': '无可下发任务',
            '方案版本': plan_version,
            '检修单号': None,
            '新建任务数': 0,
            '跳过': skipped,
            '说明': '这一版排程里的任务要么已经下发过、要么已在已批准检修单里。',
        }

    moved = 0
    for row in created:
        if _set_task_state(tasks, row['任务编号'], TASK_SCHEDULED, only_from=TASK_PENDING):
            moved += 1
    released = 0
    for task_id in set(release):
        if task_id in {item['任务编号'] for item in created}:
            continue
        if _set_task_state(tasks, task_id, '待安排', only_from=(TASK_SCHEDULED,)):
            released += 1

    _save(root, orders=orders, manifests=manifests, tasks=tasks)
    return {
        '状态': '已下发',
        '方案版本': plan_version,
        '检修单号': order_id if created else None,
        '新建任务数': len(created),
        '审批状态': ORDER_PENDING,
        '任务状态改为已排程': moved,
        '作废的旧待审批单任务数': len(set(voided)),
        '放回待安排': released,
        '跳过': skipped,
        '明细': created,
        '操作人': operator,
        '说明': (
            f"已生成待审批检修单 {len(created)} 行、出海清单 {len(created)} 条；"
            "任务状态已从待安排改为已排程，下一轮规划不会再重复排它们。"
            "审批通过后才会锁定为固定占用。"
        ),
    }


# --------------------------------------------------------------------------- 审批


def approve(
    root: Path | None,
    *,
    order_id: str,
    decision: bool,
    approver: str = '审批员',
) -> dict:
    """审批整张检修单。拒绝时把任务放回待安排，不留悬挂状态。"""
    paths = _paths(root)
    orders = read_rows(paths['orders'], ORDER_FIELDS)
    rows = [row for row in orders if row['检修单号'] == order_id]
    if not rows:
        raise ValueError(f'检修单 {order_id} 不存在')
    current = rows[0]['审批状态']
    if current != ORDER_PENDING:
        raise ValueError(f'检修单 {order_id} 当前状态为「{current}」，只有待审批的单可以审批')

    state = ORDER_APPROVED if decision else ORDER_REJECTED
    for row in rows:
        row['审批状态'] = state

    tasks = _task_rows(root)
    changed = 0
    if decision:
        for row in rows:
            if _set_task_state(tasks, row['任务编号'], TASK_SCHEDULED):
                changed += 1
    else:
        for row in rows:
            if _set_task_state(tasks, row['任务编号'], '待安排', only_from=(TASK_SCHEDULED,)):
                changed += 1

    _save(root, orders=orders, tasks=tasks)
    return {
        '检修单号': order_id,
        '审批状态': state,
        '任务数': len(rows),
        '影响任务数': changed,
        '审批人': approver,
        '说明': '已批准：这些任务下一轮规划作为固定占用，不会再被挪动。'
                if decision else '已拒绝：任务已放回待安排，可以重新规划。',
    }


# --------------------------------------------------------------------------- 反馈


def record_feedback(
    root: Path | None,
    *,
    order_id: str,
    actual_start: str,
    actual_finish: str,
    consumed: int = 1,
    returned: int = 0,
    resolved: bool = True,
    operator: str = '现场执行',
) -> dict:
    """回填现场执行结果。故障消除的任务在这一步才真正关闭、退出待安排池。"""
    paths = _paths(root)
    orders = read_rows(paths['orders'], ORDER_FIELDS)
    rows = [row for row in orders if row['检修单号'] == order_id]
    if not rows:
        raise ValueError(f'检修单 {order_id} 不存在')
    if rows[0]['审批状态'] != ORDER_APPROVED:
        raise ValueError(f'检修单 {order_id} 尚未批准，不能登记执行反馈')

    feedback = read_rows(paths['feedback'], FEEDBACK_FIELDS)
    if any(row['检修单号'] == order_id for row in feedback):
        raise ValueError(f'检修单 {order_id} 已经登记过执行反馈，不能重复回填')

    record = {
        '反馈编号': 'FB-' + _short(),
        '检修单号': order_id,
        '实际开工时间': actual_start,
        '实际完工时间': actual_finish,
        '实际消耗数量': str(consumed),
        '退料数量': str(returned),
        '故障是否消除': '是' if resolved else '否',
    }
    feedback.append(record)

    tasks = _task_rows(root)
    closed, reopened = 0, 0
    for row in rows:
        if resolved:
            if _set_task_state(tasks, row['任务编号'], TASK_DONE):
                closed += 1
        else:
            # 故障没消除：任务退回待安排，下一轮重新参与竞争。
            if _set_task_state(tasks, row['任务编号'], '待安排', only_from=(TASK_SCHEDULED,)):
                reopened += 1

    _save(root, feedback=feedback, tasks=tasks)
    return {
        '反馈编号': record['反馈编号'],
        '检修单号': order_id,
        '故障是否消除': record['故障是否消除'],
        '关闭任务数': closed,
        '退回待安排': reopened,
        '操作人': operator,
        '说明': '任务已标记为已完成，从待安排池中移除。'
                if resolved else '故障未消除，任务已退回待安排，需要重新规划。',
    }


# --------------------------------------------------------------------------- 台账


def board(root: Path | None = None) -> dict:
    """台账总览：一眼看清多少任务在等安排、多少在计划里、多少已完工。"""
    tasks = _task_rows(root)
    orders = read_rows(_paths(root)['orders'], ORDER_FIELDS)
    manifests = read_rows(_paths(root)['manifests'], MANIFEST_FIELDS)
    feedback = read_rows(_paths(root)['feedback'], FEEDBACK_FIELDS)

    by_task_state: dict[str, int] = {}
    for row in tasks:
        state = row.get('任务状态') or '未记录'
        by_task_state[state] = by_task_state.get(state, 0) + 1
    by_order_state: dict[str, int] = {}
    for row in orders:
        state = row.get('审批状态') or '未记录'
        by_order_state[state] = by_order_state.get(state, 0) + 1

    pending = sum(count for state, count in by_task_state.items() if state in TASK_PENDING)
    scheduled = by_task_state.get(TASK_SCHEDULED, 0)
    done = by_task_state.get(TASK_DONE, 0)
    return {
        '任务总数': len(tasks),
        '待安排': pending,
        '已排程': scheduled,
        '已完成': done,
        '已取消': by_task_state.get(TASK_CANCELLED, 0),
        '按任务状态': by_task_state,
        '检修单行数': len(orders),
        '检修单数': len({row['检修单号'] for row in orders}),
        '按审批状态': by_order_state,
        '出海清单条数': len(manifests),
        '执行反馈条数': len(feedback),
    }


def summary_line(root: Path | None = None) -> str:
    """给对话回复用的一行人话。"""
    data = board(root)
    if data['检修单行数'] == 0:
        return (
            f"当前台账：待安排 {data['待安排']} 项，"
            "还没有下发过检修单——排完要下发才会生成单子和出海清单。"
        )
    return (
        f"当前台账：待安排 {data['待安排']} 项、已排程 {data['已排程']} 项、"
        f"已完成 {data['已完成']} 项；共 {data['检修单数']} 张检修单"
        f"（{data['检修单行数']} 行）、出海清单 {data['出海清单条数']} 条、"
        f"执行反馈 {data['执行反馈条数']} 条。"
    )
