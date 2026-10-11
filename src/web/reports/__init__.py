"""Reports blueprint — word cloud, annual summary, HTML report."""
from flask import Blueprint

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')

from . import wordcloud  # register routes
