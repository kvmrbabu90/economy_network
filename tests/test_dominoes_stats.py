"""Statistics for the domino study (spec §5): CR1 clustered means and OLS slopes, t-based p-values with
G-1 degrees of freedom, Holm step-down, and a reproducible date-block bootstrap."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import stats as sps

from research.dominoes import stats as st


def test_clustered_mean_matches_a_hand_computation():
    r = st.clustered_mean([1, 2, 3, 4, 5, 6], ["a", "a", "b", "b", "c", "c"])
    # cluster deviation sums -4, 0, 4 -> se^2 = 3/2 * 32 / 36
    assert r["mean"] == pytest.approx(3.5)
    assert r["se"] == pytest.approx(np.sqrt(1.5 * 32 / 36))
    assert r["t"] == pytest.approx(3.5 / np.sqrt(1.5 * 32 / 36))
    assert (r["n"], r["g"], r["df"]) == (6, 3, 2)


def test_singleton_clusters_reduce_to_the_usual_iid_standard_error():
    v = np.random.default_rng(0).normal(0.01, 0.05, 50)
    r = st.clustered_mean(v, np.arange(50))
    assert r["se"] == pytest.approx(np.std(v, ddof=1) / np.sqrt(50))


def test_nan_values_and_their_clusters_are_dropped():
    r = st.clustered_mean([1.0, np.nan, 3.0, 5.0], ["a", "b", "c", "c"])
    assert (r["n"], r["g"]) == (3, 2) and r["mean"] == pytest.approx(3.0)


def test_p_values_use_t_with_g_minus_1_degrees_of_freedom():
    rng = np.random.default_rng(1)
    v = rng.normal(0.02, 0.05, 40)
    cl = np.repeat(np.arange(10), 4)
    r = st.clustered_mean(v, cl)
    assert r["df"] == 9
    assert r["p_two"] == pytest.approx(2 * sps.t.sf(abs(r["t"]), 9))
    assert st.one_sided_p(r["t"], 9, +1) == pytest.approx(sps.t.sf(r["t"], 9))
    assert st.one_sided_p(r["t"], 9, -1) == pytest.approx(sps.t.cdf(r["t"], 9))


def test_clustered_ols_slope_and_cr1_sandwich_match_numpy():
    rng = np.random.default_rng(2)
    n, G = 120, 15
    cl = np.repeat(np.arange(G), n // G)
    x = rng.normal(0, 0.03, n) + np.repeat(rng.normal(0, 0.02, G), n // G)
    y = 0.001 + 0.4 * x + rng.normal(0, 0.02, n) + np.repeat(rng.normal(0, 0.01, G), n // G)
    r = st.clustered_ols(y, x, cl)
    X = np.column_stack([np.ones(n), x])
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    u = y - X @ b
    meat = sum(np.outer(X[cl == g].T @ u[cl == g], X[cl == g].T @ u[cl == g]) for g in range(G))
    bread = np.linalg.inv(X.T @ X)
    V = bread @ meat @ bread * (G / (G - 1)) * ((n - 1) / (n - 2))
    assert r["slope"] == pytest.approx(b[1]) and r["intercept"] == pytest.approx(b[0])
    assert r["se"] == pytest.approx(np.sqrt(V[1, 1]))
    assert r["df"] == G - 1


def test_holm_step_down():
    assert st.holm([0.001, 0.01, 0.04], 0.05) == [True, True, True]      # thresholds a/3, a/2, a/1
    assert st.holm([0.001, 0.03, 0.04], 0.05) == [True, False, False]    # 0.03 > a/2 stops the sequence
    assert st.holm([0.04, 0.001, 0.03], 0.05) == [False, True, False]    # order of input preserved
    assert st.holm([], 0.05) == []


def test_block_bootstrap_ci_is_reproducible_and_brackets_the_mean():
    rng = np.random.default_rng(3)
    cl = np.repeat(np.arange(30), 5)
    v = rng.normal(0.01, 0.05, 150) + np.repeat(rng.normal(0, 0.03, 30), 5)
    lo1, hi1 = st.block_bootstrap_ci(v, cl, n_boot=2000, seed=7)
    lo2, hi2 = st.block_bootstrap_ci(v, cl, n_boot=2000, seed=7)
    assert (lo1, hi1) == (lo2, hi2) and lo1 < np.mean(v) < hi1


def test_week_of_is_iso_year_week():
    assert st.week_of("2021-01-03") == "2020-W53" and st.week_of("2021-01-04") == "2021-W01"
