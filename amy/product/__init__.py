"""Product surface — the deployable layer on top of PKOS/Knowledge/Collab.

Profile builder, agent dashboard, agent marketplace, proactive suggestions,
graph-viz transform, and the public portfolio (safe) view.
"""
from .profile import ProfileBuilder, build_profile
from .dashboard import build_dashboard
from .marketplace import Marketplace
from .suggestions import build_suggestions
from .portfolio import build_portfolio, PUBLIC_DOMAINS, BLOCKED_DOMAINS
from .graphviz import to_graph
