"""
Logistic regression analysis: which TDA features predict solve success?

Runs per-strategy and aggregated models, with feature importance ranking
and recursive feature elimination (RFE) to find minimal predictive sets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from .comparison import FEATURE_NAMES


def _build_dataset(
    strategy_features: dict[str, dict[int, dict]],
    strategy_by_problem: dict[str, dict[int, dict]],
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray, list[str]]],
           tuple[np.ndarray, np.ndarray, list[str], list[str]]]:
    """Build feature matrices and label vectors.

    Returns:
        per_strategy: {strategy: (X, y, valid_features)}
        pooled:       (X, y, valid_features, strategy_labels)
    """
    per_strategy: dict[str, tuple[np.ndarray, np.ndarray, list[str]]] = {}
    all_X_rows: list[list[float]] = []
    all_y: list[int] = []
    all_strat_labels: list[str] = []

    for strategy in sorted(strategy_features.keys()):
        features_by_pid = strategy_features[strategy]
        problems = strategy_by_problem.get(strategy, {})

        rows: list[list[float]] = []
        labels: list[int] = []

        for pid in sorted(features_by_pid.keys()):
            feat = features_by_pid[pid]
            result = problems.get(pid, {})

            solver_ans = result.get("solver_answer")
            correct_sol = result.get("correct_solution")
            if correct_sol is None:
                continue  # can't label without ground truth

            solved = 1 if (solver_ans and solver_ans.upper() == correct_sol.upper()) else 0

            row = []
            skip = False
            for f in FEATURE_NAMES:
                val = feat.get(f)
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    skip = True
                    break
                row.append(float(val))

            if skip:
                continue

            rows.append(row)
            labels.append(solved)
            all_X_rows.append(row)
            all_y.append(solved)
            all_strat_labels.append(strategy)

        if rows:
            X = np.array(rows)
            y = np.array(labels)
            # Drop features with zero variance within this strategy
            valid_mask = X.std(axis=0) > 1e-12
            valid_features = [f for f, v in zip(FEATURE_NAMES, valid_mask) if v]
            X_valid = X[:, valid_mask]
            per_strategy[strategy] = (X_valid, y, valid_features)

    # Pooled across all strategies
    if all_X_rows:
        X_all = np.array(all_X_rows)
        y_all = np.array(all_y)
        valid_mask = X_all.std(axis=0) > 1e-12
        valid_features = [f for f, v in zip(FEATURE_NAMES, valid_mask) if v]
        X_all_valid = X_all[:, valid_mask]
        pooled = (X_all_valid, y_all, valid_features, all_strat_labels)
    else:
        pooled = (np.empty((0, 0)), np.array([]), [], [])

    return per_strategy, pooled


def _run_analysis(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    label: str,
    groups: np.ndarray | None = None,
) -> dict:
    """Run logistic regression with feature importance and RFE.

    Args:
        groups: optional group labels (e.g. problem IDs) for group-aware CV.
            When provided, uses GroupKFold so all instances for a problem
            land in the same fold, preventing leakage of problem difficulty.

    Returns a results dict with coefficients, importance ranking,
    RFE results, and cross-validated accuracy.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import (
        cross_val_score, StratifiedKFold, GroupKFold,
    )
    from sklearn.feature_selection import RFECV

    n_samples, n_features = X.shape
    n_pos = int(y.sum())
    n_neg = n_samples - n_pos

    result = {
        "label": label,
        "n_samples": n_samples,
        "n_positive": n_pos,
        "n_negative": n_neg,
        "n_features_input": n_features,
        "feature_names": feature_names,
    }

    # Need both classes and enough samples
    if n_pos < 2 or n_neg < 2:
        result["error"] = "Too few samples in one class for logistic regression"
        return result

    # Standardize
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Choose CV strategy: group-aware if groups provided, else stratified
    if groups is not None:
        n_groups = len(set(groups))
        n_folds = min(5, n_groups)
        if n_folds < 2:
            result["error"] = "Too few groups for cross-validation"
            return result
        cv = GroupKFold(n_splits=n_folds)
        result["cv_type"] = "GroupKFold"
        result["n_groups"] = n_groups
    else:
        n_folds = min(5, min(n_pos, n_neg))
        if n_folds < 2:
            result["error"] = "Too few samples per class for cross-validation"
            return result
        cv = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
        result["cv_type"] = "StratifiedKFold"

    lr = LogisticRegression(
        C=1.0, solver="lbfgs",
        class_weight="balanced",
        max_iter=1000, random_state=42,
    )
    cv_kwargs = {"groups": groups} if groups is not None else {}
    scores = cross_val_score(lr, X_scaled, y, cv=cv, scoring="accuracy",
                             **cv_kwargs)

    result["cv_accuracy_mean"] = float(scores.mean())
    result["cv_accuracy_std"] = float(scores.std())
    result["cv_accuracy_folds"] = [float(s) for s in scores]

    # Fit on full data for coefficients
    lr.fit(X_scaled, y)
    coefs = lr.coef_[0]
    abs_coefs = np.abs(coefs)
    importance_order = np.argsort(-abs_coefs)

    result["coefficients"] = {
        feature_names[i]: float(coefs[i]) for i in range(n_features)
    }
    result["feature_importance_ranking"] = [
        {"rank": rank + 1, "feature": feature_names[i],
         "coefficient": float(coefs[i]), "abs_coefficient": float(abs_coefs[i])}
        for rank, i in enumerate(importance_order)
    ]

    # Recursive feature elimination with cross-validation
    if n_features >= 3 and n_folds >= 2:
        try:
            rfecv = RFECV(
                estimator=LogisticRegression(
                    C=1.0, solver="lbfgs",
                    class_weight="balanced",
                    max_iter=1000, random_state=42,
                ),
                step=1,
                cv=cv,
                scoring="accuracy",
                min_features_to_select=1,
            )
            rfecv.fit(X_scaled, y, **cv_kwargs)

            selected_mask = rfecv.support_
            selected_features = [f for f, s in zip(feature_names, selected_mask) if s]
            result["rfe_n_features_selected"] = int(rfecv.n_features_)
            result["rfe_selected_features"] = selected_features
            result["rfe_cv_accuracy"] = float(rfecv.cv_results_["mean_test_score"][
                rfecv.n_features_ - 1
            ])
            result["rfe_ranking"] = {
                feature_names[i]: int(rfecv.ranking_[i])
                for i in range(n_features)
            }
        except Exception as e:
            result["rfe_error"] = str(e)

    return result


