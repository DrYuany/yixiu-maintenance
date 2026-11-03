"""从显式中文配置生成五组可复现的合成检修窗口、电价、海况和风速基座。"""
import argparse
import csv
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


ROOT = Path.cwd()
DEFAULT_CONFIG = ROOT / 'data/场景评测/五组基座配置.json'
DEFAULT_OUTPUT = ROOT / 'data/场景评测/五组基座数据'
UTC_PLUS_8 = timezone(timedelta(hours=8))


def turbine_power(wind_speed, curve):
    rated = curve['额定功率兆瓦']
    cut_in = curve['切入风速米每秒']
    rated_speed = curve['额定风速米每秒']
    cut_out = curve['切出风速米每秒']
    if wind_speed < cut_in or wind_speed >= cut_out:
        return 0.0
    if wind_speed >= rated_speed:
        return rated
    return rated * (wind_speed ** 3 - cut_in ** 3) / (rated_speed ** 3 - cut_in ** 3)


def validate_windows(windows, rules):
    previous_end = None
    for index, window in enumerate(windows, 1):
        start = date.fromisoformat(window['开始'])
        end = date.fromisoformat(window['结束不含'])
        days = (end - start).days
        if not rules['最短天数'] <= days <= rules['最长天数']:
            raise ValueError(f'窗口{index}长度{days}天超出配置边界')
        if previous_end is not None and (start - previous_end).days < rules['窗口间最少间隔天数']:
            raise ValueError(f'窗口{index}与前一窗口间隔不足')
        previous_end = end


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(config_path=DEFAULT_CONFIG, output_dir=DEFAULT_OUTPUT):
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    start_date = date.fromisoformat(config['规划开始日期'])
    horizon_days = config['规划天数']
    horizon_end = start_date + timedelta(days=horizon_days)
    as_of = datetime.combine(start_date, time(18), UTC_PLUS_8)
    observation_end = as_of + timedelta(days=90)
    if datetime.combine(horizon_end, time.min, UTC_PLUS_8) < observation_end:
        raise ValueError('底层预测必须完整覆盖规划时点之后90×24小时')
    profile = config['基座生成参数']
    hashes = {}
    for scenario in config['场景']:
        validate_windows(scenario['窗口'], config['窗口规则'])
        eligible = [
            window for window in scenario['窗口']
            if datetime.combine(date.fromisoformat(window['开始']), time.min, UTC_PLUS_8) >= as_of + timedelta(days=7)
            and datetime.combine(date.fromisoformat(window['结束不含']), time.min, UTC_PLUS_8) <= observation_end
        ]
        if not eligible:
            raise ValueError(f"{scenario['场景编号']}在七天准备边界后没有完整可用窗口")
        folder = Path(output_dir) / scenario['场景编号']
        window_rows = []
        previous_end = None
        for index, window in enumerate(scenario['窗口'], 1):
            window_start = date.fromisoformat(window['开始'])
            window_end = date.fromisoformat(window['结束不含'])
            if window_start < start_date or window_end > horizon_end:
                raise ValueError(f"{scenario['场景编号']}窗口超出{horizon_days}天规划范围")
            window_rows.append({
                '窗口编号': f'窗口{index:02d}', '开始日期': window_start.isoformat(),
                '结束日期（不含）': window_end.isoformat(), '窗口天数': (window_end-window_start).days,
                '与上一窗口间隔天数': 0 if previous_end is None else (window_start-previous_end).days,
                '审批状态': '合成假设，未获真实审批',
                '发布时间': (as_of-timedelta(days=1)).isoformat(),
                '数据来源': '合成场景生成器'
            })
            previous_end = window_end
        scenario_hashes = {}
        scenario_hashes['海事检修窗口.csv'] = write_csv(
            folder/'海事检修窗口.csv', list(window_rows[0]), window_rows)

        daily_rows = []
        hourly_rows = []
        for day_index in range(horizon_days):
            day = start_date + timedelta(days=day_index)
            roughness = scenario['海况恶化倍率']
            marine_wind = (profile['海况风速基准'] +
                           profile['海况风速周期振幅']*math.sin(day_index*0.73+0.4) +
                           profile['海况风速慢变振幅']*math.cos(day_index*0.19))
            wave = (profile['有效波高基准'] +
                    profile['有效波高周期振幅']*(1+math.sin(day_index*0.91+1.1)) +
                    profile['有效波高慢变振幅']*(1+math.cos(day_index*0.33))) * roughness
            marine_wind = round(max(2.5, marine_wind*math.sqrt(roughness)), 2)
            wave = round(wave, 2)
            if marine_wind > profile['不可作业风速阈值'] or wave > profile['不可作业波高阈值']:
                sea_state, repair_hours = '超过安全阈值', 0
            elif marine_wind > profile['恶劣海况风速阈值'] or wave > profile['恶劣海况波高阈值']:
                sea_state, repair_hours = '恶劣但安全', profile['允许维修小时数']
            else:
                sea_state, repair_hours = '常态', profile['允许维修小时数']
            daily_rows.append({
                '日期': day.isoformat(), '作业环境风速（米每秒）': marine_wind,
                '有效波高（米）': wave, '海况类别': sea_state,
                '允许维修小时数（小时）': repair_hours, '数据性质': '合成测试数据'
            })
            for hour in range(24):
                timestamp = datetime.combine(day, time(hour), UTC_PLUS_8)
                hub_wind = (profile['轮毂风速基准'] +
                            profile['轮毂风速日周期振幅']*math.sin(day_index*0.37+0.8) +
                            profile['轮毂风速小时周期振幅']*math.sin(hour*math.pi/12-0.9) +
                            profile['轮毂风速扰动振幅']*math.cos(day_index*0.13+hour*0.21)) * scenario['风速倍率']
                hub_wind = round(max(0.0, hub_wind), 2)
                daily_price = profile['电价基准'] + profile['电价日周期振幅']*math.sin(day_index*0.44+0.3)
                hourly_price = daily_price + profile['电价小时周期振幅']*math.sin(hour*math.pi/12-1.3)
                if scenario['电价波动倍率'] > 1:
                    spike = profile['电价波动扰动振幅']*math.sin(day_index*1.71+hour*0.62)
                    hourly_price += (scenario['电价波动倍率']-1)*spike
                hourly_price = round(max(profile['电价下限'], hourly_price*scenario['电价倍率']), 2)
                power = round(turbine_power(hub_wind, config['功率曲线']), 4)
                hourly_rows.append({
                    '时间': timestamp.isoformat(), '预测电价（元每兆瓦时）': hourly_price,
                    '轮毂高度风速（米每秒）': hub_wind, '曲线计算出力（兆瓦）': power,
                    '小时潜在发电量（兆瓦时）': round(power, 4),
                    '满停机损失金额（元）': round(power*hourly_price, 2),
                    '数据性质': '合成测试数据'
                })
        scenario_hashes['每日海况预测.csv'] = write_csv(
            folder/'每日海况预测.csv', list(daily_rows[0]), daily_rows)
        scenario_hashes['每小时电价风速功率.csv'] = write_csv(
            folder/'每小时电价风速功率.csv', list(hourly_rows[0]), hourly_rows)
        snapshot = {
            '数据版本': config['数据版本'], '数据性质': config['数据性质'],
            '规划开始日期': config['规划开始日期'], '规划天数': horizon_days,
            '功率曲线': config['功率曲线'], '基座生成参数': profile,
            '部件故障剩余出力系数': config['部件故障剩余出力系数'],
            '窗口规则': config['窗口规则'], '场景': scenario
        }
        snapshot_path = folder/'场景配置快照.json'
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        scenario_hashes['场景配置快照.json'] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        hashes[scenario['场景编号']] = scenario_hashes
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    hashes = generate(args.config, args.output)
    summary = {'配置文件': str(args.config), '输出目录': str(args.output),
               '场景数量': len(hashes), '每场景数据文件数': 4, '文件哈希': hashes}
    (args.output/'基座生成摘要.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'场景数量': len(hashes), '每日海况行数': config['规划天数'], '每小时数据行数': config['规划天数']*24,
                      '输出目录': str(args.output)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
