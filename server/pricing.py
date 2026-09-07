"""Тарифы для cost-журнала: ₽ за 1M токенов (вход/выход).

Курс считает деньги с первой недели (принцип №4). Значения — ориентир на 2026-08;
актуализируй на старте потока (в программе это отдельный пункт «что нужно от лектора»).
RouteAY-дефолт из v0.5: ~25₽/M вход, ~102₽/M выход. USD пересчитан ~90₽/$ (грубо).
"""
from __future__ import annotations

# (input_rub_per_mtok, output_rub_per_mtok)
PRICING: dict[str, tuple[float, float]] = {
    # RouteAI-дефолт (если модель не найдена в таблице — берём это)
    "__default__": (25.0, 102.0),
    # DeepSeek (исследования)
    "deepseek/deepseek-v4-flash": (13.0, 25.0),     # ~$0.14/$0.28
    "deepseek/deepseek-v4-pro": (39.0, 78.0),        # ~$0.435/$0.87
    # Qwen (кодинг, через API)
    "qwen/qwen3.8-27b": (32.0, 230.0),               # ~$0.35/$2.55
    "qwen-plus": (36.0, 216.0),
    # Qwen агентский движок (победитель гейта, 30B-A3B: активны 3B -> дёшево).
    # ≈ оценка, уточнить по прайсу RouteAI на старте потока (см. дисклеймер выше).
    "qwen/qwen3-30b-a3b-instruct-2507": (18.0, 72.0),
    # Claude — только для лектора, для полноты картины
    "claude-haiku-4-5": (90.0, 450.0),
    "claude-sonnet-4-6": (270.0, 1350.0),
    "claude-opus-4-8": (450.0, 2250.0),
    # Self-hosted: маржинальная стоимость ≈ амортизация GPU, не per-token.
    # RTX 6000 @ $0.60/ч ≈ 54₽/ч. Ставим номинал ~0, реальную стоимость GPU
    # считаем отдельно в docs/cost-analysis.md (fixed cost, не per-token).
    "local/qwen3.8-27b": (0.0, 0.0),
}


def cost_rub(model: str, input_tokens: int, output_tokens: int) -> float:
    """Стоимость вызова в рублях по тарифной таблице."""
    inp, out = PRICING.get(model, PRICING["__default__"])
    return round(inp * input_tokens / 1_000_000 + out * output_tokens / 1_000_000, 4)
