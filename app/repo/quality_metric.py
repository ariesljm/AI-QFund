"""质量度量结果 seam：quality_metrics 表读写。
"""

import json as _json

from app.database import db_conn


def get_quality_metrics(limit: int=6) -> list[dict]:
    """读取最近 N 次质量度量（新→旧），含累计超额曲线点。"""
    with db_conn() as conn:
        rows = conn.execute('SELECT computed_date, period_start, period_end, ic, excess_win_rate, mean_excess, cum_excess, profit_rate, mean_abs_ret, payoff_ratio, sample_count, decision_loss, decision_gap_best, points_json FROM quality_metrics ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    out = []
    for r in rows:
        points = []
        if r[13]:
            try:
                points = _json.loads(r[13])
            except Exception:
                points = []
        out.append({'computed_date': r[0], 'period_start': r[1], 'period_end': r[2], 'ic': r[3], 'excess_win_rate': r[4], 'mean_excess': r[5], 'cum_excess': r[6], 'profit_rate': r[7], 'mean_abs_ret': r[8], 'payoff_ratio': r[9], 'sample_count': r[10], 'decision_loss': r[11], 'decision_gap_best': r[12], 'points': points})
    return out


def save_quality_metrics(m: dict) -> None:
    """保存一次质量度量结果（同区间幂等：重复运行覆盖）。"""
    points_json = _json.dumps(m.get('points', []), ensure_ascii=False)
    by_path_json = _json.dumps(m.get('by_path', {}), ensure_ascii=False)
    by_score_bucket_json = _json.dumps(m.get('by_score_bucket', {}), ensure_ascii=False)
    e2e_points_json = _json.dumps(m.get('e2e_points', []), ensure_ascii=False)
    with db_conn() as conn:
        conn.execute('INSERT INTO quality_metrics (computed_date, period_start, period_end, ic, excess_win_rate, mean_excess, cum_excess, profit_rate, mean_abs_ret, payoff_ratio, sample_count, decision_loss, decision_gap_best, points_json, by_path_json, by_score_bucket_json, e2e_profit_rate, e2e_mean_ret, e2e_payoff_ratio, timing_contribution, e2e_sample_count, e2e_points_json, e2e_mean_hold_days, e2e_mean_max_drawdown) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(period_start, period_end) DO UPDATE SET computed_date = excluded.computed_date, ic = excluded.ic, excess_win_rate = excluded.excess_win_rate, mean_excess = excluded.mean_excess, cum_excess = excluded.cum_excess, profit_rate = excluded.profit_rate, mean_abs_ret = excluded.mean_abs_ret, payoff_ratio = excluded.payoff_ratio, sample_count = excluded.sample_count, decision_loss = excluded.decision_loss, decision_gap_best = excluded.decision_gap_best, points_json = excluded.points_json, by_path_json = excluded.by_path_json, by_score_bucket_json = excluded.by_score_bucket_json, e2e_profit_rate = excluded.e2e_profit_rate, e2e_mean_ret = excluded.e2e_mean_ret, e2e_payoff_ratio = excluded.e2e_payoff_ratio, timing_contribution = excluded.timing_contribution, e2e_sample_count = excluded.e2e_sample_count, e2e_points_json = excluded.e2e_points_json, e2e_mean_hold_days = excluded.e2e_mean_hold_days, e2e_mean_max_drawdown = excluded.e2e_mean_max_drawdown', (m['computed_date'], m.get('period_start'), m.get('period_end'), m.get('ic'), m.get('excess_win_rate'), m.get('mean_excess'), m.get('cum_excess'), m.get('profit_rate'), m.get('mean_abs_ret'), m.get('payoff_ratio'), m.get('sample_count', 0), m.get('decision_loss'), m.get('decision_gap_best'), points_json, by_path_json, by_score_bucket_json, m.get('e2e_profit_rate'), m.get('e2e_mean_ret'), m.get('e2e_payoff_ratio'), m.get('timing_contribution'), m.get('e2e_sample_count', 0), e2e_points_json, m.get('e2e_mean_hold_days'), m.get('e2e_mean_max_drawdown')))


__all__ = ["get_quality_metrics", "save_quality_metrics"]
