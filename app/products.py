#!/usr/bin/env python3
"""
通用商品注册表 — 数字产品（一次性买断 / 网盘交付）的集中定义。

设计目标：未来上架新产品**只加一行字典**，不写新路由、不碰支付逻辑。
支付/交付层（app/routes/payment.py、app/helpers/tasks.py）只读这个注册表，
按 sku 走通用流程。

交付链接（网盘直链）读取优先级：
  1) config.PRODUCT_DELIVERY_LINKS[sku] —— 部署时可在服务器 config 覆盖（优先）
  2) 代码内置 PRODUCTS[sku]["delivery"] —— 随仓库走，部署零摩擦（兜底）
网盘分享链接属公开信息，内置在代码里无敏感问题。
"""

# sku -> 商品定义。
#   name:        收银台/成功页展示的商品名
#   price:       固定单价（元，float）
#   description: 收银台副标题
#   delivery:    网盘交付直链（兜底值，可被服务器 config 覆盖）
PRODUCTS = {
    "agentteam_kit": {
        "sku": "agentteam_kit",
        "name": "一人公司 AI Agent 团队搭建模板包",
        "price": 49.00,
        "description": "AgentTeam 模板包 · 一次购买 · 永久下载",
        "delivery": "https://pan.baidu.com/s/1oNe35ElWsLmrdz_TA4d6SQ?pwd=zce9",
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

    优先级：服务器 config.PRODUCT_DELIVERY_LINKS[sku]（可覆盖）
            > 代码内置 PRODUCTS[sku]["delivery"]（兜底，随仓库走）。
    两者皆无时返回 None，交付页会提示「联系补发」。
    """
    # 1) 服务器 config 可覆盖（优先）
    try:
        import config
        links = getattr(config, "PRODUCT_DELIVERY_LINKS", None) or {}
        if sku in links:
            return links[sku]
    except Exception:
        pass
    # 2) 兜底：代码内置默认链接（部署零摩擦）
    prod = PRODUCTS.get(sku)
    return prod.get("delivery") if prod else None
