"""
Mathematical engine for Black-Scholes-Merton, Greeks, and Delta Inversion.
"""

from __future__ import annotations
import math


class QuantMath:
    """High-precision quantitative mathematics engine."""

    @staticmethod
    def norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def norm_pdf(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    @staticmethod
    def norm_ppf(p: float) -> float:
        if p <= 0.0 or p >= 1.0:
            raise ValueError(f"Probability p must be in (0, 1), got {p}")

        a = [
            -3.969683028665376e+01,  2.209460984245205e+02,
            -2.759285104469687e+02,  1.383577518672690e+02,
            -3.066479806614716e+01,  2.506628277459239e+00
        ]
        b = [
            -5.447609879822406e+01,  1.615858368580409e+02,
            -1.556989798598866e+02,  6.680131188771972e+01,
            -1.328068155288572e+01
        ]
        c = [
            -7.784894002430293e-03, -3.223964580411365e-01,
            -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00,  2.938163982698783e+00
        ]
        d = [
             7.784695709041462e-03,  3.224671290700398e-01,
             2.445134137142996e+00,  3.754408661907416e+00
        ]

        p_low = 0.02425
        p_high = 1.0 - p_low

        if p < p_low:
            q = math.sqrt(-2.0 * math.log(p))
            return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                   ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
        elif p <= p_high:
            q = p - 0.5
            r = q * q
            return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
                   (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
        else:
            q = math.sqrt(-2.0 * math.log(1.0 - p))
            return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                    ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)

    @classmethod
    def black_scholes_price(
        cls, spot: float, strike: float, dte: float, iv: float, r: float = 0.045, is_call: bool = True
    ) -> float:
        t = max(dte, 0.5) / 365.0
        v_sqrt_t = iv * math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / v_sqrt_t
        d2 = d1 - v_sqrt_t
        df = math.exp(-r * t)
        if is_call:
            price = spot * cls.norm_cdf(d1) - strike * df * cls.norm_cdf(d2)
        else:
            price = strike * df * cls.norm_cdf(-d2) - spot * cls.norm_cdf(-d1)
        return max(0.01, price)
