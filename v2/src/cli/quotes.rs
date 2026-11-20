//! Tencent batch snapshot quote parser (`qt.gtimg.cn`).
//! Rust equivalent of `v2/data_sources/realtime_quotes.py` parsing semantics
//! with exact integer-cent arithmetic (no float drift).

const F_NAME: usize = 1;
const F_PRICE: usize = 3;
const F_PREV_CLOSE: usize = 4;
const F_OPEN: usize = 5;
const F_VOLUME_HAND: usize = 6;
const F_HIGH: usize = 33;
const F_LOW: usize = 34;
const F_AMOUNT_WAN: usize = 37;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TencentQuote {
    pub symbol: String,
    pub name: String,
    pub price_cents: i64,
    pub prev_close_cents: i64,
    pub open_cents: i64,
    pub high_cents: i64,
    pub low_cents: i64,
    pub volume_shares: i64,
    pub amount_cents: i64,
    pub observed_at: String,
}

impl TencentQuote {
    pub fn change_bps(&self) -> i64 {
        if self.prev_close_cents <= 0 {
            return 0;
        }
        let change = (i128::from(self.price_cents) - i128::from(self.prev_close_cents)) * 10_000;
        let bps = change.div_euclid(i128::from(self.prev_close_cents));
        bps.clamp(i128::from(i64::MIN), i128::from(i64::MAX)) as i64
    }
}

pub fn to_symbol(market: &str, code: &str) -> String {
    format!("{code}.{}", market.to_ascii_uppercase())
}

pub fn to_tencent(symbol: &str) -> String {
    let (code, market) = symbol.split_once('.').unwrap_or((symbol, ""));
    format!("{}{code}", market.to_ascii_lowercase())
}

/// Parse a decimal price string into integer cents without float drift.
pub fn to_cents(raw: &str) -> Result<i64, String> {
    let text = raw.trim();
    if text.is_empty() {
        return Err("empty_price_field".to_string());
    }
    let (negative, unsigned) = if let Some(rest) = text.strip_prefix('-') {
        (true, rest)
    } else {
        (false, text)
    };
    let (whole, frac) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    if (!whole.is_empty() && !whole.bytes().all(|b| b.is_ascii_digit()))
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(format!("unparsable_price:{raw}"));
    }
    let whole_val: i64 = if whole.is_empty() {
        0
    } else {
        whole
            .parse()
            .map_err(|_| format!("unparsable_price:{raw}"))?
    };
    let d1 = frac.as_bytes().first().copied().unwrap_or(b'0') - b'0';
    let d2 = frac.as_bytes().get(1).copied().unwrap_or(b'0') - b'0';
    let value = whole_val
        .checked_mul(100)
        .and_then(|v| v.checked_add(i64::from(d1) * 10 + i64::from(d2)))
        .ok_or_else(|| format!("unparsable_price:{raw}"))?;
    Ok(if negative { -value } else { value })
}

/// Convert Tencent `amount` in 10,000-yuan (`万`) units into integer cents with
/// half-up rounding, matching `Decimal(text) * 1_000_000` quantized to 1.
fn wan_to_cents(raw: &str) -> Result<i64, String> {
    let text = raw.trim();
    if text.is_empty() {
        return Ok(0);
    }
    let (negative, unsigned) = if let Some(rest) = text.strip_prefix('-') {
        (true, rest)
    } else {
        (false, text)
    };
    let (whole, frac) = unsigned.split_once('.').unwrap_or((unsigned, ""));
    if (!whole.is_empty() && !whole.bytes().all(|b| b.is_ascii_digit()))
        || !frac.bytes().all(|b| b.is_ascii_digit())
    {
        return Err("invalid_amount".to_string());
    }
    let whole_val: i128 = if whole.is_empty() {
        0
    } else {
        whole.parse().map_err(|_| "invalid_amount")?
    };
    let mut scaled = whole_val * 1_000_000;
    let bytes = frac.as_bytes();
    let mut mul = 100_000i128;
    for &b in bytes.iter().take(6) {
        scaled += i128::from(b - b'0') * mul;
        mul /= 10;
    }
    if bytes.get(6).copied().is_some_and(|b| b >= b'5') {
        scaled += 1;
    }
    let signed = if negative { -scaled } else { scaled };
    i64::try_from(signed).map_err(|_| "amount_overflow".to_string())
}

pub fn parse_record(symbol: &str, payload: &str, observed_at: &str) -> Option<TencentQuote> {
    let fields: Vec<&str> = payload.split('~').collect();
    if fields.len() < 40 {
        return None;
    }
    let price_str = fields[F_PRICE].trim();
    if price_str.is_empty() {
        return None;
    }
    let price_cents = to_cents(price_str).ok()?;
    if price_cents <= 0 {
        return None;
    }
    let prev_close_cents = to_cents(fields[F_PREV_CLOSE]).ok()?;
    let open_cents = to_cents(fields[F_OPEN]).ok()?;
    let high_cents = to_cents(fields[F_HIGH]).ok()?;
    let low_cents = to_cents(fields[F_LOW]).ok()?;
    let vol_hand_str = fields[F_VOLUME_HAND].trim();
    let volume_hands: i64 = if vol_hand_str.is_empty() {
        0
    } else {
        vol_hand_str.parse().ok()?
    };
    let volume_shares = volume_hands.checked_mul(100)?;
    let amount_cents = wan_to_cents(fields[F_AMOUNT_WAN]).ok()?;
    Some(TencentQuote {
        symbol: symbol.to_string(),
        name: fields[F_NAME].to_string(),
        price_cents,
        prev_close_cents,
        open_cents,
        high_cents,
        low_cents,
        volume_shares,
        amount_cents,
        observed_at: observed_at.to_string(),
    })
}

pub fn split_records(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    for line in text.split(';') {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some((_, rest)) = trimmed.split_once("=\"")
            && let Some((payload, _)) = rest.rsplit_once('"')
            && !payload.is_empty()
        {
            out.push(payload);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_cents_without_float_drift() {
        assert_eq!(to_cents("19.99"), Ok(1999));
        assert_eq!(to_cents("10.5"), Ok(1050));
        assert_eq!(to_cents("10"), Ok(1000));
        assert!(to_cents("").is_err());
        assert!(to_cents("abc").is_err());
    }

    #[test]
    fn parses_and_splits_tencent_records() {
        let mut fields = vec![""; 41];
        fields[1] = "浦发银行";
        fields[2] = "600000";
        fields[3] = "10.25";
        fields[4] = "10.00";
        fields[5] = "10.05";
        fields[6] = "1234";
        fields[33] = "10.30";
        fields[34] = "9.98";
        fields[37] = "126.5";
        let payload = fields.join("~");
        let raw = format!("v_sh600000=\"{payload}\";\nv_sz000001=\"\";");
        let recs = split_records(&raw);
        assert_eq!(recs.len(), 1);
        let q = parse_record("600000.SH", recs[0], "2026-11-20T09:30:00+08:00").unwrap();
        assert_eq!(q.name, "浦发银行");
        assert_eq!(q.price_cents, 1025);
        assert_eq!(q.volume_shares, 123_400);
        assert_eq!(q.amount_cents, 126_500_000);
        assert_eq!(q.change_bps(), 250);
    }

    #[test]
    fn skips_zero_price_and_short_records() {
        assert!(parse_record("600000.SH", "a~b~c", "t").is_none());
        let mut fields = vec!["0"; 41];
        fields[3] = "0.00";
        assert!(parse_record("600000.SH", &fields.join("~"), "t").is_none());
    }
}
