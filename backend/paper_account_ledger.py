"""paper_accounts 的唯一持久化入口。

现金、开户、周期、配置模块保留各自判断；此处仅执行窄业务写入。
使用调用方连接，不读时钟、不开启/提交事务、不吞异常；跨字段操作保留单条 SQL。
"""
_UNSET = object()


def write_params(conn, account_id, *, params, now=_UNSET, version=_UNSET):
    """写参数快照；未提供字段不变，显式 None 交给数据库处理约束。"""
    sets = ["params=?"]
    values: list = [params]
    if version is not _UNSET:
        sets.append("version=?")
        values.append(version)
    if now is not _UNSET:
        sets.append("updated_at=?")
        values.append(now)
    values.append(account_id)
    conn.execute(
        f"UPDATE paper_accounts SET {','.join(sets)} WHERE id=?",
        tuple(values),
    )


def set_style(conn, account_id, *, style, now, params=None):
    """写账户风格字段；``params`` 给出时同一语句里一并刷新参数快照。"""
    if params is None:
        conn.execute(
            "UPDATE paper_accounts SET style=?,updated_at=? WHERE id=?",
            (style, now, account_id),
        )
    else:
        conn.execute(
            "UPDATE paper_accounts SET style=?,params=?,updated_at=? WHERE id=?",
            (style, params, now, account_id),
        )


def set_cooldown(conn, *, cooldown_until, account_id=None):
    """写共享风控冷却窗口；``account_id`` 为空表示整池（原始语义）。"""
    if account_id is None:
        conn.execute(
            "UPDATE paper_accounts SET cooldown_until=?",
            (cooldown_until,),
        )
    else:
        conn.execute(
            "UPDATE paper_accounts SET cooldown_until=? WHERE id=?",
            (cooldown_until, account_id),
        )


ACCOUNT_INSERT_COLUMNS = (
    "id", "name", "source_strategy", "status", "initial_cash", "cash", "cycle_days",
    "max_positions", "max_weight", "max_exposure", "risk_profile", "version",
    "benchmark_start", "created_at", "updated_at",
)

def insert_account(conn, *, fields):
    """创建一条账户行；列集合受 :data:`ACCOUNT_INSERT_COLUMNS` 约束。"""
    unknown = set(fields) - set(ACCOUNT_INSERT_COLUMNS)
    if unknown:
        raise ValueError(f"未知账户列：{sorted(unknown)}")
    columns = [name for name in ACCOUNT_INSERT_COLUMNS if name in fields]
    marks = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO paper_accounts({','.join(columns)}) VALUES({marks})",
        tuple(fields[name] for name in columns),
    )


def ensure_profile(conn, account_id, *, name, source_strategy, cycle_days,
                   max_weight, max_exposure, mode, risk_profile):
    """既有账户的档案字段补齐（模式为空时才写入调用方给出的默认模式）。"""
    conn.execute(
        """UPDATE paper_accounts SET name=?,source_strategy=?,cycle_days=?,
           max_weight=?,max_exposure=?,mode=COALESCE(NULLIF(mode,''),?),
           risk_profile=? WHERE id=?""",
        (name, source_strategy, cycle_days, max_weight, max_exposure, mode,
         risk_profile, account_id),
    )


def set_version(conn, account_id, *, version):
    """只给新引入的策略写首次版本号；已有版本不被配置刷新改写。"""
    conn.execute(
        "UPDATE paper_accounts SET version=? WHERE id=?",
        (version, account_id),
    )


def detach_cycle(conn, account_id, *, now):
    """账户退出当前周期：清空 cycle_id 并把账户置为暂停、资金归零。"""
    conn.execute(
        "UPDATE paper_accounts SET cycle_id=NULL,status='paused',"
        "initial_cash=0,cash=0,updated_at=? WHERE id=?",
        (now, account_id),
    )


def set_status(conn, *, status, now, clause, clause_params=()):
    """账户启停：``clause`` 由调用方给出（活动周期过滤），本入口只落库状态。"""
    if not clause:
        raise ValueError("set_status 需要显式过滤条件")
    return conn.execute(
        f"UPDATE paper_accounts SET status=?, updated_at=? WHERE {clause}",
        (status, now, *clause_params),
    ).rowcount


