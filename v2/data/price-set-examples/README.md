# 价格集合样例（模板，非审核数据）

`sample-prices.csv` 是 `make_price_set.py` 的输入模板：

```bash
python research/make_price_set.py \
  --input data/price-set-examples/sample-prices.csv \
  --out /tmp/example.json \
  --report /tmp/example-report.json \
  --set-id example-synthetic \
  --pack cn-offline-fixture-pack-1 \
  --trust synthetic \
  --coverage-start 2026-10-30 --coverage-end 2026-11-03 \
  --symbols 600000.SH
```

- **它不是行情数据**，价格与时间为示意值；
- 只用于演示列结构与校验规则（缺口、重复、区间越界会直接失败）；
- `trust` 必须显式写 `synthetic`；放进 `data/price-sets/` 也不会被估值采用；
- 真实数据必须来自有来源、可复核的行情文件，`trust=reviewed`，并经人工审核后由
  `scripts/publish_price_set.sh` 安装。
