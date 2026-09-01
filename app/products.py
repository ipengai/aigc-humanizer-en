#!/usr/bin/env python3
"""
通用商品注册表 — 数字产品（一次性买断 / 网盘交付）的集中定义。

设计目标：未来上架新产品**只加一行字典**，不写新路由、不碰支付逻辑。
支付/交付层（app/routes/payment.py、app/helpers/tasks.py）只读这个注册表，
按 sku 走通用流程。

交付链接（网盘直链）不放代码里，统一从 config.PRODUCT_DELIVERY_LINKS 读，
部署时填真实网盘地址即可，避免把敏感链接提交进仓库。
"""

# sku -> 商品定义。
#   name:        收银台/成功页展示的商品名
#   price:       固定单价（元，float）
#   description: 收银台副标题
PRODUCTS = {
    "agentteam_kit": {
        "sku": "agentteam_kit",
        "name": "一人公司 AI Agent 团队搭建模板包",
        "price": 49.00,
        "description": "AgentTeam 模板包 · 一次购买 · 永久下载",
    },
}


def get_product(sku):
    """按 sku 返回商品定义，不存在返回 None。"""
    return PRODUCTS.get(sku)


def list_product_skus():
    """返回所有已注册 sku（测试/管理用）。"""
    return list(PRODUCTS.keys())


def get_delivery_link(sku):
    """
    返回某 sku 的网盘交付直链。

    链接从 config.PRODUCT_DELIVERY_LINKS 读（部署时配置），
    不在代码里硬编码。未配置或缺失时返回 None，交付页会提示「联系补发」。
    """
    try:
        import config
    except Exception:
        return None
    links = getattr(config, "PRODUCT_DELIVERY_LINKS", None) or {}
    return links.get(sku)
