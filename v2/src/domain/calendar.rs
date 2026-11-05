//! Fixed-version scheduled market-day facts. No clock, IO or mutable data source.
use super::contract::RuleError;
use chrono::{Datelike, NaiveDate, Weekday};
use serde::{Deserialize, Serialize};

pub const CALENDAR_VERSION: &str = "sse-szse-scheduled-2026-v1";
pub const BUNDLED_CALENDAR_JSON: &str = include_str!("../../data/calendar-2026-v1.json");

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalendarSource {
    pub publisher: String,
    pub notice_id: String,
    pub published_on: String,
    pub url: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HolidayRange {
    pub name: String,
    pub start: String,
    pub end: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradingCalendar {
    pub schema_version: u32,
    pub version: String,
    pub markets: [String; 2],
    pub coverage_start: String,
    pub coverage_end: String,
    pub retrieved_on: String,
    pub sources: [CalendarSource; 2],
    pub holidays: Vec<HolidayRange>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CalendarDay {
    Trading,
    Weekend,
    Holiday,
    Unknown,
}

fn date(value: &str) -> Result<NaiveDate, RuleError> {
    let day = NaiveDate::parse_from_str(value, "%Y-%m-%d")
        .map_err(|_| RuleError("invalid_calendar_date".into()))?;
    if day.to_string() != value {
        return Err(RuleError("noncanonical_calendar_date".into()));
    }
    Ok(day)
}

impl TradingCalendar {
    pub fn validate(&self) -> Result<(), RuleError> {
        if self.schema_version != 1 || self.version != CALENDAR_VERSION
            || self.markets != ["SH", "SZ"] || self.holidays.is_empty()
        {
            return Err(RuleError("unsupported_or_incomplete_calendar".into()));
        }
        let start = date(&self.coverage_start)?;
        let end = date(&self.coverage_end)?;
        date(&self.retrieved_on)?;
        if start > end {
            return Err(RuleError("invalid_calendar_coverage".into()));
        }
        for source in &self.sources {
            date(&source.published_on)?;
            if source.publisher.is_empty() || source.notice_id.is_empty() || source.url.is_empty() {
                return Err(RuleError("missing_calendar_source".into()));
            }
        }
        let mut previous_end = None;
        for range in &self.holidays {
            let first = date(&range.start)?;
            let last = date(&range.end)?;
            if range.name.is_empty() || first > last || first < start || last > end
                || previous_end.is_some_and(|previous| first <= previous)
            {
                return Err(RuleError("invalid_calendar_holiday_range".into()));
            }
            previous_end = Some(last);
        }
        Ok(())
    }

    pub fn day(&self, day: NaiveDate) -> Result<CalendarDay, RuleError> {
        self.validate()?;
        if day < date(&self.coverage_start)? || day > date(&self.coverage_end)? {
            return Ok(CalendarDay::Unknown);
        }
        if matches!(day.weekday(), Weekday::Sat | Weekday::Sun) {
            return Ok(CalendarDay::Weekend);
        }
        for range in &self.holidays {
            if (date(&range.start)?..=date(&range.end)?).contains(&day) {
                return Ok(CalendarDay::Holiday);
            }
        }
        Ok(CalendarDay::Trading)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn published_2026_schedule_has_242_scheduled_trading_days() {
        let calendar: TradingCalendar = serde_json::from_str(BUNDLED_CALENDAR_JSON).unwrap();
        calendar.validate().unwrap();
        let mut day = date("2026-01-01").unwrap();
        let mut count = 0;
        while day <= date("2026-12-31").unwrap() {
            if calendar.day(day).unwrap() == CalendarDay::Trading { count += 1; }
            day = day.succ_opt().unwrap();
        }
        assert_eq!(count, 242);
        assert_eq!(calendar.day(date("2026-10-30").unwrap()).unwrap(), CalendarDay::Trading);
        for holiday in &calendar.holidays {
            let mut day = date(&holiday.start).unwrap();
            while day <= date(&holiday.end).unwrap() {
                assert_ne!(calendar.day(day).unwrap(), CalendarDay::Trading);
                day = day.succ_opt().unwrap();
            }
        }
    }

    #[test]
    fn government_makeup_work_weekends_do_not_become_trading_days() {
        let calendar: TradingCalendar = serde_json::from_str(BUNDLED_CALENDAR_JSON).unwrap();
        for value in ["2026-02-14", "2026-02-28", "2026-05-09", "2026-09-20", "2026-10-10"] {
            assert_eq!(calendar.day(date(value).unwrap()).unwrap(), CalendarDay::Weekend);
        }
        for value in ["2025-12-31", "2027-01-04"] {
            assert_eq!(calendar.day(date(value).unwrap()).unwrap(), CalendarDay::Unknown);
        }
    }

    #[test]
    fn malformed_or_unsupported_snapshots_fail_closed() {
        let original: TradingCalendar = serde_json::from_str(BUNDLED_CALENDAR_JSON).unwrap();
        let mut changed = original.clone();
        changed.version = "latest".into();
        assert!(changed.day(date("2026-10-30").unwrap()).is_err());
        changed = original.clone();
        changed.holidays[0].end = "2026-02-30".into();
        assert!(changed.validate().is_err());
        changed = original;
        changed.holidays.clear();
        assert!(changed.validate().is_err());
    }
}