def set_cycle_capital(conn, account_id, *, cycle_id, capital, now):
    """周期资金配置：在同一周期下把初始资金/现金/基准净值设为同一份额。

    ``WHERE cycle_id=? AND id=?`` 保留原保护 —— 账户已经切到别的周期时不写。
    """
    return conn.execute(
        "UPDATE paper_accounts SET initial_cash=?,cash=?,daily_start_nav=?,updated_at=?"
        " WHERE cycle_id=? AND id=?",
        (capital, capital, capital, now, cycle_id, account_id),
    ).rowcount


def initialize_user_cycle(conn, account_id, *, cycle_id, mode, style, status,
                          capital, benchmark, nav_date, risk_profile, version,
                          max_positions, max_weight, max_exposure, now):
    """用户账户首次接入周期：资金、画像及限额在同一语句内初始化。"""
    conn.execute(
        "UPDATE paper_accounts SET cycle_id=?,mode=?,style=?,status=?,initial_cash=?,cash=?,benchmark_start=?,daily_start_nav=?,daily_nav_date=?,risk_profile=?,version=?,max_positions=?,max_weight=?,max_exposure=?,updated_at=? WHERE id=?",
        (cycle_id, mode, style, status, capital, capital, benchmark, capital, nav_date,
         risk_profile, version, max_positions, max_weight, max_exposure, now, account_id),
    )


def debit_cash(conn, account_id, *, amount, now):
    """扣除已由现金业务模块校验的金额。"""
    conn.execute("UPDATE paper_accounts SET cash=cash-?,updated_at=? WHERE id=?",
                 (amount, now, account_id))


def apply_cash_delta(conn, account_id, *, delta, now):
    """回款或对账补偿；只落库调用方已判定的增量。"""
    conn.execute("UPDATE paper_accounts SET cash=cash+?,updated_at=? WHERE id=?",
                 (delta, now, account_id))



CYCLE_MEMBERSHIP_COLUMNS = (
    "name", "source_strategy", "status", "initial_cash", "cash", "cycle_days",
    "max_positions", "max_weight", "max_exposure", "version", "benchmark_start",
    "cycle_id", "mode", "style", "risk_profile", "params", "daily_start_nav",
    "daily_nav_date", "cooldown_until", "updated_at",
)

def attach_to_cycle(conn, account_id, *, cycle_id, mode, style, status, capital,
                    benchmark, nav_date, risk_profile):
    """把账户挂到活动周期并初始化资金/基准/日初净值（首次挂接路径）。"""
    conn.execute(
        """UPDATE paper_accounts SET cycle_id=?,mode=?,style=?,status=?,initial_cash=?,
             cash=?,benchmark_start=?,daily_start_nav=?,daily_nav_date=?,risk_profile=?,
             params=COALESCE(NULLIF(params,''),'{}') WHERE id=?""",
        (cycle_id, mode, style, status, capital, capital, benchmark, capital,
         nav_date, risk_profile, account_id),
    )


def set_cycle_profile(conn, account_id, *, mode, style, risk_profile):
    """已挂接账户的周期画像字段（mode/style/risk_profile）；参数为空时兜底 '{}'。"""
    conn.execute(
        "UPDATE paper_accounts SET mode=?,style=?,risk_profile=?,"
        "params=COALESCE(NULLIF(params,''),'{}') WHERE id=?",
        (mode, style, risk_profile, account_id),
    )


def activate_for_running_cycle(conn, account_id, *, cycle_id, capital, nav_date, now):
    """无成交记录的新策略跟随运行中的周期直接激活。"""
    conn.execute(
        """UPDATE paper_accounts SET cycle_id=?,status='running',initial_cash=?,cash=?,
             daily_start_nav=?,daily_nav_date=?,updated_at=? WHERE id=?""",
        (cycle_id, capital, capital, capital, nav_date, now, account_id),
    )


def set_daily_start_nav(conn, account_id, *, nav, nav_date):
    """回填日初净值与对应日期。"""
    conn.execute(
        "UPDATE paper_accounts SET daily_start_nav=?,daily_nav_date=? WHERE id=?",
        (nav, nav_date, account_id),
    )


def apply_cycle_membership(conn, account_id, *, fields):
    """周期创建时的账户成员配置写入；列集合受白名单约束。"""
    unknown = set(fields) - set(CYCLE_MEMBERSHIP_COLUMNS)
    if unknown:
        raise ValueError(f"未知账户列：{sorted(unknown)}")
    columns = [name for name in CYCLE_MEMBERSHIP_COLUMNS if name in fields]
    conn.execute(
        f"UPDATE paper_accounts SET {','.join(f'{c}=?' for c in columns)} WHERE id=?",
        tuple(fields[name] for name in columns) + (account_id,),
    )
