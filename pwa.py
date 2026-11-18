"""TenantMan PWA + TWA support: manifest, service worker, asset links.

Imported at the bottom of app.py.

Environment variables:
  TWA_PACKAGE_NAME    e.g. com.tenantman.app
  TWA_SHA256          SHA-256 fingerprint from your signing key (colon-separated)
  TWA_HOST            your production hostname, e.g. tenantman.co.ls
"""
import os

from flask import Response, jsonify, send_from_directory, url_for

from app import app, now


TWA_PACKAGE = os.environ.get("TWA_PACKAGE_NAME", "com.tenantman.app")
TWA_SHA256 = os.environ.get("TWA_SHA256", "")
TWA_HOST = os.environ.get("TWA_HOST", "")


@app.route("/manifest.webmanifest")
def pwa_manifest():
    # Flask's jsonify gives us correct JSON; we serve it with the right mimetype
    data = {
        "name": "TenantMan",
        "short_name": "TenantMan",
        "description": "Manage your tenancy, rent and maintenance requests.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ffffff",
        "theme_color": "#0a6b3e",
        "lang": "en",
        "categories": ["business", "finance", "productivity"],
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "My statement", "url": "/statement",
             "icons": [{"src": "/static/icons/statement-96.png", "sizes": "96x96"}]},
            {"name": "Requests", "url": "/requests",
             "icons": [{"src": "/static/icons/requests-96.png", "sizes": "96x96"}]},
        ],
    }
    resp = jsonify(data)
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.route("/service-worker.js")
def pwa_service_worker():
    """Served from root so its scope covers the whole app."""
    return send_from_directory(
        app.static_folder, "service-worker.js", mimetype="application/javascript"
    )


@app.route("/.well-known/assetlinks.json")
def pwa_asset_links():
    """Android verifies this file to trust your domain inside the TWA."""
    if not TWA_SHA256:
        return jsonify([{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {"namespace": "android_app", "package_name": TWA_PACKAGE,
                       "sha256_cert_fingerprints": []},
        }]), 200, {"Content-Type": "application/json"}
    fingerprints = [fp.strip().upper() for fp in TWA_SHA256.split(",") if fp.strip()]
    return jsonify([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": TWA_PACKAGE,
            "sha256_cert_fingerprints": fingerprints,
        },
    }])


@app.after_request
def pwa_headers(resp):
    """No-cache for the manifest and SW so updates go live quickly;
    long-cache for icons."""
    path = resp.request.path if hasattr(resp, "request") else ""
    # The 'request' attr isn't on response — use g via flask instead:
    from flask import request
    p = request.path
    if p in ("/manifest.webmanifest", "/service-worker.js", "/.well-known/assetlinks.json"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif p.startswith("/static/icons/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp