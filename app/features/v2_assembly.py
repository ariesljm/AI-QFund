"""票 11 前置：2.0 特征的数据装配层（估值维度）。

从 DB 装配「重仓股加权 PE 分位」：PIT 持仓（票 04 as_of）→ PE 历史（票 05
stock_valuation_daily）→ `weighted_valuation_percentile`（票 05 纯函数）。
回填完成后，`screen_top30` 的 2.0 打分可注入本装配的特征。

PEG 匹配度（票 08）需要个股盈利增速历史——数据源未定，暂不装配（增速缺 →
peg=None，宁缺勿用）。
"""

from app.features.valuation import weighted_valuation_percentile
from app.repo.base import get_holdings, get_pe_histories


def valuation_features(code: str, as_of: str,
                       pe_days: int = 750) -> dict:
    """重仓股估值特征：{"weighted_pe_pctile": float|None}。

    - 持仓：`get_holdings(code, 10, as_of)`（PIT：disclosure_date <= as_of）
    - PE 历史：`get_pe_histories`（最近 pe_days 日，末位=当前）
    - 无持仓 / 无 PE 数据 / 权重不可归一 → None（调用方回退缺省特征值）
    """
    holdings = get_holdings(code, limit=10, as_of=as_of)
    if not holdings:
        return {"weighted_pe_pctile": None}
    codes = [h["stock_code"] for h in holdings]
    hist = get_pe_histories(codes, days=pe_days)
    pct = weighted_valuation_percentile(holdings, hist)
    return {"weighted_pe_pctile": pct}
