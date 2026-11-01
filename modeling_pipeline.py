"""Reproducible PEK airport-hour delay models.

Model A is the weekday-hour historical median. Model B keeps that time
foundation fixed and adds a shrunk residual correction driven only by lagged
Dst-change and ROTI. The target is the mean departure delay of all valid PEK
departures scheduled in [T,T+1h).
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parent
if (BASE / ".deps").exists():
    sys.path.insert(0, str(BASE / ".deps"))

import joblib
import numpy as np
import pandas as pd


DATA = BASE / "data"
RESULTS = BASE / "results"
AIRPORTS = {"PEK": "北京首都国际机场"}
AIRPORT = "PEK"
TIMEZONE = "Asia/Shanghai"
WINDOW_MINUTES = 60
MIN_FLIGHTS_PER_WINDOW = 5
ASSET_SCHEMA_VERSION = 4
TARGET = "actual_mean_delay"
TIME_FEATURES = [
    "month", "weekday", "hour", "sin_doy", "cos_doy", "sin_hour", "cos_hour"
]
SPACE_FEATURES = ["dst_rate_lag", "roti_lag"]
SOURCE_FOLDER = "DT_Dst_ROTI_20260929"
SOURCE_FILES = {
    "flights_peK_raw_cache.pkl": "7EAEB843AA7ABFB73699C7BF32789D3A3C7400293616CC7901AF77195FE1D556",
    "dst_hourly.csv": "60BBB1EF5B86459F5684BDA8107826BE79247578D48E1574EFEA21DE509B0680",
    "roti_hourly.csv": "9687EB4E65E011984304B9DFEA0DD862291AD9FA17A603AB7F4696DD2B9F14E8",
}
ROLLING_FOLDS = [
    ("F1", "2017-01-01", "2017-07-01", "2017-10-01"),
    ("F2", "2017-01-01", "2017-10-01", "2018-01-01"),
    ("F3", "2017-01-01", "2018-01-01", "2018-04-01"),
    ("F4", "2017-01-01", "2018-04-01", "2018-07-01"),
]
PARAM_GRID = [
    {
        "learning_rate": learning_rate,
        "max_iter": max_iter,
        "max_leaf_nodes": max_leaf_nodes,
        "min_samples_leaf": min_samples_leaf,
        "l2_regularization": 30.0,
    }
    for learning_rate, max_iter, max_leaf_nodes, min_samples_leaf in itertools.product(
        (0.03, 0.05), (80, 160), (7, 15), (80, 160)
    )
]
RESIDUAL_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def discover_source(explicit: str | Path | None = None) -> Path:
    """Find the immutable source folder without depending on nesting depth."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("MODEL_SOURCE_DIR"):
        candidates.append(Path(os.environ["MODEL_SOURCE_DIR"]))
    candidates.extend([
        BASE.parent / "outputs" / SOURCE_FOLDER,
        BASE.parents[1] / "outputs" / SOURCE_FOLDER,
        BASE / "outputs" / SOURCE_FOLDER,
    ])
    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if all((resolved / name).is_file() for name in SOURCE_FILES):
            return resolved
    checked = "\n".join(f"- {path}" for path in seen)
    raise FileNotFoundError(
        "找不到重新建模所需的三个只读源文件。可设置 MODEL_SOURCE_DIR。已检查：\n" + checked
    )


SOURCE = discover_source()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_sources(source: Path) -> dict[str, dict[str, object]]:
    snapshot = {}
    for name, expected in SOURCE_FILES.items():
        path = source / name
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"源文件哈希变化，停止训练：{path}\n期望 {expected}\n实际 {actual}")
        snapshot[name] = {"path": str(path), "bytes": path.stat().st_size, "sha256": actual}
    return snapshot


def features(frame: pd.DataFrame, roti_feature: str = "roti_lag") -> pd.DataFrame:
    """Create features available before an airport-hour begins."""
    start = pd.to_datetime(frame["window_start"])
    x = pd.DataFrame(index=frame.index)
    x["month"] = start.dt.month - 1
    x["weekday"] = start.dt.dayofweek
    x["hour"] = start.dt.hour
    x["sin_doy"] = np.sin(2 * np.pi * start.dt.dayofyear / 365.25)
    x["cos_doy"] = np.cos(2 * np.pi * start.dt.dayofyear / 365.25)
    x["sin_hour"] = np.sin(2 * np.pi * start.dt.hour / 24)
    x["cos_hour"] = np.cos(2 * np.pi * start.dt.hour / 24)
    x["dst_rate_lag"] = frame.get("dst_rate_lag", np.nan)
    x["roti_lag"] = frame.get(roti_feature, np.nan)
    return x.astype(float)


def finite_quantile(errors, coverage: float = 0.8) -> float:
    values = np.sort(np.asarray(errors, dtype=float))
    values = values[np.isfinite(values)]
    if not len(values):
        raise ValueError("校准集没有可用的机场小时窗口。")
    index = min(len(values) - 1, int(np.ceil((len(values) + 1) * coverage)) - 1)
    return float(values[index])


def score(y, prediction) -> dict[str, float | int]:
    error = np.asarray(prediction, dtype=float) - np.asarray(y, dtype=float)
    if not len(error) or not np.isfinite(error).all():
        raise ValueError("评分输入为空或包含非有限预测。")
    return {
        "mae": float(np.abs(error).mean()),
        "rmse": float(np.sqrt(np.square(error).mean())),
        "bias": float(error.mean()),
        "n": int(len(error)),
    }


