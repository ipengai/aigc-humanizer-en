"""AI Detector (sub-product) specific configuration.

Kept in a separate module (not config.py) so the detector's pricing/quota
constants can be edited without touching the sensitive-protected main config.
All values here are non-secret.
"""

# 检测侧计费：¥4.9 / 1000 词（用户定价）
DETECTION_PRICE_PER_1000 = 4.9

# 注册即赠送的检测词数（与改写 200 词独立）
DETECTION_SIGNUP_BONUS = 1000

# 每自然月免费检测额度（重置值）
DETECTION_MONTHLY_QUOTA = 1000

# 可选的充值档位（词数）
DETECTION_RECHARGE_PACKAGES = [1000, 2000, 5000]