def _find_disagree_pids(
    strategy_by_problem: dict[str, dict[int, dict]],
) -> set[int]:
    """Find problem IDs where at least one strategy solved and one failed."""
    strategies = sorted(strategy_by_problem.keys())
    if len(strategies) < 2:
        return set()

    # Find shared problems with ground truth
    pid_sets = [set(v.keys()) for v in strategy_by_problem.values()]
    shared = set.intersection(*pid_sets) if pid_sets else set()

    disagree = set()
    for pid in shared:
        solved_any = False
        failed_any = False
        for s in strategies:
            d = strategy_by_problem[s].get(pid, {})
            sa = (d.get("solver_answer") or "").upper()
            cs = (d.get("correct_solution") or "").upper()
            if not cs:
                continue
            if sa == cs:
                solved_any = True
            else:
                failed_any = True
        if solved_any and failed_any:
            disagree.add(pid)

    return disagree


def _build_disagree_dataset(
    strategy_features: dict[str, dict[int, dict]],
    strategy_by_problem: dict[str, dict[int, dict]],
    disagree_pids: set[int],
) -> tuple[np.ndarray, np.ndarray, list[str], list[str], list[int]]:
    """Build feature matrix from only the disagreement problems.

    Returns: (X, y, valid_features, strategy_labels, problem_ids)
    """
    rows: list[list[float]] = []
    labels: list[int] = []
    strat_labels: list[str] = []
    pids: list[int] = []

    for strategy in sorted(strategy_features.keys()):
        for pid in sorted(disagree_pids):
            feat = strategy_features[strategy].get(pid)
            result = strategy_by_problem.get(strategy, {}).get(pid)
            if feat is None or result is None:
                continue

            sa = (result.get("solver_answer") or "").upper()
            cs = (result.get("correct_solution") or "").upper()
            if not cs:
                continue

            row = []
            skip = False
            for f in FEATURE_NAMES:
                val = feat.get(f)
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    skip = True
                    break
                row.append(float(val))
            if skip:
                continue

            rows.append(row)
            labels.append(1 if sa == cs else 0)
            strat_labels.append(strategy)
            pids.append(pid)

    if not rows:
        return np.empty((0, 0)), np.array([]), [], [], []

    X = np.array(rows)
    y = np.array(labels)
    valid_mask = X.std(axis=0) > 1e-12
    valid_features = [f for f, v in zip(FEATURE_NAMES, valid_mask) if v]
    return X[:, valid_mask], y, valid_features, strat_labels, pids


