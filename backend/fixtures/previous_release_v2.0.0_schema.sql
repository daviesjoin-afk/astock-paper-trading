-- R26 previous-version compatibility fixture.
--
-- 来源：release tag v2.0.0 的 commit cbb1863b8d3cc1f07e52dfe9b99a360494f53be4
-- 里的 backend/paper_trading.py init_db() ``executescript`` DDL 块，**逐字**摘录。
--
-- 为什么把它落成文件而不是在测试里跑 ``git show``：CI 的 actions/checkout 默认
-- 浅克隆且 --no-tags，测试进程里根本解析不到 tag。兼容性 gate 的价值在于 fixture
-- 必须**真的**是上一版本产生的形状，所以这里固定住真实来源并标注 commit；
-- 只要 tag 可解析（本地/完整检出），
-- ``test_fixture_matches_the_live_release_tag`` 就会逐字比对，防止这份摘录漂移。
--
-- 手动重新生成：
--   git show v2.0.0:backend/paper_trading.py
-- 取 "CREATE TABLE IF NOT EXISTS paper_accounts" 到其所在字符串字面量的收尾引号。

CREATE TABLE IF NOT EXISTS paper_accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source_strategy TEXT NOT NULL,
                status TEXT NOT NULL, initial_cash REAL NOT NULL, cash REAL NOT NULL,
                cycle_days INTEGER NOT NULL, max_positions INTEGER NOT NULL,
                max_weight REAL NOT NULL, max_exposure REAL NOT NULL, version TEXT NOT NULL,
                benchmark_start REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                signal_date TEXT NOT NULL, intended_date TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT, industry TEXT, close_price REAL, rank_score REAL, t_tier TEXT,
                t_score REAL, payload TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                created_at TEXT NOT NULL, strategy_id TEXT,
                strategy_version INTEGER, strategy_checksum TEXT,
                UNIQUE(account_id, signal_date, code)
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                signal_id INTEGER, side TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
                qty INTEGER NOT NULL, planned_price REAL, filled_price REAL,
                amount REAL, fees REAL, status TEXT NOT NULL, reason TEXT,
                risk_payload TEXT NOT NULL, realized_pnl REAL, created_at TEXT NOT NULL, executed_at TEXT,
                order_type TEXT NOT NULL DEFAULT 'market',
                origin TEXT NOT NULL DEFAULT 'strategy', expires_at TEXT, cancelled_at TEXT,
                strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT,
                retry_of_order_id INTEGER,
                execution_status TEXT, execution_verified INTEGER, execution_evidence_source TEXT,
                cycle_id INTEGER
            );
            -- 归档表：列集与活跃表严格一致（清理函数用 SELECT * 归档），避免列错位。
            CREATE TABLE IF NOT EXISTS paper_orders_archive (
                id INTEGER, account_id TEXT, signal_id INTEGER, side TEXT, code TEXT, name TEXT,
                qty INTEGER, planned_price REAL, filled_price REAL, amount REAL, fees REAL,
                status TEXT, reason TEXT, risk_payload TEXT, realized_pnl REAL,
                created_at TEXT, executed_at TEXT, order_type TEXT, origin TEXT,
                expires_at TEXT, cancelled_at TEXT, strategy_id TEXT,
                strategy_version INTEGER, strategy_checksum TEXT, retry_of_order_id INTEGER,
                execution_status TEXT, execution_verified INTEGER, execution_evidence_source TEXT,
                cycle_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS paper_signals_archive (
                id INTEGER, account_id TEXT, signal_date TEXT, intended_date TEXT, code TEXT, name TEXT,
                industry TEXT, close_price REAL, rank_score REAL, t_tier TEXT, t_score REAL,
                payload TEXT, status TEXT, reason TEXT, created_at TEXT,
                strategy_id TEXT, strategy_version INTEGER, strategy_checksum TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
                qty INTEGER NOT NULL, cost REAL NOT NULL, entry_date TEXT NOT NULL,
                available_date TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'stock_t1', peak_price REAL, take_stage INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(account_id, code)
            );
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, side TEXT NOT NULL, code TEXT NOT NULL,
                qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL, fees REAL NOT NULL,
                fill_date TEXT NOT NULL, quote_at TEXT, assumption TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                code TEXT, side TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT,
                payload TEXT NOT NULL, created_at TEXT NOT NULL, strategy_id TEXT,
                strategy_version INTEGER, strategy_checksum TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_risk_decisions_recent
                ON paper_risk_decisions(id DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_risk_decisions_filter
                ON paper_risk_decisions(account_id, created_at DESC, code, decision);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_recent
                ON paper_orders(id DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_account_status
                ON paper_orders(account_id, status, created_at DESC);
            CREATE TABLE IF NOT EXISTS paper_capital_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                order_key TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                code TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                fees REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'reserved',
                created_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_capital_reservations_cycle_status
                ON paper_capital_reservations(cycle_id, status);
            CREATE TABLE IF NOT EXISTS paper_ignition_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                bucket TEXT NOT NULL,
                code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                price REAL,
                pct REAL,
                runup REAL,
                old_rule_passed INTEGER NOT NULL DEFAULT 0,
                old_rule_reason TEXT,
                ignition_passed INTEGER NOT NULL DEFAULT 0,
                ignition_reasons TEXT,
                price_30m REAL,
                at_30m TEXT,
                price_60m REAL,
                at_60m TEXT,
                resolved INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_ignition_shadow_unique
                ON paper_ignition_shadow(day, bucket, code);
            CREATE INDEX IF NOT EXISTS idx_paper_ignition_shadow_recent
                ON paper_ignition_shadow(day, resolved);
            CREATE TABLE IF NOT EXISTS paper_position_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL,
                code TEXT NOT NULL,
                review_date TEXT NOT NULL,
                score REAL NOT NULL,
                grade TEXT NOT NULL,
                action TEXT NOT NULL,
                market_value REAL NOT NULL DEFAULT 0,
                position_pct REAL NOT NULL DEFAULT 0,
                replacement_code TEXT,
                replacement_score REAL,
                reasons TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(cycle_id, account_id, code, review_date)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_position_reviews_recent
                ON paper_position_reviews(cycle_id, review_date, score);
            CREATE TABLE IF NOT EXISTS paper_nav (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                nav_date TEXT NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL,
                nav REAL NOT NULL, benchmark REAL, created_at TEXT NOT NULL,
                quote_status TEXT NOT NULL DEFAULT 'verified',
                UNIQUE(account_id, nav_date)
            );
            CREATE TABLE IF NOT EXISTS paper_jobs (
                slot TEXT NOT NULL, market_date TEXT NOT NULL, status TEXT NOT NULL,
                detail TEXT, started_at TEXT NOT NULL, finished_at TEXT,
                 owner_key TEXT, heartbeat_at TEXT, expires_at TEXT,
                 fencing_token INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(slot, market_date)
            );
            CREATE TABLE IF NOT EXISTS paper_reviews (
                week_key TEXT NOT NULL, account_id TEXT NOT NULL, report TEXT NOT NULL,
                recommendation TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(week_key, account_id)
            );
            CREATE TABLE IF NOT EXISTS paper_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, event TEXT NOT NULL,
                detail TEXT, created_at TEXT NOT NULL, strategy_id TEXT,
                strategy_version INTEGER, strategy_checksum TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, capital REAL NOT NULL, risk_profile TEXT NOT NULL,
                started_at TEXT, ended_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_position_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, industry TEXT,
                qty INTEGER NOT NULL, remaining_qty INTEGER NOT NULL, cost REAL NOT NULL,
                acquired_at TEXT NOT NULL, available_date TEXT NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'stock_t1', source_order_id INTEGER,
                cost_fee_included INTEGER NOT NULL DEFAULT 0,
                is_t_base INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_paper_lots_active
                ON paper_position_lots(cycle_id, account_id, code, available_date);
            CREATE TABLE IF NOT EXISTS paper_intraday_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, code TEXT, observed_at TEXT NOT NULL,
                price REAL, action TEXT NOT NULL, reason TEXT, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_intraday_obs
                ON paper_intraday_observations(cycle_id, account_id, observed_at);
            CREATE TABLE IF NOT EXISTS paper_parameter_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                account_id TEXT NOT NULL, version TEXT NOT NULL, style TEXT NOT NULL,
                params TEXT NOT NULL, reason TEXT NOT NULL, effective_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, cycle_key TEXT,
                reason TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_job_runs (
                run_key TEXT PRIMARY KEY, slot TEXT NOT NULL, market_date TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT, started_at TEXT NOT NULL, finished_at TEXT,
                 owner_key TEXT, heartbeat_at TEXT, expires_at TEXT,
                 fencing_token INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS paper_runtime_locks (
                lock_key TEXT PRIMARY KEY,
                owner_key TEXT NOT NULL,
                slot TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT,
                expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS paper_position_limit_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                allocation_key TEXT NOT NULL,
                pool_limit INTEGER NOT NULL,
                limits TEXT NOT NULL,
                weights TEXT NOT NULL,
                inputs TEXT NOT NULL,
                source TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(cycle_id, allocation_key)

            );
            CREATE INDEX IF NOT EXISTS idx_paper_position_limit_versions_cycle
                ON paper_position_limit_versions(cycle_id, effective_at DESC);
