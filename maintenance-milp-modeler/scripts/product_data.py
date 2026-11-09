"""Versioned input snapshot shared by optimization, baselines and order approval."""
import copy
import csv
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from planning_horizon import PlanningHorizonError, select_target_window

TZ = timezone(timedelta(hours=8))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve_planning_as_of(explicit, base, days, nature=None):
    """决定本次规划的时点，返回 (时点字符串, 来源标注)。

    优先级：显式指定 > 环境变量 PLANNING_AS_OF > 当前时刻。
    规划时点必须落在数据覆盖期内，越界就夹到边界并如实标注来源——
    这样"今天"能自然驱动告警累积和窗口推进，同时验收测试仍可钉住某一天复现。
    """
    import os

    limit_end = base + timedelta(days=days)
    if explicit:
        moment, source = datetime.fromisoformat(explicit), '指定时点'
    else:
        pinned = os.environ.get('PLANNING_AS_OF')
        if pinned:
            moment, source = datetime.fromisoformat(pinned), '环境变量钉住'
        else:
            moment, source = datetime.now(TZ), '当前时刻'
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    if moment < base:
        return (base + timedelta(hours=18)).isoformat(), f'{source}早于数据覆盖期，已取覆盖期起点'
    if moment >= limit_end:
        return (limit_end - timedelta(hours=6)).isoformat(), f'{source}晚于数据覆盖期，已取覆盖期终点'
    return moment.isoformat(), source


def finite(value, label, minimum=None):
    if isinstance(value, bool):
        raise ValueError(f'{label}不能为布尔值')
    n = float(value)
    if not math.isfinite(n) or (minimum is not None and n < minimum):
        raise ValueError(f'{label}取值无效：{value}')
    return n


