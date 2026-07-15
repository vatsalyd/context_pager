from __future__ import annotations

# Model pricing: (input_per_1k, output_per_1k) in USD
MODEL_PRICING = {
    "gpt-4.1": (0.0025, 0.01),
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gemini-2.5-flash": (0.000075, 0.0003),  # $0.075/M in, $0.30/M out
    "gemini-1.5-pro": (0.00125, 0.005),
}


def get_model_pricing(model: str) -> tuple[float, float]:
    return MODEL_PRICING.get(model, (0.0, 0.0))


def calculate_savings(original_tokens: int, compressed_tokens: int, model: str = "gemini-2.5-flash") -> float:
    """Calculate cost savings from compression."""
    input_price, _ = get_model_pricing(model)
    saved_tokens = original_tokens - compressed_tokens
    return (saved_tokens / 1000) * input_price


def calculate_total_cost(tokens: int, model: str = "gemini-2.5-flash") -> float:
    """Calculate total cost for token count."""
    input_price, output_price = get_model_pricing(model)
    # Assume 50/50 input/output split for simplicity
    return (tokens / 1000) * ((input_price + output_price) / 2)