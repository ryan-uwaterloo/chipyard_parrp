#!/usr/bin/env python3
"""
violin_helpers.py
-----------------
Shared KDE violin-plot utilities used by plot_per_test.py.
"""

import numpy as np
from scipy.stats import gaussian_kde

MIN_SAMPLES = 5


def is_degenerate(data, tol=1e-6):
    """Return True if data is empty or has no spread (all values identical)."""
    return len(data) < MIN_SAMPLES or np.ptp(data) < tol


def compute_max_area(data, half_width, bw_method=0.4):
    """
    Compute the KDE-normalised half-width so that the violin's area equals
    `half_width`.  Used to keep paired violins on a shared scale.
    """
    kde = gaussian_kde(data, bw_method=bw_method)
    y_vals = np.linspace(data.min(), data.max(), 500)
    density = kde(y_vals)
    area = np.trapezoid(density, y_vals)
    return half_width * area / density.max()


def make_half_violin(ax, data, x_center, side, color, hatch, max_area, bw_method=0.4):
    """
    Draw one half of a split violin onto `ax`.

    Parameters
    ----------
    side : 'left' | 'right'
    """
    kde = gaussian_kde(data, bw_method=bw_method)
    y_vals = np.linspace(data.min(), data.max(), 500)
    density = kde(y_vals)
    area = np.trapezoid(density, y_vals)
    density = density / area * max_area

    if side == "left":
        ax.fill_betweenx(
            y_vals, x_center - density, x_center,
            facecolor="white", edgecolor=color, hatch=hatch,
            alpha=1, linewidth=0.5,
        )
    else:
        ax.fill_betweenx(
            y_vals, x_center, x_center + density,
            facecolor="white", edgecolor=color, hatch=hatch,
            alpha=1, linewidth=0.5,
        )


def draw_violin_pair(
    ax,
    data_stock,
    data_mod,
    x_pos,
    half_width=0.35,
    gap=0.01,
    stock_color="red",
    mod_color="blue",
    stock_hatch="////////",
    mod_hatch="xxxxxxxx",
):
    """
    Draw a split violin at `x_pos`:
      • left  half  → stock  (ctrl)
      • right half  → modified (parrp)

    Handles degenerate / missing data gracefully.
    """
    has_stock = not is_degenerate(data_stock)
    has_mod   = not is_degenerate(data_mod)

    # ---- Both sides degenerate / empty --------------------------------
    if not has_stock and not has_mod:
        for data, xoff, ha in [
            (data_stock, -gap, "right"),
            (data_mod,    gap, "left"),
        ]:
            if len(data) > 0:
                ax.axhline(np.mean(data), color="gray", linewidth=0.5, linestyle="--")
                ax.text(
                    x_pos + xoff, np.mean(data), f"{np.mean(data):.1f}",
                    ha=ha, va="bottom", fontsize=5, color="gray",
                )
            else:
                ax.text(
                    x_pos + xoff, 0.5, "No data", ha=ha, va="center",
                    transform=ax.get_xaxis_transform(), fontsize=6, color="gray",
                )
        return

    # ---- Compute shared scale so both halves are comparable -----------
    if has_stock and has_mod:
        shared_area = min(
            compute_max_area(data_stock, half_width),
            compute_max_area(data_mod,   half_width),
        )
        area_stock = area_mod = shared_area
    elif has_stock:
        area_stock = compute_max_area(data_stock, half_width)
        area_mod   = None
    else:
        area_stock = None
        area_mod   = compute_max_area(data_mod, half_width)

    # ---- Stock (left) half --------------------------------------------
    if has_stock:
        make_half_violin(
            ax, data_stock, x_pos - gap, "left",
            stock_color, stock_hatch, area_stock,
        )
        ax.hlines(
            np.mean(data_stock),
            x_pos - half_width, x_pos - gap,
            linewidth=0.8, color="k",
        )
    else:
        val = np.mean(data_stock) if len(data_stock) > 0 else None
        if val is not None:
            ax.hlines(val, x_pos - half_width, x_pos - gap,
                      linewidth=0.8, color="k", linestyle="--")
            ax.text(x_pos - gap, val, f"{val:.1f}",
                    ha="right", va="bottom", fontsize=5, color=stock_color)
        else:
            ax.text(x_pos - gap, 0.5, "N/A", ha="right", va="center",
                    transform=ax.get_xaxis_transform(), fontsize=5, color="gray")

    # ---- Modified (right) half ----------------------------------------
    if has_mod:
        make_half_violin(
            ax, data_mod, x_pos + gap, "right",
            mod_color, mod_hatch, area_mod,
        )
        ax.hlines(
            np.mean(data_mod),
            x_pos + gap, x_pos + half_width,
            linewidth=0.8, color="k",
        )
    else:
        val = np.mean(data_mod) if len(data_mod) > 0 else None
        if val is not None:
            ax.hlines(val, x_pos + gap, x_pos + half_width,
                      linewidth=0.8, color="k", linestyle="--")
            ax.text(x_pos + gap, val, f"{val:.1f}",
                    ha="left", va="bottom", fontsize=5, color=mod_color)
        else:
            ax.text(x_pos + gap, 0.5, "N/A", ha="left", va="center",
                    transform=ax.get_xaxis_transform(), fontsize=5, color="gray")