def run_regression_analysis(
    strategy_features: dict[str, dict[int, dict]],
    strategy_by_problem: dict[str, dict[int, dict]],
    output_dir: str,
) -> dict:
    """Run logistic regression analysis and save results.

    Args:
        strategy_features: {strategy: {problem_id: features_dict}}
        strategy_by_problem: {strategy: {problem_id: coded_result}}
        output_dir: where to save outputs

    Returns:
        dict of all analysis results
    """
    os.makedirs(output_dir, exist_ok=True)

    per_strategy, pooled = _build_dataset(strategy_features, strategy_by_problem)

    all_results = {}
    lines = ["\n══════════════════════════════════════════════════════════════"]
    lines.append("  Logistic Regression: TDA Features → Solve Success")
    lines.append("══════════════════════════════════════════════════════════════\n")

    # Per-strategy models
    for strategy, (X, y, feat_names) in sorted(per_strategy.items()):
        result = _run_analysis(X, y, feat_names, label=strategy)
        all_results[strategy] = result
        lines.extend(_format_result(result))

    # Aggregated across all strategies
    X_all, y_all, feat_names_all, strat_labels = pooled
    if X_all.shape[0] > 0:
        result = _run_analysis(X_all, y_all, feat_names_all, label="ALL_STRATEGIES")
        all_results["ALL_STRATEGIES"] = result
        lines.extend(_format_result(result))

    # Disagree subset: problems where strategies disagree on solve
    strategies = sorted(strategy_by_problem.keys())
    disagree_pids = _find_disagree_pids(strategy_by_problem)
    if disagree_pids:
        X_d, y_d, feat_d, strat_d, pids_d = _build_disagree_dataset(
            strategy_features, strategy_by_problem, disagree_pids)

        n_pos = int(y_d.sum())
        n_neg = len(y_d) - n_pos
        lines.append(f"── DISAGREE SUBSET ──")
        lines.append(f"  Problems where strategies disagree: {len(disagree_pids)}")
        lines.append(f"  Per-strategy instances: {len(y_d)} "
                     f"(solved={n_pos}, unsolved={n_neg})")
        lines.append(f"  Problem IDs: {sorted(disagree_pids)}")

        # Per-problem breakdown
        disagree_detail = []
        lines.append("")
        hdr = f"  {'pid':>4s}  {'clue':<45s}"
        for s in strategies:
            hdr += f"  {s[:12]:>12s}"
        lines.append(hdr)
        lines.append("  " + "─" * (len(hdr) - 2))

        for pid in sorted(disagree_pids):
            # Get clue text from any strategy
            clue = ""
            correct = ""
            row_detail = {"problem_id": pid, "strategies": {}}
            for s in strategies:
                d = strategy_by_problem.get(s, {}).get(pid, {})
                if not clue:
                    clue = d.get("raw_clue", "")
                    correct = d.get("correct_solution", "")

            clue_short = clue[:42] + "..." if len(clue) > 45 else clue
            row = f"  {pid:4d}  {clue_short:<45s}"
            for s in strategies:
                d = strategy_by_problem.get(s, {}).get(pid, {})
                sa = (d.get("solver_answer") or "")
                cs = (d.get("correct_solution") or "").upper()
                solved = sa.upper() == cs if cs else None
                mark = "✓" if solved else "✗" if solved is not None else "?"
                row += f"  {sa[:9]+mark:>12s}"
                row_detail["strategies"][s] = {
                    "solver_answer": sa,
                    "solved": solved,
                }
            row_detail["correct_solution"] = correct
            row_detail["clue"] = clue
            disagree_detail.append(row_detail)
            lines.append(row)

        lines.append("")

        if X_d.shape[0] > 0:
            result = _run_analysis(X_d, y_d, feat_d,
                                   label="DISAGREE_SUBSET",
                                   groups=np.array(pids_d))
            result["disagree_problems"] = disagree_detail
            all_results["DISAGREE_SUBSET"] = result
            lines.extend(_format_result(result))
    else:
        lines.append("── DISAGREE SUBSET ──")
        lines.append("  No disagreement problems found (need ≥2 strategies).")
        lines.append("")

    report = "\n".join(lines)
    print(report)

    # Save
    report_path = os.path.join(output_dir, "regression_report.txt")
    Path(report_path).write_text(report)
    print(f"\n  Saved report: {report_path}")

    json_path = os.path.join(output_dir, "regression_results.json")
    Path(json_path).write_text(json.dumps(all_results, indent=2))
    print(f"  Saved JSON: {json_path}")

    # Plot feature importance
    _plot_importance(all_results, output_dir)

    return all_results