@dataclass
class Snapshot:
    base: datetime
    end: int
    now: int
    params: dict
    parts: dict
    tasks: list
    windows: list
    weather: list
    hourly: list
    stocks: dict
    movements: list
    alpha: dict
    meta: dict
    observation_windows: list = field(default_factory=list)
    window_selection: dict = field(default_factory=dict)
    # 部件在此分钟之前不可用（自然语言“到货前不能开工”登记在这里）
    part_freeze: dict = field(default_factory=dict)
    run_mode: str = 'HYPOTHETICAL'
    fixed: list = field(default_factory=list)
    issued: list = field(default_factory=list)
    # 已批准检修单锁定的任务：{任务编号: {开始, 窗口编号, 作业线}}。
    # 时间和作业线都不能再被新一轮规划挪动，只登记不参与自由竞争。
    locked: dict = field(default_factory=dict)
    # 下面四项是 build_model() 每次求解现算出来的「计价上下文」，不是用户输入。
    # 必须声明成正式字段：Store.save_run 存的是 data.__dict__，审批回放要
    # Snapshot(**snap) 还原；偷偷挂动态属性会让回放直接 TypeError。
    # 每次 build_model 都会重算，旧快照里带的值只作审计，不参与下一轮决策。
    pricing_window: tuple | None = None
    penalty_records: list = field(default_factory=list)
    penalty_total: float = 0.0
    effective_weights: dict = field(default_factory=dict)

    def minute(self, value):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError('时间必须包含时区')
        return int((dt - self.base).total_seconds() / 60)

    def stamp(self, value):
        return (self.base + timedelta(minutes=value)).isoformat()

    def revenue(self, start, end):
        total = 0.0
        for h in range(max(0, start // 60), min(len(self.hourly), (end + 59) // 60)):
            overlap = max(0, min(end, (h+1)*60) - max(start, h*60)) / 60
            r = self.hourly[h]
            total += overlap * r['功率'] * r['电价']
        return total

    def availability(self, part, at):
        return self.stocks[part] + sum(m['数量'] * (1 if m['方向']=='补库' else -1)
            for m in self.movements if m['部件编号']==part and m['时间'] <= at)


OVERRIDES = {'船租（元每艘每天）', '人工单价（元每人时）', '最多作业线数', '人员总上限',
             '每天最多维修小时数', '风机间转移小时数', '每天维修开始时间', '每天维修结束时间',
             # 生成层规则参数（第三期）：改了需要重新生成候选
             '时间步长（小时）', '候选开工粒度',
             '常态最大风速（米每秒）', '常态最大有效波高（米）',
             '安全最大风速（米每秒）', '安全最大有效波高（米）',
             '跨天暂停', '允许夜间转移'}

# 注册表/自然语言口径 → 系统参数表口径
PARAM_ALIASES = {
    '常态风速上限': '常态最大风速（米每秒）',
    '常态浪高上限': '常态最大有效波高（米）',
    '安全风速上限': '安全最大风速（米每秒）',
    '安全浪高上限': '安全最大有效波高（米）',
    '允许跨天暂停': '跨天暂停',
}
_YES_VALUES = {'是', '允许', '允许并保留进度', True}
_NO_VALUES = {'否', '不允许', '禁止', False}


def _patched_business(business, patches, base, end):
    """把行级数据补丁写进业务数据副本。

    改的是业务记录行（部件工时、库存到货流水），不是全局参数；
    改完之后原有的类型、范围、库存校验会原样作用在新值上。
    """
    if not patches:
        return business, {}
    business = copy.deepcopy(business)
    block_frozen: dict[str, int] = {}
    for patch in patches:
        kind = patch.get('kind')
        if kind == 'part_hours':
            rows = [r for r in business['部件库'] if r['部件编号'] == patch['partId']]
            if not rows:
                raise ValueError(f"部件{patch['partId']}不存在于部件库")
            # 定点范围：severity 管严重度这一维，sea 管海况这一维。
            # 不给就是该维全改（factor 的老口径：整件 × 三级两况）。
            levels = (patch['severity'],) if patch.get('severity') else ('高', '中', '低')
            seas = (patch['sea'],) if patch.get('sea') else ('常态', '恶劣但安全')
            for level in levels:
                if level not in ('高', '中', '低'):
                    raise ValueError(f"严重度 {level!r} 只能是 高/中/低")
            for sea in seas:
                if sea not in ('常态', '恶劣但安全'):
                    raise ValueError(f"海况 {sea!r} 只能是 常态/恶劣但安全")
            keys = [f'{level}严重度{sea}维修工时（小时）' for level in levels for sea in seas]
            for row in rows:
                missing = [k for k in keys if k not in row]
                if missing:
                    raise ValueError(f"部件库缺少工时刻：{missing}")

            if 'hours' in patch:
                # 绝对值：把命中的格子直接定成 N 小时。
                hours = float(patch['hours'])
                if hours <= 0:
                    raise ValueError(f"部件{patch['partId']}的维修工时必须为正")
                for row in rows:
                    for key in keys:
                        row[key] = round(hours, 6)
            else:
                factor = float(patch['factor'])
                if factor <= 0:
                    raise ValueError(f"部件{patch['partId']}的工时倍率必须为正")
                for row in rows:
                    for key in keys:
                        row[key] = round(float(row[key]) * factor, 6)
        elif kind == 'part_arrival':
            moment = datetime.fromisoformat(patch['at'])
            minute = int((moment - base).total_seconds() / 60)
            if not 0 < minute <= end:
                raise ValueError(
                    f"到货时间 {moment.date().isoformat()} 不在本次规划范围内，"
                    "这次改了也不会影响结果；请换一个规划期内的日期。"
                )
            business['库存收发计划'].append({
                '流水编号': f"补库-{patch['partId']}-{moment.strftime('%Y%m%d')}",
                '部件编号': patch['partId'],
                '收发方向': '补库',
                '数量': int(patch['quantity']),
                '计划时间': moment.isoformat(),
                '流水状态': '已确认计划',
            })
            if patch.get('blockBefore'):
                # 「到货前不能开工」不靠冲账流水表达——那会和已有的出库计划打架算出负库存；
                # 改为直接登记“该部件在此刻之前不可用”，由求解器在读可用量时生效。
                block_frozen[patch['partId']] = max(
                    block_frozen.get(patch['partId'], 0), minute
                )
    return business, block_frozen


def _apply_task_patches(tasks, patches, base):
    """任务期限补丁：只做收紧，收紧不了就明确报错，不静默忽略。"""
    for patch in patches:
        if patch.get('kind') != 'task_deadline':
            continue
        minute = int((datetime.fromisoformat(patch['at']) - base).total_seconds() / 60)
        level = patch.get('severity')
        tightened = []
        for task in tasks:
            if level and task['严重等级'] != level:
                continue
            current = task.get('截止分钟')
            if current is not None and minute >= current:
                continue
            task['截止分钟'] = minute
            task['截止来源'] = '自然语言期限'
            tightened.append(task['任务编号'])
        if not tightened:
            scope = f"{level}严重任务" if level else '全部任务'
            raise ValueError(
                f"这次给的期限没有让任何{scope}的完工时间提前，"
                "改了也不会影响结果；请给一个更早的日期。"
            )


def _apply_timeline_patches(data, patches, cfg):
    """时序/结构数据的假设修改（第二期）：海况/逐时/窗口，只改快照不改源文件。

    weather：按日设风速/波高（联动重算海况类别与可用小时）或直接设海况类别；
    hourly：按时设电价/风速（联动重算功率）或直接设功率。

    窗口起止不接受补丁：那是已批准海事窗口的客观事实（带审批状态、受窗口规则
    校验），不是可以由对话改写的假设。要约束开工或收工，请改任务级的
    earliest / 截止分钟；候选上界本来就是 min(窗口结束, 任务截止)。
    """
    if not patches:
        return
    threshold = cfg['基座生成参数']
    curve = cfg['功率曲线']
    vi = finite(curve['切入风速米每秒'], '切入风速', 0)
    vr = finite(curve['额定风速米每秒'], '额定风速', 0)
    vo = finite(curve['切出风速米每秒'], '切出风速', 0)
    rated = finite(curve['额定功率兆瓦'], '额定功率', 0)
    for patch in patches:
        kind = patch.get('kind')
        if kind == 'weather':
            target = str(patch['day'])
            row = next(
                (r for i, r in enumerate(data.weather) if data.stamp(i * 1440).startswith(target)),
                None,
            )
            if row is None:
                raise ValueError(f'海况补丁日期 {target} 不在规划期内')
            wind = patch.get('wind')
            wave = patch.get('wave')
            hours = patch.get('hours')
            state = patch.get('state')
            if wind is not None or wave is not None:
                wind = finite(wind if wind is not None else 0, '风速', 0)
                wave = finite(wave if wave is not None else 0, '波高', 0)
                state = ('不可作业' if wind > threshold['不可作业风速阈值'] or wave > threshold['不可作业波高阈值']
                         else '恶劣但安全' if wind > threshold['恶劣海况风速阈值'] or wave > threshold['恶劣海况波高阈值']
                         else '常态')
            if state is not None:
                if state not in ('常态', '恶劣但安全', '不可作业'):
                    raise ValueError(f'海况类别 {state} 不合法')
                row['海况类别'] = state
                base_hours = float(hours) if hours is not None else row['可用小时']
                row['可用小时'] = 0 if state == '不可作业' else min(
                    finite(base_hours, '可用小时', 0),
                    data.params['每天最多维修小时数'],
                )
        elif kind == 'hourly':
            hour_index = data.minute(str(patch['time'])) // 60
            if not 0 <= hour_index < len(data.hourly):
                raise ValueError(f'逐时补丁时刻 {patch["time"]} 不在规划期内')
            row = data.hourly[hour_index]
            if 'price' in patch:
                row['电价'] = finite(patch['price'], '电价', 0)
            if 'wind' in patch:
                wind = finite(patch['wind'], '风速', 0)
                row['风速'] = wind
                row['功率'] = (0 if wind < vi or wind >= vo
                               else rated if wind >= vr
                               else rated * (wind**3 - vi**3) / (vr**3 - vi**3))
            if 'power' in patch:
                row['功率'] = finite(patch['power'], '功率', 0)
        else:
            raise ValueError(f'未知时序补丁类型 {kind}')


def load_project(project, base_id='基座01', as_of=None, overrides=None, input_file=None, run_mode=None, data_patches=None, timeline_patches=None):
    project = Path(project).resolve()
    source = project / 'data/phase1-demo/初版完整数据.json'
    raw = read_json(input_file or source)
    # 台账（检修单／出海清单／执行反馈）跟业务数据放在同一目录，真实数据包也一样。
    ledger_dir = Path(input_file).parent if input_file else source.parent
    patches = [dict(p) for p in (data_patches or [])]
    if input_file:
        # Real adapters use precisely this envelope; never silently mix in synthetic weather.
        if set(raw) != {'schema_version', '业务数据', '基座', '元数据'} or raw['schema_version'] != '1.0':
            raise ValueError('输入包需schema_version=1.0、业务数据、基座、元数据四个字段')
        meta = raw['元数据']
        if meta.get('数据性质') not in ('真实', '合成') or not meta.get('来源'):
            raise ValueError('输入需明确数据性质与来源')
        foundation, business = raw['基座'], raw['业务数据']
        cfg = foundation['配置']
        window_rows = foundation['窗口']
        weather_rows = foundation['海况']
        hour_rows = foundation['逐时预测']
    else:
        if base_id not in [f'基座{i:02d}' for i in range(1, 6)]:
            raise ValueError('未知测试场景')
        directory = project / 'data/场景评测/五组基座数据' / base_id
        cfg = read_json(directory/'场景配置快照.json')
        window_rows = read_csv(directory/'海事检修窗口.csv')
        weather_rows = read_csv(directory/'每日海况预测.csv')
        hour_rows = read_csv(directory/'每小时电价风速功率.csv')
        business = raw
        meta = {'数据性质':'合成', '来源':str(source), '场景':base_id}
    meta = copy.deepcopy(meta)
    meta['输入哈希'] = digest({'业务数据':business, '配置':cfg, '窗口':window_rows,
                              '海况':weather_rows, '逐时预测':hour_rows})
    base = datetime.fromisoformat(cfg['规划开始日期']+'T00:00:00+08:00')
    end = int(cfg['规划天数'])*1440
    # 规划时点跟着“今天”走，数据覆盖期内不再钉死在起点当天。
    as_of, as_of_source = resolve_planning_as_of(as_of, base, int(cfg['规划天数']), meta.get('数据性质'))
    params = {r['参数名称']:r['参数值'] for r in business['系统参数']}
    overrides = {PARAM_ALIASES.get(key, key): value for key, value in (overrides or {}).items()}
    if set(overrides) - OVERRIDES:
        raise ValueError('未支持参数：'+str(set(overrides)-OVERRIDES))
    params.update(overrides)
    # 生成层参数归一化：候选开工粒度（分钟）与时间步长（小时）互为别名
    if '候选开工粒度' in overrides:
        params['时间步长（小时）'] = float(overrides['候选开工粒度']) / 60
    params.setdefault('时间步长（小时）', 1)
    params.setdefault('跨天暂停', '是')
    params.setdefault('允许夜间转移', '是')
    granularity = float(params['时间步长（小时）']) * 60
    if granularity <= 0 or granularity > 1440 or (1440 % granularity != 0):
        raise ValueError('候选开工粒度必须是能整除一天的正数分钟')
    params['候选开工粒度'] = int(granularity)
    for flag in ('跨天暂停', '允许夜间转移'):
        if params[flag] not in _YES_VALUES | _NO_VALUES:
            raise ValueError(f'{flag}必须为是或否或允许/不允许')
    business, block_frozen = _patched_business(business, patches, base, end)
    for name in OVERRIDES - {'每天维修开始时间','每天维修结束时间','跨天暂停','允许夜间转移'}:
        params[name] = finite(params[name], name, 0)
    for name in ('最多作业线数','人员总上限'):
        if not params[name].is_integer() or not 1 <= params[name] <= 100:
            raise ValueError(f'{name}必须为1至100整数')
        params[name] = int(params[name])
    if not 0 < params['每天最多维修小时数'] <= 24:
        raise ValueError('每日维修小时应在0至24以内')
    for name in ('动员费（元）','额外燃油费（元）','等待和转移人工单价（元每人时）'):
        if finite(params.get(name, 0), name, 0) != 0:
            raise ValueError(f'{name}非零尚未绑定计费事件，不能静默忽略')
    parts = {p['部件编号']:copy.deepcopy(p) for p in business['部件库']}
    if len(parts) != len(business['部件库']):
        raise ValueError('部件编号重复')
    for p in parts.values():
        if p['固定维修人数'] != 1 or p['每任务备件数量'] != 1:
            raise ValueError('当前构件定义为一部件一人一套备件')
        for level in ('高','中','低'):
            for sea in ('常态','恶劣但安全'):
                value = finite(p[f'{level}严重度{sea}维修工时（小时）'], '维修时长', 0)
                if value == 0:
                    raise ValueError('维修时长必须为正')
        finite(p['维修包成本（元每套）'], '材料价格', 0)
        finite(p.get('其他费用（元每任务）', 0), '其他费用', 0)
    # 故障未修期间的剩余出力系数 α（决定不修惩罚 P 的大小）。
    # 业务表是唯一权威：部件库里带「X严重度剩余出力系数」三列时按部件逐项读；
    # 旧数据包/外部输入没有这三列时回退到场景配置快照，行为与改造前一致，
    # 但来源会标成「配置回退」，免得把「需现场校准的假设」当成已确认数据。
    alpha = {}
    alpha_source = '部件库业务表'
    for p in parts.values():
        name = p['部件名称']
        record = {}
        for level in ('高','中','低'):
            key = f'{level}严重度剩余出力系数'
            if key in p:
                record[level] = finite(p[key], f'{name}{key}', 0)
        if len(record) == 3:
            alpha[name] = record
    if len(alpha) != len({p['部件名称'] for p in parts.values()}):
        alpha = {k: dict(v) for k, v in cfg['部件故障剩余出力系数'].items()
                 if k != '数值性质' and isinstance(v, dict)}
        alpha_source = '场景配置快照（回退，业务表缺 α 列）'
    horizon = finite(params.get('未修惩罚展望期（天）', 14), '未修惩罚展望期（天）', 0)
    if horizon <= 0:
        raise ValueError('未修惩罚展望期（天）必须为正')
    params['未修惩罚展望期（天）'] = horizon
    meta['剩余出力系数来源'] = alpha_source
    meta['未修惩罚展望期（天）'] = horizon
    run_mode = run_mode or ('PRODUCTION' if meta['数据性质']=='真实' else 'HYPOTHETICAL')
    data = Snapshot(base, end, 0, params, parts, [], [], [], [], {}, [], alpha, meta,
                    run_mode=run_mode)
    data.part_freeze = block_frozen
    data.now = data.minute(as_of)
    if not 0 <= data.now < end:
        raise ValueError('规划时点必须位于预测覆盖期内')
    meta.update({'规划时点':data.stamp(data.now), '时点来源':as_of_source,
                 '覆盖期起':data.stamp(0), '覆盖期止':data.stamp(end),
                 '参数覆盖':overrides, '编译器版本':'3.1', '运行模式':run_mode})
    selector_rows=[]
    for w in window_rows:
        lo = data.minute(w['开始日期']+'T00:00:00+08:00')
        hi = data.minute(w['结束日期（不含）']+'T00:00:00+08:00')
        if not 0 <= lo < hi <= end:
            raise ValueError('窗口超出预测期或端点无效')
        if meta['数据性质']=='真实' and w['审批状态'] != '已批准':
            raise ValueError('真实数据不允许未批准海事窗口')
        parsed={'编号':w['窗口编号'], '开始':lo, '结束':hi, '审批状态':w['审批状态']}
        data.observation_windows.append(parsed)
        selector_rows.append({'编号':w['窗口编号'], '开始时间':data.stamp(lo),
                              '结束时间不含':data.stamp(hi), '审批状态':w['审批状态']})
    data.observation_windows.sort(key=lambda w:(w['开始'],w['编号']))
    if len({w['编号'] for w in data.observation_windows}) != len(data.observation_windows):
        raise ValueError('窗口编号重复')
    rules = cfg['窗口规则']
    for i, w in enumerate(data.observation_windows):
        days = (w['结束']-w['开始'])/1440
        if not rules['最短天数'] <= days <= rules['最长天数']:
            raise ValueError('窗口长度不符规则')
        if i and w['开始']-data.observation_windows[i-1]['结束'] < rules['窗口间最少间隔天数']*1440:
            raise ValueError('窗口间隔不足')
    selection=select_target_window(data.stamp(data.now),selector_rows,run_mode=run_mode)
    if not selection.target_window:
        raise PlanningHorizonError(selection.status,'90天观察范围内没有满足七天准备期的完整可用窗口')
    target_id=selection.target_window['编号']
    target=next(w for w in data.observation_windows if w['编号']==target_id)
    data.windows=[copy.deepcopy(target)]
    data.window_selection={
        '状态':selection.status,
        '规划边界':selection.planning_boundary.isoformat(),
        '观察终点':selection.observation_end.isoformat(),
        '目标窗口编号':target_id,
        '跳过窗口':selection.skipped_windows,
    }
    meta.update({'规划边界':selection.planning_boundary.isoformat(),
                 '观察终点':selection.observation_end.isoformat(),
                 '目标窗口编号':target_id,
                 '窗口选择':copy.deepcopy(data.window_selection)})
    if len(weather_rows) != end//1440 or len(hour_rows) != end//60:
        raise ValueError('天气或逐时预测覆盖不完整')
    threshold = cfg['基座生成参数']
    # 海况判定阈值参数化（第三期）：显式覆盖时用系统参数，否则沿用基座生成参数（零回归）
    weather_override = any(
        key in overrides for key in ('常态最大风速（米每秒）', '常态最大有效波高（米）',
                                     '安全最大风速（米每秒）', '安全最大有效波高（米）')
    )
    if weather_override:
        wind_stop = float(params['安全最大风速（米每秒）'])
        wave_stop = float(params['安全最大有效波高（米）'])
        wind_harsh = float(params['常态最大风速（米每秒）'])
        wave_harsh = float(params['常态最大有效波高（米）'])
    else:
        wind_stop = threshold['不可作业风速阈值']
        wave_stop = threshold['不可作业波高阈值']
        wind_harsh = threshold['恶劣海况风速阈值']
        wave_harsh = threshold['恶劣海况波高阈值']
    for day, r in enumerate(weather_rows):
        if r['日期'] != (base+timedelta(days=day)).date().isoformat():
            raise ValueError('每日预测时间不连续或重复')
        wind = finite(r['作业环境风速（米每秒）'], '环境风速', 0)
        wave = finite(r['有效波高（米）'], '波高', 0)
        state = ('不可作业' if wind > wind_stop or wave > wave_stop
                 else '恶劣但安全' if wind > wind_harsh or wave > wave_harsh else '常态')
        data.weather.append({'海况类别':state, '可用小时':min(float(r['允许维修小时数（小时）']),params['每天最多维修小时数']) if state!='不可作业' else 0})
    curve = cfg['功率曲线']
    vi, vr, vo, rated = [finite(curve[k], k, 0) for k in ('切入风速米每秒','额定风速米每秒','切出风速米每秒','额定功率兆瓦')]
    if not 0 < vi < vr < vo or rated <= 0:
        raise ValueError('功率曲线端点无效')
    for h, r in enumerate(hour_rows):
        if data.minute(r['时间']) != h*60:
            raise ValueError('逐时预测时间不连续或重复')
        wind = finite(r['轮毂高度风速（米每秒）'], '轮毂风速', 0)
        power = 0 if wind < vi or wind >= vo else rated if wind >= vr else rated*(wind**3-vi**3)/(vr**3-vi**3)
        data.hourly.append({'功率':power,'电价':finite(r['预测电价（元每兆瓦时）'], '电价'),'风速':wind})
    for p in parts.values():
        for level in ('高','中','低'):
            a = finite(data.alpha[p['部件名称']][level], '剩余出力系数', 0)
            if a > 1:
                raise ValueError('剩余出力系数应在0至1')
    stocks = {r['部件编号']:r for r in business['库存快照']}
    if set(stocks)!=set(parts):
        raise ValueError('每种部件均需库存快照')
    data.stocks = {p:int(finite(r['现有库存数量'], '库存', 0)) for p,r in stocks.items()}
    seen_movements = set()
    for m in business['库存收发计划']:
        if m['流水编号'] in seen_movements:
            raise ValueError('库存流水重复')
        seen_movements.add(m['流水编号'])
        if m['流水状态'] != '已确认计划':
            raise ValueError('库存流水需先对账为确认计划，不能混入不明状态')
        if m['收发方向'] not in ('出库','补库') or m['部件编号'] not in parts:
            raise ValueError('库存流水方向或部件无效')
        if data.minute(m['计划时间']) <= data.minute(stocks[m['部件编号']]['库存统计时间']):
            raise ValueError('快照之前流水可能已入账，必须先对账')
        qty = finite(m['数量'], '流水数量', 0)
        if not qty.is_integer():
            raise ValueError('库存量必须为整数')
        data.movements.append({'编号':m['流水编号'],'部件编号':m['部件编号'],'方向':m['收发方向'],'数量':int(qty),'时间':data.minute(m['计划时间'])})
    for p in parts:
        for t in {0,end} | {m['时间'] for m in data.movements}:
            if data.availability(p,t)<0:
                raise ValueError('已确认库存流水自身出现负库存：'+p)
    seen_tasks = set()
    # 已下发的检修单：时间和作业线已经写进单子对外承诺，新一轮规划不能再挪。
    # 待审批也算锁定——单子已经生成了，要改就先拒绝或作废，不让规划静默改掉。
    # 输入包和台账 CSV 都读一遍：真实数据包自包含，演示数据两边同步写。
    locked_orders = {}
    ledger_rows = list(business.get('检修单') or [])
    ledger = ledger_dir / '检修单.csv'
    if ledger.exists():
        ledger_rows += read_csv(ledger)
    for row in ledger_rows:
        if row.get('审批状态') in ('待审批', '已批准') and row.get('任务编号'):
            locked_orders[row['任务编号']] = row
    for task in business['告警任务']:
        if task['任务编号'] in seen_tasks:
            raise ValueError('重复任务需通过告警更新接口合并')
        seen_tasks.add(task['任务编号'])
        if data.minute(task['首次规划时间']) > data.now or data.minute(task['发生时间']) > data.now:
            continue
        if task['任务状态'] in ('已完成','已取消'):
            continue
        if task['部件编号'] not in parts or task['严重等级'] not in ('高','中','低'):
            raise ValueError('未知告警部件或严重等级')
        t = copy.deepcopy(task)
        occurrence = data.minute(t['发生时间'])
        planning_boundary=data.minute(selection.planning_boundary.isoformat())
        observation_end=data.minute(selection.observation_end.isoformat())
        eligible = [w for w in data.observation_windows
                    if w['开始'] >= planning_boundary and w['结束'] <= observation_end and w['结束'] > occurrence]
        index = {'高':0, '中':1, '低':2}[t['严重等级']]
        t['截止分钟'] = eligible[index]['结束'] if len(eligible)>index else None
        t['截止来源窗口编号'] = eligible[index]['编号'] if len(eligible)>index else None
        t['发生分钟'] = occurrence
        t['接收分钟'] = data.minute(t['首次规划时间'])
        t['剩余出力'] = data.alpha[parts[t['部件编号']]['部件名称']][t['严重等级']]
        order = locked_orders.get(t['任务编号'])
        if order:
            # 已排程的任务照样进模型——它仍然占着作业线、要备件、有发电损失，
            # 只是开始时间和作业线不能改。剔掉它会让新一轮排程假装资源是空的。
            t['锁定'] = {'开始': data.minute(order['计划开工时间']),
                          '窗口编号': order.get('窗口编号') or '',
                          '作业线': int(float(order.get('作业线编号') or 1))}
            data.locked[t['任务编号']] = t['锁定']
        data.tasks.append(t)
    _apply_task_patches(data.tasks, patches, base)
    _apply_timeline_patches(data, timeline_patches or [], cfg)
    meta['告警水位线'] = max((data.stamp(t['接收分钟']) for t in data.tasks),default=data.stamp(data.now))
    data.end=target['结束']
    meta['运行哈希'] = digest({'输入哈希':meta['输入哈希'],'时点':data.now,'覆盖':overrides,
                              '运行模式':run_mode,'目标窗口':target,'告警水位线':meta['告警水位线'],
                              '数据补丁':sorted(patches, key=lambda p: json.dumps(p, ensure_ascii=False, sort_keys=True))})
    return data