def physical_departures(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Clean PEK departures and collapse identifiable physical duplicates."""
    airport = raw[raw.origin.eq(AIRPORT)].copy()
    cancelled = airport.status.astype("string").str.contains("取消", na=False)
    valid = (~cancelled) & airport.scheduled.notna() & airport.delta_min.notna()
    valid &= airport.delta_min.ge(-60)
    departures = airport[valid].copy()

    registration = departures.registration.astype("string").fillna("").str.strip()
    has_registration = registration.ne("") & ~registration.str.lower().isin(["nan", "none", "null", "-"])
    schedule_key = departures.scheduled.dt.strftime("%Y-%m-%dT%H:%M")
    flight_no = departures.flight_no.astype("string").fillna("UNKNOWN")
    departures["_physical_key"] = np.where(
        has_registration,
        "REG|" + registration + "|" + schedule_key,
        "FLT|" + flight_no + "|" + schedule_key,
    )
    before_dedup = len(departures)
    departures = departures.drop_duplicates("_physical_key", keep="first")
    audit = {
        "source_records": int(len(airport)),
        "cancelled_records": int(cancelled.sum()),
        "invalid_or_early_outlier_records": int((~cancelled).sum() - before_dedup),
        "duplicate_operational_records_removed": int(before_dedup - len(departures)),
        "valid_physical_departures": int(len(departures)),
    }
    return departures, audit


def airport_hours(departures: pd.DataFrame) -> pd.DataFrame:
    """Aggregate departures into left-closed, right-open local clock hours."""
    work = departures.copy()
    work["window_start"] = work.scheduled.dt.floor("h")
    hourly = work.groupby("window_start", as_index=False).agg(
        actual_mean_delay=("delta_min", "mean"),
        actual_median_delay=("delta_min", "median"),
        flight_count=("delta_min", "size"),
        delayed_flights=("delta_min", lambda values: int(values.gt(15).sum())),
        delay15_rate=("delta_min", lambda values: float(values.gt(15).mean())),
    )
    hourly["airport"] = AIRPORT
    hourly["window_end"] = hourly.window_start + pd.Timedelta(hours=1)
    return hourly


def prepare_hourly(source: Path | None = None) -> tuple[pd.DataFrame, dict[str, object]]:
    source = SOURCE if source is None else Path(source).resolve()
    source_snapshot = verify_sources(source)
    raw = pd.read_pickle(source / "flights_peK_raw_cache.pkl")
    departures, audit = physical_departures(raw)
    all_hours = airport_hours(departures)
    audit["all_observed_hours"] = int(len(all_hours))
    audit["hours_below_minimum_flights"] = int(
        all_hours.flight_count.lt(MIN_FLIGHTS_PER_WINDOW).sum()
    )
    hourly = all_hours[all_hours.flight_count.ge(MIN_FLIGHTS_PER_WINDOW)].copy()

    dst = pd.read_csv(source / "dst_hourly.csv", parse_dates=["hour_utc"])[
        ["hour_utc", "abs_dDst_dt_nT_per_h"]
    ]
    roti = pd.read_csv(source / "roti_hourly.csv", parse_dates=["hour_utc"])[
        ["hour_utc", "ROTI_TECU_per_min"]
    ]
    if dst.hour_utc.duplicated().any() or roti.hour_utc.duplicated().any():
        raise ValueError("空间天气小时键不是唯一值。")

    # Current hypothesis: raw TEC timestamps are UTC. T_local-10h is UTC T-2h.
    hourly["feature_hour_utc"] = hourly.window_start - pd.Timedelta(hours=10)
    hourly = hourly.merge(
        dst.rename(columns={
            "hour_utc": "feature_hour_utc",
            "abs_dDst_dt_nT_per_h": "dst_rate_lag",
        }),
        on="feature_hour_utc", how="left", validate="many_to_one",
    )
    hourly = hourly.merge(
        roti.rename(columns={
            "hour_utc": "feature_hour_utc",
            "ROTI_TECU_per_min": "roti_lag",
        }),
        on="feature_hour_utc", how="left", validate="many_to_one",
    )

    # Sensitivity only: if raw TEC timestamps were Beijing time, the matching key is +8h.
    hourly["feature_hour_roti_local_hypothesis"] = hourly.window_start - pd.Timedelta(hours=2)
    hourly = hourly.merge(
        roti.rename(columns={
            "hour_utc": "feature_hour_roti_local_hypothesis",
            "ROTI_TECU_per_min": "roti_lag_local_hypothesis",
        }),
        on="feature_hour_roti_local_hypothesis", how="left", validate="many_to_one",
    )
    hourly = hourly.sort_values("window_start").reset_index(drop=True)
    audit["valid_windows"] = int(len(hourly))
    audit["valid_departures_in_windows"] = int(hourly.flight_count.sum())
    audit["missing_dst_windows"] = int(hourly.dst_rate_lag.isna().sum())
    audit["missing_roti_utc_hypothesis_windows"] = int(hourly.roti_lag.isna().sum())
    audit["missing_roti_local_hypothesis_windows"] = int(
        hourly.roti_lag_local_hypothesis.isna().sum()
    )
    return hourly, {"source": source_snapshot, "audit": audit}


def split_masks(hourly: pd.DataFrame) -> dict[str, np.ndarray]:
    start = hourly.window_start
    return {
        "train": start.lt("2018-07-01").to_numpy(),
        "validation": (start.ge("2018-07-01") & start.lt("2018-10-01")).to_numpy(),
        "calibration": (start.ge("2018-10-01") & start.lt("2019-01-01")).to_numpy(),
        "test": start.ge("2019-01-01").to_numpy(),
    }


def rolling_masks(hourly: pd.DataFrame):
    start = hourly.window_start
    for name, train_start, validation_start, validation_end in ROLLING_FOLDS:
        train_mask = (start.ge(train_start) & start.lt(validation_start)).to_numpy()
        validation_mask = (start.ge(validation_start) & start.lt(validation_end)).to_numpy()
        yield name, train_mask, validation_mask


def fit_time_prior(hourly: pd.DataFrame, train_mask) -> dict[str, object]:
    train = hourly.loc[np.asarray(train_mask, dtype=bool)].copy()
    if train.empty:
        raise ValueError("时间先验训练段为空。")
    train["weekday"] = train.window_start.dt.dayofweek
    train["hour"] = train.window_start.dt.hour
    medians = train.groupby(["weekday", "hour"])[TARGET].median()
    counts = train.groupby(["weekday", "hour"]).flight_count.median()
    support = train.groupby(["weekday", "hour"]).size()
    return {
        "values": {f"{int(d)}:{int(h)}": float(v) for (d, h), v in medians.items()},
        "fallback": float(train[TARGET].median()),
        "expected_counts": {f"{int(d)}:{int(h)}": float(v) for (d, h), v in counts.items()},
        "count_fallback": float(train.flight_count.median()),
        "support_windows": {f"{int(d)}:{int(h)}": int(v) for (d, h), v in support.items()},
    }


def predict_time_prior(frame: pd.DataFrame, prior: dict[str, object]) -> np.ndarray:
    values = prior["values"]
    fallback = float(prior["fallback"])
    return np.asarray([
        values.get(f"{stamp.dayofweek}:{stamp.hour}", fallback)
        for stamp in pd.to_datetime(frame.window_start)
    ], dtype=float)


def make_model(params: dict[str, object]):
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        loss="absolute_error",
        learning_rate=float(params["learning_rate"]),
        max_iter=int(params["max_iter"]),
        max_leaf_nodes=int(params["max_leaf_nodes"]),
        min_samples_leaf=int(params["min_samples_leaf"]),
        l2_regularization=float(params["l2_regularization"]),
        early_stopping=False,
        random_state=42,
    )


def fit_model_pair(
    hourly: pd.DataFrame,
    train_mask,
    params: dict[str, object],
    roti_feature: str = "roti_lag",
) -> dict[str, object]:
    train_mask = np.asarray(train_mask, dtype=bool)
    prior = fit_time_prior(hourly, train_mask)
    prior_all = predict_time_prior(hourly, prior)
    residual = hourly[TARGET].to_numpy(dtype=float) - prior_all
    x = features(hourly, roti_feature=roti_feature)
    model_b = make_model(params)
    model_b.fit(x.loc[train_mask, SPACE_FEATURES], residual[train_mask])
    residual_weight = float(params["residual_weight"])
    pred_a = prior_all.copy()
    pred_b_raw = prior_all + residual_weight * model_b.predict(x[SPACE_FEATURES])
    have_space = x[SPACE_FEATURES].notna().all(axis=1).to_numpy()
    pred_b_op = np.where(have_space, pred_b_raw, pred_a)
    return {
        "prior": prior,
        "model_b": model_b,
        "residual_weight": residual_weight,
        "prior_prediction": prior_all,
        "prediction_a": pred_a,
        "prediction_b_raw": pred_b_raw,
        "prediction_b_op": pred_b_op,
        "have_space": have_space,
    }


def select_grid_row(summary: pd.DataFrame) -> pd.Series:
    """Apply the frozen 1e-9 tie rule, then prefer the simpler configuration."""
    ranked = summary.copy()
    ranked["complexity"] = ranked.max_iter * ranked.max_leaf_nodes
    ranked["alpha_zero_rank"] = ranked.residual_weight.ne(0).astype(int)
    best_mae = float(ranked.mean_cv_mae.min())
    tied = ranked[ranked.mean_cv_mae.le(best_mae + 1e-9)].copy()
    return tied.sort_values(
        [
            "alpha_zero_rank", "complexity", "residual_weight",
            "learning_rate", "min_samples_leaf", "config_id",
        ],
        ascending=[True, True, True, True, False, True],
        kind="mergesort",
    ).iloc[0]


def grid_search_time_model(hourly: pd.DataFrame) -> tuple[dict[str, object], pd.DataFrame, pd.DataFrame]:
    x = features(hourly)
    y = hourly[TARGET].to_numpy(dtype=float)
    rows = []
    for tree_config_id, params in enumerate(PARAM_GRID, start=1):
        for fold_name, train_mask, validation_mask in rolling_masks(hourly):
            prior = fit_time_prior(hourly, train_mask)
            prior_all = predict_time_prior(hourly, prior)
            residual = y - prior_all
            model = make_model(params)
            model.fit(x.loc[train_mask, SPACE_FEATURES], residual[train_mask])
            correction = model.predict(x.loc[validation_mask, SPACE_FEATURES])
            have_space = x.loc[validation_mask, SPACE_FEATURES].notna().all(axis=1).to_numpy()
            for alpha_index, residual_weight in enumerate(RESIDUAL_WEIGHTS, start=1):
                config_id = (tree_config_id - 1) * len(RESIDUAL_WEIGHTS) + alpha_index
                prediction = prior_all[validation_mask] + residual_weight * correction
                prediction = np.where(have_space, prediction, prior_all[validation_mask])
                metrics = score(y[validation_mask], prediction)
                rows.append({
                    "config_id": config_id,
                    "tree_config_id": tree_config_id,
                    "fold": fold_name,
                    **params,
                    "residual_weight": residual_weight,
                    "train_windows": int(train_mask.sum()),
                    "validation_windows": int(validation_mask.sum()),
                    "space_complete_windows": int(have_space.sum()),
                    **metrics,
                })
    folds = pd.DataFrame(rows)
    group_columns = [
        "config_id", "tree_config_id", "learning_rate", "max_iter",
        "max_leaf_nodes", "min_samples_leaf", "l2_regularization", "residual_weight",
    ]
    summary = folds.groupby(group_columns, as_index=False).agg(
        mean_cv_mae=("mae", "mean"),
        std_cv_mae=("mae", lambda values: float(np.std(values, ddof=0))),
    )
    selected_row = select_grid_row(summary)
    selected = {
        name: (int(selected_row[name]) if name in {"max_iter", "max_leaf_nodes", "min_samples_leaf"}
               else float(selected_row[name]))
        for name in [
            "learning_rate", "max_iter", "max_leaf_nodes", "min_samples_leaf",
            "l2_regularization", "residual_weight",
        ]
    }
    return selected, folds, summary


def evaluate_selected_cv(hourly: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    rows = []
    y = hourly[TARGET].to_numpy(dtype=float)
    for fold_name, train_mask, validation_mask in rolling_masks(hourly):
        fitted = fit_model_pair(hourly, train_mask, params)
        metrics_a = score(y[validation_mask], fitted["prediction_a"][validation_mask])
        metrics_b = score(y[validation_mask], fitted["prediction_b_op"][validation_mask])
        rows.append({
            "fold": fold_name,
            "train_windows": int(train_mask.sum()),
            "validation_windows": int(validation_mask.sum()),
            "model_a_mae": metrics_a["mae"],
            "model_b_mae": metrics_b["mae"],
            "b_relative_improvement": (metrics_a["mae"] - metrics_b["mae"]) / metrics_a["mae"],
            "model_a_bias": metrics_a["bias"],
            "model_b_bias": metrics_b["bias"],
            "space_complete_windows": int(fitted["have_space"][validation_mask].sum()),
            "b_strictly_better": bool(metrics_b["mae"] < metrics_a["mae"]),
        })
    return pd.DataFrame(rows)


def paired_daily_block_bootstrap(
    window_start,
    y,
    prediction_a,
    prediction_b,
    samples: int = 5000,
    seed: int = 42,
) -> dict[str, float | int | str]:
    work = pd.DataFrame({
        "window_start": pd.to_datetime(window_start),
        "error_a": np.abs(np.asarray(prediction_a, dtype=float) - np.asarray(y, dtype=float)),
        "error_b": np.abs(np.asarray(prediction_b, dtype=float) - np.asarray(y, dtype=float)),
    })
    work["day"] = work.window_start.dt.floor("D")
    grouped = work.groupby("day").agg(
        sum_a=("error_a", "sum"), sum_b=("error_b", "sum"), count=("error_a", "size")
    )
    if grouped.empty:
        raise ValueError("Bootstrap 阶段没有自然日。")
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(grouped), size=(int(samples), len(grouped)))
    sum_a = grouped.sum_a.to_numpy()[draws].sum(axis=1)
    sum_b = grouped.sum_b.to_numpy()[draws].sum(axis=1)
    count = grouped["count"].to_numpy()[draws].sum(axis=1)
    differences = sum_b / count - sum_a / count
    lower, upper = np.quantile(differences, [0.025, 0.975])
    point = float(work.error_b.mean() - work.error_a.mean())
    return {
        "direction": "mae_b_minus_mae_a",
        "point": point,
        "lower": float(lower),
        "upper": float(upper),
        "samples": int(samples),
        "seed": int(seed),
        "block": "local_calendar_day",
        "days": int(len(grouped)),
    }


def _stage_metrics(hourly, mask, prediction_a, prediction_b, prediction_simple):
    y = hourly.loc[mask, TARGET]
    return {
        "baseline": score(y, prediction_a[mask]),
        "space": score(y, prediction_b[mask]),
        "seasonal": score(y, prediction_simple[mask]),
    }


def _json_ready(value):
    if isinstance(value, dict):
        return {str(key): _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def smoke(source: Path | None = None) -> dict[str, object]:
    """Run one real-input fold through preprocessing and both model branches."""
    hourly, data_snapshot = prepare_hourly(source)
    fold_name, train_mask, validation_mask = next(rolling_masks(hourly))
    params = {**PARAM_GRID[0], "residual_weight": 0.75}
    fitted = fit_model_pair(hourly, train_mask, params)
    y = hourly[TARGET].to_numpy(dtype=float)
    metrics_a = score(y[validation_mask], fitted["prediction_a"][validation_mask])
    metrics_b = score(y[validation_mask], fitted["prediction_b_op"][validation_mask])
    train_times = hourly.loc[train_mask, "window_start"]
    validation_times = hourly.loc[validation_mask, "window_start"]
    time_order_ok = bool(train_times.max() < validation_times.min())
    feature_availability_ok = bool(
        (hourly.feature_hour_utc + pd.Timedelta(hours=9) <= hourly.window_start).all()
    )
    if not time_order_ok or not feature_availability_ok:
        raise AssertionError("时间折或空间天气特征可用性断言失败。")
    prior_support = int(sum(fitted["prior"]["support_windows"].values()))
    if prior_support != int(train_mask.sum()):
        raise AssertionError("时间先验使用的窗口数与折内训练窗口数不一致。")
    tie_probe = pd.DataFrame([
        {
            "config_id": 1, "mean_cv_mae": 10.0,
            "learning_rate": 0.05, "max_iter": 160, "max_leaf_nodes": 15,
            "min_samples_leaf": 80, "l2_regularization": 30.0, "residual_weight": 0.0,
        },
        {
            "config_id": 2, "mean_cv_mae": 10.0 + 5e-10,
            "learning_rate": 0.03, "max_iter": 80, "max_leaf_nodes": 7,
            "min_samples_leaf": 160, "l2_regularization": 30.0, "residual_weight": 0.25,
        },
    ])
    tie_rule_ok = int(select_grid_row(tie_probe)["config_id"]) == 1
    if not tie_rule_ok:
        raise AssertionError("1e-9 近似平局时未按 alpha=0 优先的字典序选择。")
    result = {
        "status": "ok",
        "mode": "smoke_real_input_single_fold",
        "airport": AIRPORT,
        "target": "PEK [T,T+1h) airport-hour mean departure delay",
        "unit": "minutes",
        "source": data_snapshot["source"],
        "audit": data_snapshot["audit"],
        "fold": fold_name,
        "train_windows": int(train_mask.sum()),
        "validation_windows": int(validation_mask.sum()),
        "params": params,
        "model_a": metrics_a,
        "model_b_operational": metrics_b,
        "space_complete_validation_windows": int(fitted["have_space"][validation_mask].sum()),
        "space_missing_fallback_windows": int((~fitted["have_space"][validation_mask]).sum()),
        "time_prior_fitted_only_on_fold_train": bool(prior_support == int(train_mask.sum())),
        "time_order_asserted": time_order_ok,
        "feature_availability_asserted": feature_availability_ok,
        "grid_tie_rule_asserted": tie_rule_ok,
        "space_feature_availability": "T_local-10h; completed at least one hour before T",
        "roti_timezone_status": "unconfirmed_assumed_utc",
        "max_abs_prediction_difference_a_vs_b": float(np.max(np.abs(
            fitted["prediction_a"][validation_mask] - fitted["prediction_b_op"][validation_mask]
        ))),
        "prediction_finite": bool(
            np.isfinite(fitted["prediction_a"][validation_mask]).all()
            and np.isfinite(fitted["prediction_b_op"][validation_mask]).all()
        ),
    }
    RESULTS.mkdir(exist_ok=True)
    output = RESULTS / "smoke_result.json"
    output.write_text(json.dumps(_json_ready(result), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(_json_ready(result), ensure_ascii=False, indent=2), flush=True)
    return result


def train(
    source: Path | None = None,
    roti_timezone_status: str = "unconfirmed_assumed_utc",
) -> dict[str, object]:
    """Run the frozen full experiment and write versioned model assets."""
    DATA.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    hourly, data_snapshot = prepare_hourly(source)
    masks = split_masks(hourly)
    y = hourly[TARGET].to_numpy(dtype=float)

    selected_params, grid_folds, grid_summary = grid_search_time_model(hourly)
    selected_cv = evaluate_selected_cv(hourly, selected_params)
    fitted = fit_model_pair(hourly, masks["train"], selected_params)

    # Timezone sensitivity: same prior, A and parameters; only the ROTI timestamp hypothesis changes.
    x_local = features(hourly, roti_feature="roti_lag_local_hypothesis")
    prior_prediction = fitted["prior_prediction"]
    residual = y - prior_prediction
    local_space_model = make_model(selected_params)
    local_space_model.fit(
        x_local.loc[masks["train"], SPACE_FEATURES],
        residual[masks["train"]],
    )
    local_raw = prior_prediction + float(selected_params["residual_weight"]) * local_space_model.predict(
        x_local[SPACE_FEATURES]
    )
    local_complete = x_local[SPACE_FEATURES].notna().all(axis=1).to_numpy()
    local_op = np.where(local_complete, local_raw, fitted["prediction_a"])

    validation = _stage_metrics(
        hourly, masks["validation"], fitted["prediction_a"], fitted["prediction_b_op"],
        fitted["prior_prediction"],
    )
    validation_bootstrap = paired_daily_block_bootstrap(
        hourly.loc[masks["validation"], "window_start"],
        y[masks["validation"]],
        fitted["prediction_a"][masks["validation"]],
        fitted["prediction_b_op"][masks["validation"]],
    )
    validation_improvement = (
        validation["baseline"]["mae"] - validation["space"]["mae"]
    ) / validation["baseline"]["mae"]
    cv_wins = int(selected_cv.b_strictly_better.sum())
    timezone_confirmed = roti_timezone_status == "confirmed_utc"
    internal_screening_gates = {
        "relative_improvement_at_least_1pct": bool(validation_improvement >= 0.01),
        "cv_wins_at_least_3_of_4": bool(cv_wins >= 3),
        "validation_bootstrap_upper_below_zero": bool(validation_bootstrap["upper"] < 0),
        "absolute_bias_not_worse_by_more_than_1min": bool(
            abs(validation["space"]["bias"]) <= abs(validation["baseline"]["bias"]) + 1.0
        ),
        "no_future_information": True,
    }
    internal_screening_pass = all(internal_screening_gates.values())

    q_a = finite_quantile(
        np.abs(y[masks["calibration"]] - fitted["prediction_a"][masks["calibration"]])
    )
    q_b = finite_quantile(
        np.abs(y[masks["calibration"]] - fitted["prediction_b_op"][masks["calibration"]])
    )
    alpha_zero_collapse_asserted = None
    if float(selected_params["residual_weight"]) == 0.0:
        predictions_equal = bool(np.array_equal(fitted["prediction_a"], fitted["prediction_b_op"]))
        radii_equal = bool(q_a == q_b)
        alpha_zero_collapse_asserted = predictions_equal and radii_equal
        if not alpha_zero_collapse_asserted:
            raise AssertionError("alpha=0 时模型 B 未严格退化为模型 A。")

    test_a = score(y[masks["test"]], fitted["prediction_a"][masks["test"]])
    test_b = score(y[masks["test"]], fitted["prediction_b_op"][masks["test"]])
    test_simple = score(y[masks["test"]], fitted["prior_prediction"][masks["test"]])
    test_bootstrap = paired_daily_block_bootstrap(
        hourly.loc[masks["test"], "window_start"],
        y[masks["test"]],
        fitted["prediction_a"][masks["test"]],
        fitted["prediction_b_op"][masks["test"]],
    )
    test_gates = {
        "model_b_mae_below_model_a": bool(test_b["mae"] < test_a["mae"]),
        "test_bootstrap_upper_below_zero": bool(test_bootstrap["upper"] < 0),
    }
    fresh_confirmation_available = False
    production_gates = {
        "internal_screening_pass": bool(internal_screening_pass),
        "historical_holdout_screen_pass": bool(all(test_gates.values())),
        "roti_timezone_confirmed_utc": bool(timezone_confirmed),
        "fresh_confirmation_available": bool(fresh_confirmation_available),
    }
    selected = "space" if all(production_gates.values()) else "baseline"

    have_space = fitted["have_space"]
    if selected == "space":
        active = fitted["prediction_b_op"]
        radius = np.where(have_space, q_b, q_a)
    else:
        active = fitted["prediction_a"]
        radius = np.full(len(hourly), q_a, dtype=float)
    hourly["predicted_mean_delay"] = active
    hourly["baseline_model_mean_delay"] = fitted["prediction_a"]
    hourly["space_model_mean_delay"] = fitted["prediction_b_op"]
    hourly["space_model_raw_mean_delay"] = fitted["prediction_b_raw"]
    hourly["simple_baseline_mean_delay"] = fitted["prior_prediction"]
    hourly["lower"] = active - radius
    hourly["upper"] = active + radius

    test_score = score(y[masks["test"]], active[masks["test"]])
    test_coverage = float(
        hourly.loc[masks["test"], TARGET].between(
            hourly.loc[masks["test"], "lower"], hourly.loc[masks["test"], "upper"]
        ).mean()
    )
    calibration_a_coverage = float(
        np.mean(
            np.abs(y[masks["calibration"]] - fitted["prediction_a"][masks["calibration"]]) <= q_a
        )
    )
    calibration_b_coverage = float(
        np.mean(
            np.abs(y[masks["calibration"]] - fitted["prediction_b_op"][masks["calibration"]]) <= q_b
        )
    )

    timezone_sensitivity = {
        "utc_hypothesis": {
            "validation": validation["space"],
            "test": test_b,
            "complete_windows": int(fitted["have_space"].sum()),
        },
        "beijing_time_hypothesis": {
            "validation": score(y[masks["validation"]], local_op[masks["validation"]]),
            "test": score(y[masks["test"]], local_op[masks["test"]]),
            "complete_windows": int(local_complete.sum()),
        },
        "selection_prohibited": True,
        "note": "TEC 文件头时区未确认；敏感性结果不得用于事后择优选择时区。",
    }

    asset_id = "pek-hourly-v4-" + pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    test_frame = hourly.loc[masks["test"]]
    airport_metrics = [{
        "code": AIRPORT,
        "name": AIRPORTS[AIRPORT],
        "window_minutes": WINDOW_MINUTES,
        "test_windows": int(len(test_frame)),
        "test_flights": int(test_frame.flight_count.sum()),
        "mean_flights_per_window": float(test_frame.flight_count.mean()),
        "mean_actual_delay": float(test_frame[TARGET].mean()),
        "mae": test_score["mae"],
        "rmse": test_score["rmse"],
        "bias": test_score["bias"],
        "baseline_model_mae": test_a["mae"],
        "space_model_mae": test_b["mae"],
        "simple_baseline_mae": test_simple["mae"],
        "relative_improvement_vs_simple": float(
            (test_simple["mae"] - test_score["mae"]) / test_simple["mae"]
        ),
        "coverage80": test_coverage,
        "radius80_model_a": q_a,
        "radius80_model_b": q_b,
    }]
    split_dates = {
        "train": ["2017-01-01", "2018-06-30", int(masks["train"].sum())],
        "validation": ["2018-07-01", "2018-09-30", int(masks["validation"].sum())],
        "calibration": ["2018-10-01", "2018-12-31", int(masks["calibration"].sum())],
        "test": ["2019-01-01", "2019-12-31", int(masks["test"].sum())],
    }
    meta = {
        "asset_id": asset_id,
        "schema_version": ASSET_SCHEMA_VERSION,
        "prediction_unit": "airport_hour",
        "interval_unit": "airport_hour",
        "primary_metric": "unweighted_hour_mae",
        "timezone": TIMEZONE,
        "product_scope": "airport_hour_average_departure_delay",
        "target": "PEK 计划离港窗口 [T, T+1h) 内有效出港航班的平均离港延误（分钟）",
        "target_definition": "离港延误 = 实际出发时间 − 计划出发时间；提前出发保留负值。",
        "airport": AIRPORT,
        "horizon_minutes": WINDOW_MINUTES,
        "minimum_flights_per_window": MIN_FLIGHTS_PER_WINDOW,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "valid_windows": int(len(hourly)),
        "valid_departures": int(hourly.flight_count.sum()),
        "source_snapshot": data_snapshot["source"],
        "audit": data_snapshot["audit"],
        "splits": split_dates,
        "rolling_folds": selected_cv.to_dict(orient="records"),
        "selected_hyperparameters": selected_params,
        "alpha_zero_collapse_asserted": alpha_zero_collapse_asserted,
        "model_design": {
            "model_a": "weekday-hour historical median time model",
            "model_b": "model A + shrunk HGB residual correction using only lagged Dst-change and ROTI",
            "time_features": ["weekday", "hour"],
            "space_features": SPACE_FEATURES,
            "residual_weight": float(selected_params["residual_weight"]),
            "space_feature_hour": "T_local - 10h under UTC hypothesis",
            "missing_space_rule": "model B operational prediction equals model A",
        },
        "roti_timezone_status": roti_timezone_status,
        "timezone_sensitivity": timezone_sensitivity,
        "selected": selected,
        "selected_reason": (
            "模型 B 通过全部生产门槛。"
            if selected == "space"
            else "ROTI 时区未确认且缺少全新确认段，生产主模型固定为 A；B 仅作历史研究对照。"
        ),
        "validation": validation,
        "validation_relative_improvement": float(validation_improvement),
        "validation_bootstrap": validation_bootstrap,
        "validation_gates": internal_screening_gates,
        "validation_candidate": bool(internal_screening_pass),
        "internal_screening_gates": internal_screening_gates,
        "internal_screening_pass": bool(internal_screening_pass),
        "historical_holdout_gates": test_gates,
        "production_gates": production_gates,
        "fresh_confirmation_available": bool(fresh_confirmation_available),
        "test": test_score,
        "baseline_model_test": test_a,
        "space_model_test": test_b,
        "simple_baseline_test": test_simple,
        "test_bootstrap": test_bootstrap,
        "interval_calibration": {
            "nominal_coverage": 0.8,
            "model_a_radius": q_a,
            "model_b_operational_radius": q_b,
            "model_a_calibration_coverage": calibration_a_coverage,
            "model_b_calibration_coverage": calibration_b_coverage,
            "test_selected_model_coverage": test_coverage,
        },
        "airports": airport_metrics,
        "comparison_note": "所有误差按机场一小时窗口等权；模型 B 缺测窗口逐窗口回退模型 A。",
        "horizon_note": "主目标固定为 1 小时机场平均延误，不能与单航班、航线或 3 小时平均值混用。",
        "limitations": [
            "当前航班数据只支持北京首都机场 PEK 出港，不能外推为任意机场或某条航线。",
            "2018 Q3 与 2019 指标已参与旧项目或本轮设计认识，只能作为历史开发/留出证据。",
            "不包含地面天气、机场流量实况、跑道构型和前序航班；不预测取消。",
            "TEC 文件头时区尚未经数据提供者确认，因此模型 B 当前只能作为研究对照。",
            "缺少一个从未用于设计或结果查看的全新确认时间段，模型 B 当前不能生产启用。",
            "观察性预测增益不等于空间天气对延误的因果效应。",
        ],
    }
    bundle = {
        "asset_id": asset_id,
        "schema_version": ASSET_SCHEMA_VERSION,
        "models": {
            "baseline": None,
            "space": fitted["model_b"],
            "space_local_hypothesis": local_space_model,
        },
        "selected": selected,
        "fallback_model": "baseline",
        "q_a": q_a,
        "q_b": q_b,
        "radius": q_b if selected == "space" else q_a,
        "fallback_radius": q_a,
        "seasonal": fitted["prior"]["values"],
        "seasonal_fallback": fitted["prior"]["fallback"],
        "expected_counts": fitted["prior"]["expected_counts"],
        "count_fallback": fitted["prior"]["count_fallback"],
        "support_windows": fitted["prior"]["support_windows"],
        "airport": AIRPORT,
        "horizon_minutes": WINDOW_MINUTES,
        "timezone": TIMEZONE,
        "selected_hyperparameters": selected_params,
        "time_features": ["weekday", "hour"],
        "space_features": SPACE_FEATURES,
        "residual_weight": float(selected_params["residual_weight"]),
        "fresh_confirmation_available": bool(fresh_confirmation_available),
        "roti_timezone_status": roti_timezone_status,
    }

    joblib.dump(bundle, DATA / "model.joblib")
    hourly["asset_id"] = asset_id
    hourly["schema_version"] = ASSET_SCHEMA_VERSION
    output_columns = [
        "asset_id", "schema_version", "airport", "window_start", "window_end",
        "flight_count", "delayed_flights", "delay15_rate", "actual_mean_delay",
        "actual_median_delay", "predicted_mean_delay", "baseline_model_mean_delay",
        "space_model_mean_delay", "space_model_raw_mean_delay", "simple_baseline_mean_delay",
        "lower", "upper", "dst_rate_lag", "roti_lag",
    ]
    hourly.loc[masks["test"], output_columns].to_pickle(DATA / "test_windows.pkl")
    (DATA / "metadata.json").write_text(
        json.dumps(_json_ready(meta), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    grid_folds.to_csv(RESULTS / "cv_grid_folds.csv", index=False, encoding="utf-8-sig")
    grid_summary.to_csv(RESULTS / "cv_grid_summary.csv", index=False, encoding="utf-8-sig")
    selected_cv.to_csv(RESULTS / "cv_selected_model_comparison.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([
        {"hypothesis": name, "stage": stage, **metrics}
        for name, detail in timezone_sensitivity.items() if isinstance(detail, dict)
        for stage, metrics in detail.items() if isinstance(metrics, dict) and "mae" in metrics
    ]).to_csv(RESULTS / "roti_timezone_sensitivity.csv", index=False, encoding="utf-8-sig")
    (RESULTS / "model_metrics.json").write_text(
        json.dumps(_json_ready(meta), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(_json_ready(meta), ensure_ascii=False, indent=2), flush=True)
    return meta


def estimate(bundle, airport, window_start, dst=None, roti=None):
    if airport != bundle["airport"]:
        raise ValueError("当前模型仅支持北京首都机场 PEK。")
    start = pd.Timestamp(window_start)
    if start.tzinfo is not None:
        start = start.tz_convert(TIMEZONE).tz_localize(None)
    start = start.floor("h")
    local_start = start.tz_localize(TIMEZONE)
    frame = pd.DataFrame([{"window_start": start, "dst_rate_lag": dst, "roti_lag": roti}])
    x = features(frame)
    key = f"{start.dayofweek}:{start.hour}"
    simple = float(bundle["seasonal"].get(key, bundle["seasonal_fallback"]))
    baseline = simple
    scenario = None
    if dst is not None and roti is not None:
        scenario = simple + float(bundle["residual_weight"]) * float(
            bundle["models"]["space"].predict(x[SPACE_FEATURES])[0]
        )
    model_name = "space" if bundle["selected"] == "space" and scenario is not None else "baseline"
    prediction = scenario if model_name == "space" else baseline
    interval_radius = float(bundle["q_b"] if model_name == "space" else bundle["q_a"])
    expected = float(bundle["expected_counts"].get(key, bundle["count_fallback"]))
    support = int(bundle["support_windows"].get(key, 0))
    return {
        "asset_id": bundle["asset_id"],
        "schema_version": bundle["schema_version"],
        "target": "mean_departure_delay_minutes",
        "scope": "airport_hour",
        "scope_note": "结果表示整个机场该一小时计划离港窗口的平均离港延误，不适用于单个航班或航线。",
        "airport": airport,
        "timezone": bundle["timezone"],
        "window_start": local_start.isoformat(),
        "window_end": (local_start + pd.Timedelta(hours=1)).isoformat(),
        "horizon_minutes": bundle["horizon_minutes"],
        "predicted_mean_delay_minutes": prediction,
        "baseline_model_mean_delay_minutes": baseline,
        "space_scenario_mean_delay_minutes": scenario,
        "simple_baseline_mean_delay_minutes": simple,
        "model_a_mean_delay_minutes": baseline,
        "model_b_mean_delay_minutes": scenario,
        "model": model_name,
        "lower_minutes": prediction - interval_radius,
        "upper_minutes": prediction + interval_radius,
        "nominal_coverage": 0.8,
        "expected_flights": expected,
        "historical_support_windows": support,
        "low_support": expected < MIN_FLIGHTS_PER_WINDOW or support < 20,
        "out_of_period": start.year > 2019,
        "roti_timezone_status": bundle.get("roti_timezone_status", "unknown"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="Run one real-input fold without full search.")
    mode.add_argument("--train", action="store_true", help="Run the frozen full experiment.")
    parser.add_argument("--source-dir", type=Path, default=None)
    parser.add_argument(
        "--roti-timezone-status",
        choices=["unconfirmed_assumed_utc", "confirmed_utc"],
        default="unconfirmed_assumed_utc",
    )
    args = parser.parse_args(argv)
    if args.smoke:
        smoke(args.source_dir)
    else:
        train(args.source_dir, roti_timezone_status=args.roti_timezone_status)


if __name__ == "__main__":
    main()