def _format_result(result: dict) -> list[str]:
    """Format a single analysis result for console output."""
    lines = []
    label = result["label"]
    lines.append(f"── {label} ──")
    lines.append(f"  Samples: {result['n_samples']} "
                 f"(solved={result['n_positive']}, "
                 f"unsolved={result['n_negative']})")
    cv_type = result.get("cv_type", "")
    if cv_type == "GroupKFold":
        lines.append(f"  CV: {cv_type} ({result.get('n_groups', '?')} groups), "
                     f"class_weight=balanced")
    elif cv_type:
        lines.append(f"  CV: {cv_type}, class_weight=balanced")

    if "error" in result:
        lines.append(f"  Skipped: {result['error']}")
        lines.append("")
        return lines

    lines.append(f"  Features: {result['n_features_input']}")
    lines.append(f"  CV accuracy: {result['cv_accuracy_mean']:.3f} "
                 f"± {result['cv_accuracy_std']:.3f} "
                 f"(folds: {', '.join(f'{s:.2f}' for s in result['cv_accuracy_folds'])})")

    # Top features by |coefficient|
    lines.append(f"  Feature importance (by |coefficient|):")
    for item in result["feature_importance_ranking"][:10]:
        sign = "+" if item["coefficient"] > 0 else "-"
        lines.append(f"    {item['rank']:2d}. {item['feature']:<25s} "
                     f"{sign}{item['abs_coefficient']:.4f}")

    # RFE results
    if "rfe_selected_features" in result:
        lines.append(f"  RFE selected {result['rfe_n_features_selected']} features "
                     f"(CV accuracy: {result['rfe_cv_accuracy']:.3f}):")
        for f in result["rfe_selected_features"]:
            lines.append(f"    • {f}")

    if "rfe_error" in result:
        lines.append(f"  RFE error: {result['rfe_error']}")

    lines.append("")
    return lines


def _plot_importance(all_results: dict, output_dir: str):
    """Plot feature importance bar charts."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    for label, result in all_results.items():
        if "error" in result:
            continue

        ranking = result.get("feature_importance_ranking", [])
        if not ranking:
            continue

        features = [r["feature"] for r in ranking]
        coefs = [r["coefficient"] for r in ranking]
        colors = ["#2E7D32" if c > 0 else "#C62828" for c in coefs]

        fig, ax = plt.subplots(figsize=(8, max(4, len(features) * 0.35)))
        y_pos = range(len(features))
        ax.barh(y_pos, coefs, color=colors, edgecolor="white", linewidth=0.5)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(features, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("Coefficient (standardized)")
        ax.set_title(f"Feature Importance: {label}\n"
                     f"CV accuracy: {result['cv_accuracy_mean']:.3f} "
                     f"± {result['cv_accuracy_std']:.3f}")
        ax.axvline(0, color="black", linewidth=0.5)

        # Mark RFE-selected features
        rfe_selected = set(result.get("rfe_selected_features", []))
        if rfe_selected:
            for i, f in enumerate(features):
                if f in rfe_selected:
                    ax.get_yticklabels()[i].set_fontweight("bold")

        plt.tight_layout()
        safe_label = label.replace(" ", "_")
        path = os.path.join(output_dir, f"importance_{safe_label}.png")
        fig.savefig(path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  Saved importance plot: {path}")
