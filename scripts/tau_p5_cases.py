"""Retired custom evaluation entry points; business records remain in their DB."""

def _retired():
    raise RuntimeError(
        "Custom evaluation cases were removed at the user's request. "
        "Use scripts/aftercare-eval.sh official for official Retail tasks. "
        "Existing demo orders and conversations are preserved."
    )

def p5_cases():
    return _retired()

def p5_inputs():
    return _retired()
