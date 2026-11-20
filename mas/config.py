"""Configuration loading. Secrets never live in config files."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = Path(os.environ.get('MAS_LOGS') or (ROOT / 'logs'))

DEFAULTS = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> dict:
    path = path or os.environ.get('MAS_CONFIG') or str(ROOT / 'config.json')
    data = {}
    if Path(path).exists():
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    cfg = _merge(DEFAULTS, data)
    if isinstance(data.get('pilots'), dict):
        cfg['pilots'] = dict(data['pilots'])
    if os.environ.get('SPACEMOLT_MCP_URL'):
        cfg['mcp_endpoint'] = os.environ['SPACEMOLT_MCP_URL']
    if os.environ.get('PILOT_1_USERNAME'):
        cfg['pilots']['A'] = os.environ['PILOT_1_USERNAME']
    if os.environ.get('PILOT_2_USERNAME'):
        cfg['pilots']['B'] = os.environ['PILOT_2_USERNAME']
    if os.environ.get('GROQ_MODEL'):
        cfg['llm']['model'] = os.environ['GROQ_MODEL']
    LOGS.mkdir(parents=True, exist_ok=True)
    return cfg


def pilot_username(cfg: dict, pilot: str) -> str:
    if pilot in cfg['pilots']:
        return cfg['pilots'][pilot]
    if pilot in cfg['pilots'].values():
        return pilot
    raise ValueError(f'unknown pilot {pilot!r}')
