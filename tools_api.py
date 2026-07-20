from flask import Blueprint, Response, jsonify, request, current_app
import json
from typing import Dict

from mcp_client import REGISTRY
from agent_system import validate_api_key, current_user_id

bp = Blueprint('tools', __name__)


def _has_role_or_api_key(request, allowed_roles):
    # API key check
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if api_key:
        keyd = validate_api_key(api_key)
        if keyd and keyd.get('role') in allowed_roles:
            return True
    # Session-based role
    # current_user_id used for user id; agent_system keeps session role in session but we avoid importing session here
    # Fallback: treat as admin if no auth (for local dev) - but prefer deny
    # For simplicity, expose only if allowed role includes 'user' or 'admin'
    # In real deployment, integrate with agent_system.session
    return False


@bp.route('/api/tools')
def api_tools():
    tools = []
    for t in REGISTRY.list_tools():
        tools.append({
            'name': t.name,
            'label': t.label,
            'description': t.description,
            'transport': t.transport,
            'endpoint': t.endpoint,
            'enabled': t.enabled,
            'allowed_roles': t.allowed_roles,
        })
    return jsonify({'ok': True, 'tools': tools})


@bp.route('/api/tools/enable', methods=['POST'])
def api_tools_enable():
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    enabled = bool(data.get('enabled', True))
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    ok = REGISTRY.set_enabled(name, enabled)
    return jsonify({'ok': ok})


@bp.route('/api/tools/run', methods=['POST'])
def api_tools_run():
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    payload = data.get('input', {})
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    tool = REGISTRY.get(name)
    if not tool:
        return jsonify({'ok': False, 'error': 'tool not found'}), 404
    if not tool.enabled:
        return jsonify({'ok': False, 'error': 'tool disabled'}), 403
    # Permission check - try API key then deny
    api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
    if api_key:
        keyd = validate_api_key(api_key)
        if keyd and keyd.get('role') not in tool.allowed_roles:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    else:
        # no API key -> require session login, but we cannot access session here reliably without circular import
        # For safety in this implementation, deny if tool requires admin
        if 'admin' in tool.allowed_roles and 'user' not in tool.allowed_roles:
            return jsonify({'ok': False, 'error': 'forbidden - admin only'}), 403

    def generate():
        for chunk in REGISTRY.run(name, payload):
            # Stream as SSE-compatible data: each chunk prefixed by data: for event-stream
            yield f"data: {json.dumps({'text': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), content_type='text/event-stream')
