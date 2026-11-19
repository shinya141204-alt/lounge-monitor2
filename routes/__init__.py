"""Modular Flask Blueprints for Aiseki Navi (`app.py` route decomposition)."""

from routes.live_api_bp import live_api_bp
from routes.portal_bp import portal_bp
from routes.push_line_bp import push_line_bp
from routes.seo_feeds_bp import seo_feeds_bp
from routes.sns_jobs_bp import sns_jobs_bp
from routes.staff_bp import staff_bp


def register_blueprints(flask_app):
    """Attach all domain Blueprints to the primary Flask application."""
    flask_app.register_blueprint(live_api_bp)
    flask_app.register_blueprint(portal_bp)
    flask_app.register_blueprint(seo_feeds_bp)
    flask_app.register_blueprint(staff_bp)
    flask_app.register_blueprint(sns_jobs_bp)
    flask_app.register_blueprint(push_line_bp)
