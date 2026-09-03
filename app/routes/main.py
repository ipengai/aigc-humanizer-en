"""
Main page routes — index, orders page, health check, SEO.
"""

from flask import Blueprint, render_template, session, jsonify, make_response, request
from datetime import datetime, timezone
from urllib.parse import urlparse

main_bp = Blueprint('main', __name__)


def _capture_order_attribution():
    """Keep privacy-safe acquisition dimensions for the next order in this session."""
    current = dict(session.get('order_attribution') or {})
    utm_source = (request.args.get('utm_source') or '').strip()[:100]
    utm_medium = (request.args.get('utm_medium') or '').strip()[:100]
    utm_campaign = (request.args.get('utm_campaign') or '').strip()[:150]

    referrer_domain = None
    if request.referrer:
        try:
            parsed = urlparse(request.referrer)
            if parsed.hostname and parsed.hostname != request.host.split(':', 1)[0]:
                referrer_domain = parsed.hostname.lower()[:150]
        except ValueError:
            referrer_domain = None

    if utm_source:
        current['utm_source'] = utm_source
    if utm_medium:
        current['utm_medium'] = utm_medium
    if utm_campaign:
        current['utm_campaign'] = utm_campaign
    if referrer_domain:
        current['referrer_domain'] = referrer_domain
    current['traffic_source'] = (
        current.get('utm_source') or current.get('referrer_domain') or 'direct'
    )
    session['order_attribution'] = current


def _site_url():
    """Return the canonical site URL based on the incoming request host.

    The app is served behind nginx with HTTPS-only public access, so we
    always use https:// regardless of the internal scheme between nginx
    and the Flask process.
    """
    return f"https://{request.host}"


@main_bp.route('/robots.txt')
def robots_txt():
    """Allow all crawlers, point to sitemap."""
    site_url = _site_url()
    resp = make_response(f"""User-agent: *
Allow: /
Sitemap: {site_url}/sitemap.xml
""")
    resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
    return resp


@main_bp.route('/sitemap.xml')
def sitemap_xml():
    """Simple sitemap listing all public pages."""
    site_url = _site_url()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pages = [
        {'loc': site_url + '/', 'lastmod': today, 'changefreq': 'weekly', 'priority': '1.0'},
        {'loc': site_url + '/faq', 'lastmod': today, 'changefreq': 'weekly', 'priority': '0.5'},
        {'loc': site_url + '/orders', 'lastmod': today, 'changefreq': 'weekly', 'priority': '0.3'},
    ]
    urls = '\n'.join(
        f"""  <url>\n    <loc>{p['loc']}</loc>\n    <lastmod>{p['lastmod']}</lastmod>\n    <changefreq>{p['changefreq']}</changefreq>\n    <priority>{p['priority']}</priority>\n  </url>"""
        for p in pages
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}
</urlset>"""
    resp = make_response(xml)
    resp.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return resp


@main_bp.route('/')
def index():
    """Landing page."""
    from config import PRICE_PER_1000_WORDS
    _capture_order_attribution()
    return render_template(
        'index.html',
        price_per_1000_words=PRICE_PER_1000_WORDS,
    )


@main_bp.route('/faq')
def faq_page():
    """Help / FAQ page with usage guide, tips and common questions."""
    return render_template('faq.html')


@main_bp.route('/orders')
def orders_page():
    """Personal account page with balance and order history."""
    user_id = session.get('user_id')
    if not user_id:
        return render_template('orders.html', needs_login=True)
    return render_template('orders.html', needs_login=False)


@main_bp.route('/api/health')
def api_health():
    """Health check endpoint for monitoring and load balancers."""
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})
