//! Build a reviewed A-share close price set artifact from a CSV export.
//! Records a digest of the normalized input rows and can report coverage gaps.

use std::{
    collections::{BTreeSet, HashMap},
    fs,
    path::{Path, PathBuf},
};

use chrono::NaiveDate;
use serde::Serialize;

use super::sha256::sha256_hex;

const COLUMNS: [&str; 5] = ["symbol", "day", "price", "source", "observed_at"];

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PriceSetEntry {
    pub symbol: String,
    pub day: String,
    pub price_cents: i64,
    pub source: String,
    pub observed_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PriceSetArtifact {
    pub schema_version: u32,
    pub price_set_id: String,
    pub market_data_pack_id: String,
    pub trust: String,
    pub coverage_start: String,
    pub coverage_end: String,
    pub entries: Vec<PriceSetEntry>,
    pub source_digest: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct GapCell {
    pub symbol: String,
    pub day: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct GapReport {
    pub price_set_id: String,
    pub entries: usize,
    pub symbols: Vec<String>,
    pub days: Vec<String>,
    pub missing: Vec<GapCell>,
    pub source_digest: String,
}

pub fn parse_price(raw: &str) -> Result<i64, String> {
    let invalid = || format!("price_not_a_number:{raw}");
    let nonpositive = || format!("price_must_be_positive:{raw}");
    let trimmed = raw.trim();
    let unsigned = trimmed.strip_prefix(['-', '+']).unwrap_or(trimmed);
    let (mantissa, exponent) = match unsigned.split_once(['e', 'E']) {
        Some((mantissa, exponent)) => (mantissa, exponent.parse::<i64>().map_err(|_| invalid())?),
        None => (unsigned, 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if (whole.is_empty() && fraction.is_empty())
        || !whole
            .bytes()
            .chain(fraction.bytes())
            .all(|b| b.is_ascii_digit())
    {
        return Err(invalid());
    }
    let digits = format!("{whole}{fraction}");
    let digits = digits.trim_start_matches('0');
    if digits.is_empty() {
        return Err(nonpositive());
    }
    // Shift the decimal point into cents exactly, without rounding or floating point.
    let shift = exponent
        .saturating_add(2)
        .saturating_sub(fraction.len() as i64);
    let (significant, zeros) = if shift < 0 {
        let removed = usize::try_from(shift.unsigned_abs()).unwrap_or(usize::MAX);
        let keep = digits.len().saturating_sub(removed);
        if !digits[keep..].bytes().all(|b| b == b'0') {
            return Err(format!("price_more_than_two_decimals:{raw}"));
        }
        (&digits[..keep], 0)
    } else {
        (digits, shift)
    };
    if trimmed.starts_with('-') {
        return Err(nonpositive());
    }
    if zeros > 18 {
        return Err(invalid());
    }
    significant
        .parse::<i64>()
        .ok()
        .and_then(|cents| cents.checked_mul(10_i64.pow(zeros as u32)))
        .ok_or_else(invalid)
}

fn csv_record(
    chars: &mut std::iter::Peekable<std::str::Chars<'_>>,
) -> Result<Option<Vec<String>>, String> {
    if chars.peek().is_none() {
        return Ok(None);
    }
    let mut fields = Vec::new();
    let mut field = String::new();
    loop {
        if chars.next_if_eq(&'"').is_some() {
            loop {
                match chars.next() {
                    Some('"') if chars.next_if_eq(&'"').is_some() => field.push('"'),
                    Some('"') => break,
                    Some(character) => field.push(character),
                    None => return Err("unterminated CSV quoted field".into()),
                }
            }
            if !matches!(chars.peek(), None | Some(',' | '\r' | '\n')) {
                return Err("unexpected character after CSV quoted field".into());
            }
        } else {
            while let Some(character) = chars.next_if(|c| !matches!(c, ',' | '\r' | '\n')) {
                field.push(character);
            }
        }
        fields.push(std::mem::take(&mut field));
        match chars.next() {
            Some(',') => {}
            Some('\r') => {
                chars.next_if_eq(&'\n');
                break;
            }
            _ => break,
        }
    }
    Ok(Some(fields))
}

fn canonical_day(value: &str) -> Option<NaiveDate> {
    let day = NaiveDate::parse_from_str(value, "%Y-%m-%d").ok()?;
    (value.len() == 10 && !value.starts_with("0000") && day.to_string() == value).then_some(day)
}

pub fn load_rows(path: &Path) -> Result<Vec<PriceSetEntry>, String> {
    let raw = fs::read_to_string(path).map_err(|err| err.to_string())?;
    parse_rows(&raw)
}

fn parse_rows(raw: &str) -> Result<Vec<PriceSetEntry>, String> {
    let mut chars = raw.trim_start_matches('\u{feff}').chars().peekable();
    let header =
        csv_record(&mut chars)?.ok_or_else(|| format!("missing_columns:{}", COLUMNS.join(",")))?;
    let mut idx = HashMap::new();
    for (i, name) in header.iter().enumerate() {
        idx.insert(name.trim().to_string(), i);
    }
    let missing: Vec<&str> = COLUMNS
        .iter()
        .copied()
        .filter(|c| !idx.contains_key(*c))
        .collect();
    if !missing.is_empty() {
        return Err(format!("missing_columns:{}", missing.join(",")));
    }

    let mut seen = BTreeSet::new();
    let mut rows = Vec::new();
    let mut line_no = 1;
    while let Some(cells) = csv_record(&mut chars)? {
        line_no += 1;
        if cells.len() == 1 && cells[0].is_empty() {
            continue;
        }
        let get = |col: &str| -> String {
            idx.get(col)
                .and_then(|&pos| cells.get(pos))
                .map(|s| s.trim().to_string())
                .unwrap_or_default()
        };
        let symbol = get("symbol");
        let day = get("day");
        let price = get("price");
        let source = get("source");
        let observed_at = get("observed_at");
        if symbol.is_empty()
            || day.is_empty()
            || price.is_empty()
            || source.is_empty()
            || observed_at.is_empty()
        {
            return Err(format!("empty_field:line {line_no}"));
        }
        canonical_day(&day).ok_or_else(|| format!("invalid_day:line {line_no}:{day}"))?;
        if !seen.insert((symbol.clone(), day.clone())) {
            return Err(format!("duplicate_row:{symbol}:{day}"));
        }
        let price_cents = parse_price(&price)?;
        rows.push(PriceSetEntry {
            symbol,
            day,
            price_cents,
            source,
            observed_at,
        });
    }
    if rows.is_empty() {
        return Err("empty_input".to_string());
    }
    Ok(rows)
}

pub fn source_digest(rows: &[PriceSetEntry]) -> String {
    let mut lines: Vec<String> = rows
        .iter()
        .map(|row| {
            format!(
                "{},{},{},{},{}\n",
                row.symbol, row.day, row.price_cents, row.source, row.observed_at
            )
        })
        .collect();
    lines.sort();
    sha256_hex(lines.concat().as_bytes())
}

pub fn build_set(
    mut rows: Vec<PriceSetEntry>,
    set_id: &str,
    pack: &str,
    trust: &str,
    coverage_start: &str,
    coverage_end: &str,
    symbols: &[String],
) -> Result<PriceSetArtifact, String> {
    if trust != "reviewed" && trust != "synthetic" {
        return Err(format!("invalid_trust:{trust}"));
    }
    let start = canonical_day(coverage_start).ok_or("invalid_coverage")?;
    let end = canonical_day(coverage_end).ok_or("invalid_coverage")?;
    if start > end {
        return Err("invalid_coverage".to_string());
    }
    let wanted: BTreeSet<&str> = symbols.iter().map(String::as_str).collect();
    for row in &rows {
        if !wanted.contains(row.symbol.as_str()) {
            return Err(format!("symbol_not_declared:{}", row.symbol));
        }
        let day = canonical_day(&row.day).ok_or_else(|| format!("invalid_day:{}", row.day))?;
        if day < start || day > end {
            return Err(format!("entry_outside_coverage:{}:{}", row.symbol, row.day));
        }
    }
    let digest = source_digest(&rows);
    rows.sort_by(|a, b| a.symbol.cmp(&b.symbol).then_with(|| a.day.cmp(&b.day)));
    Ok(PriceSetArtifact {
        schema_version: 1,
        price_set_id: set_id.to_string(),
        market_data_pack_id: pack.to_string(),
        trust: trust.to_string(),
        coverage_start: coverage_start.to_string(),
        coverage_end: coverage_end.to_string(),
        entries: rows,
        source_digest: digest,
    })
}

pub fn gap_report(entries: &[PriceSetEntry], symbols: &[String], days: &[String]) -> Vec<GapCell> {
    let have: BTreeSet<(&str, &str)> = entries
        .iter()
        .map(|e| (e.symbol.as_str(), e.day.as_str()))
        .collect();
    let mut missing = Vec::new();
    for symbol in symbols {
        for day in days {
            if !have.contains(&(symbol.as_str(), day.as_str())) {
                missing.push(GapCell {
                    symbol: symbol.clone(),
                    day: day.clone(),
                });
            }
        }
    }
    missing
}

pub fn run(args: &[String]) -> i32 {
    const USAGE: &str = "usage: astock-v2 price-set --input <csv> --out <json> --set-id <id> --pack <id> --coverage-start <day> --coverage-end <day> --symbols <csv> [--days <csv>] [--report <json>] [--trust reviewed|synthetic] [--allow-gaps]";
    let mut input: Option<PathBuf> = None;
    let mut out: Option<PathBuf> = None;
    let mut report_path: Option<PathBuf> = None;
    let mut set_id: Option<&str> = None;
    let mut pack: Option<&str> = None;
    let mut trust = "reviewed";
    let mut coverage_start: Option<&str> = None;
    let mut coverage_end: Option<&str> = None;
    let mut symbols_raw: Option<&str> = None;
    let mut days_raw = "";
    let mut allow_gaps = false;

    let mut flags = args.iter().map(String::as_str).peekable();
    while let Some(arg) = flags.next() {
        let (flag, inline) = arg
            .split_once('=')
            .map_or((arg, None), |(f, v)| (f, Some(v)));
        match flag {
            "-h" | "--help" if inline.is_none() => {
                println!("{USAGE}");
                return 0;
            }
            "--allow-gaps" if inline.is_none() => {
                allow_gaps = true;
                continue;
            }
            _ => {}
        }
        let value =
            inline.or_else(|| flags.next_if(|next| !next.starts_with("--") && *next != "-h"));
        let Some(value) = value else {
            eprintln!("error: missing value for {flag}");
            return 2;
        };
        match flag {
            "--input" => input = Some(PathBuf::from(value)),
            "--out" => out = Some(PathBuf::from(value)),
            "--report" => report_path = Some(PathBuf::from(value)),
            "--set-id" => set_id = Some(value),
            "--pack" => pack = Some(value),
            "--trust" => trust = value,
            "--coverage-start" => coverage_start = Some(value),
            "--coverage-end" => coverage_end = Some(value),
            "--symbols" => symbols_raw = Some(value),
            "--days" => days_raw = value,
            other => {
                eprintln!("error: unknown argument: {other}");
                return 2;
            }
        }
    }

    let (
        Some(input),
        Some(out),
        Some(set_id),
        Some(pack),
        Some(cov_start),
        Some(cov_end),
        Some(sym_raw),
    ) = (
        input,
        out,
        set_id,
        pack,
        coverage_start,
        coverage_end,
        symbols_raw,
    )
    else {
        eprintln!("{USAGE}");
        return 2;
    };

    let symbols: Vec<String> = sym_raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();
    let days: Vec<String> = days_raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();

    let rows = match load_rows(&input) {
        Ok(r) => r,
        Err(err) => {
            eprintln!("error: {err}");
            return 2;
        }
    };
    let artifact = match build_set(rows, set_id, pack, trust, cov_start, cov_end, &symbols) {
        Ok(a) => a,
        Err(err) => {
            eprintln!("error: {err}");
            return 2;
        }
    };
    let missing = if days.is_empty() {
        Vec::new()
    } else {
        gap_report(&artifact.entries, &symbols, &days)
    };
    if let Some(rpath) = report_path {
        let report = GapReport {
            price_set_id: artifact.price_set_id.clone(),
            entries: artifact.entries.len(),
            symbols: symbols.clone(),
            days: days.clone(),
            missing: missing.clone(),
            source_digest: artifact.source_digest.clone(),
        };
        if let Some(parent) = rpath.parent().filter(|p| !p.as_os_str().is_empty())
            && let Err(err) = fs::create_dir_all(parent)
        {
            eprintln!("error: {err}");
            return 2;
        }
        let body = format!("{}\n", serde_json::to_string_pretty(&report).unwrap());
        if let Err(err) = fs::write(&rpath, body) {
            eprintln!("error: {err}");
            return 2;
        }
    }
    if !missing.is_empty() && !allow_gaps {
        eprintln!(
            "error: price_set_has_gaps:{} (use --allow-gaps to write anyway)",
            missing.len()
        );
        return 3;
    }
    if let Some(parent) = out.parent().filter(|p| !p.as_os_str().is_empty())
        && let Err(err) = fs::create_dir_all(parent)
    {
        eprintln!("error: {err}");
        return 2;
    }
    let body = format!("{}\n", serde_json::to_string_pretty(&artifact).unwrap());
    if let Err(err) = fs::write(&out, body) {
        eprintln!("error: {err}");
        return 2;
    }
    println!(
        "wrote {} entries={} missing={} source_digest={}",
        out.display(),
        artifact.entries.len(),
        missing.len(),
        artifact.source_digest
    );
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_more_than_two_decimals_and_non_positive() {
        assert_eq!(parse_price("12.34"), Ok(1234));
        assert_eq!(parse_price("12.3"), Ok(1230));
        assert_eq!(parse_price("12"), Ok(1200));
        assert_eq!(parse_price("+1.2340e1"), Ok(1234));
        assert_eq!(parse_price("1E-2"), Ok(1));
        assert_eq!(parse_price("92233720368547758.07"), Ok(i64::MAX));
        assert!(parse_price("92233720368547758.08").is_err());
        assert!(parse_price("1e-3").is_err());
        assert!(parse_price("1e100").is_err());
        assert!(parse_price("12.345").is_err());
        assert!(parse_price("0").is_err());
        assert!(parse_price("-1.00").is_err());
    }

    #[test]
    fn quoted_multiline_cell_cannot_inject_a_price_row() {
        let input = concat!(
            "symbol,day,price,source,observed_at,notes\r\n",
            "600000.SH,2026-11-19,10,fixture,2026-11-19T15:00:00+08:00,\"notes\r\n",
            "600000.SH,2026-11-20,11,fixture,2026-11-20T15:00:00+08:00,ignored\"\r\n",
        );
        let rows = parse_rows(input).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(
            gap_report(&rows, &["600000.SH".into()], &["2026-11-20".into()]).len(),
            1
        );
        assert!(parse_rows(&input.replace("2026-11-19,10", "2026-11-9,10")).is_err());
    }

    #[test]
    fn missing_flag_value_returns_usage_error() {
        assert_eq!(run(&["--report".into()]), 2);
    }

    #[test]
    fn cli_builds_reviewed_artifacts_and_reports_gaps() {
        let dir = std::env::temp_dir().join(format!("astock-price-set-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let input = dir.join("prices.csv");
        let out = dir.join("set.json");
        let report = dir.join("report.json");
        fs::write(&input, format!("{HEADER}{ROWS}")).unwrap();
        let mut args = vec![
            format!("--input={}", input.display()),
            format!("--out={}", out.display()),
            format!("--report={}", report.display()),
            "--set-id=cn-close-2026-11".into(),
            "--pack=cn-offline-fixture-pack-1".into(),
            "--coverage-start=2026-10-30".into(),
            "--coverage-end=2026-11-02".into(),
            "--symbols=600000.SH".into(),
            "--days=2026-10-30,2026-11-02,2026-11-03".into(),
        ];
        let read_json = |path: &Path| -> serde_json::Value {
            serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
        };
        assert_eq!(run(&args), 3);
        assert!(!out.exists());
        assert_eq!(
            read_json(&report)["missing"],
            serde_json::json!([{"symbol": "600000.SH", "day": "2026-11-03"}])
        );
        args.push("--allow-gaps".into());
        assert_eq!(run(&args), 0);
        let artifact = read_json(&out);
        assert_eq!(artifact["schema_version"], 1);
        assert_eq!(artifact["trust"], "reviewed");
        assert_eq!(artifact["entries"][0]["price_cents"], 1234);
        assert_eq!(artifact["entries"][1]["price_cents"], 1219);
        assert_eq!(
            artifact["source_digest"],
            "353a24025fdb8aa8aaee094af829ec27de7441f21c9dc76fbec450597bb51d8c"
        );
        args.pop();
        *args.last_mut().unwrap() = "--days=2026-10-30,2026-11-02".into();
        assert_eq!(run(&args), 0);
        let complete_report = read_json(&report);
        assert_eq!(complete_report["missing"], serde_json::json!([]));
        assert_eq!(complete_report["entries"], 2);
        assert_eq!(complete_report["source_digest"], artifact["source_digest"]);
        fs::remove_dir_all(dir).unwrap();
    }

    const HEADER: &str = "symbol,day,price,source,observed_at
";
    const ROWS: &str = concat!(
        "600000.SH,2026-10-30,12.34,vendor-close,2026-10-30T15:00:00+08:00
",
        "600000.SH,2026-11-02,12.19,vendor-close,2026-11-02T15:00:00+08:00
",
    );

    #[test]
    fn rejects_duplicate_undeclared_and_out_of_coverage_rows() {
        let duplicate = ROWS.lines().next().unwrap();
        assert_eq!(
            parse_rows(&format!(
                "{HEADER}{ROWS}{duplicate}
"
            )),
            Err("duplicate_row:600000.SH:2026-10-30".into())
        );
        let rows = parse_rows(&format!("{HEADER}{ROWS}")).unwrap();
        assert_eq!(
            build_set(
                rows.clone(),
                "s",
                "pack",
                "reviewed",
                "2026-10-30",
                "2026-11-02",
                &["000001.SZ".into()]
            ),
            Err("symbol_not_declared:600000.SH".into())
        );
        assert_eq!(
            build_set(
                rows,
                "s",
                "pack",
                "reviewed",
                "2026-11-01",
                "2026-11-02",
                &["600000.SH".into()]
            ),
            Err("entry_outside_coverage:600000.SH:2026-10-30".into())
        );
    }

    #[test]
    fn sample_prices_digest_is_stable() {
        let sample =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("data/price-set-examples/sample-prices.csv");
        let rows = load_rows(&sample).unwrap();
        let artifact = build_set(
            rows,
            "ci-example",
            "ci-pack",
            "synthetic",
            "2026-10-30",
            "2026-11-03",
            &["600000.SH".to_string()],
        )
        .unwrap();
        assert_eq!(artifact.entries.len(), 3);
        assert_eq!(
            artifact.source_digest,
            "1a2ca8ef81169150a331fa1a805cd3af2c5c6938a6e69d7c6c14c5e97fa2c5bc"
        );
    }
}
