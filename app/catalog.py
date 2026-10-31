"""actions.json(OpenAPI) → 에이전트가 쓸 엔드포인트 카탈로그 + 호출 검증."""
import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKED_IDS = {"resetMyActionData"}  # 에이전트는 초기화 호출 금지


def _resolve(schema, S):
    while "$ref" in schema:
        schema = S[schema["$ref"].split("/")[-1]]
    return schema


def _brief(schema, S):
    """요청 본문 스키마를 '필드*:타입' 형태 짧은 문자열로."""
    schema = _resolve(schema, S)
    props = schema.get("properties", {})
    if not props:
        return "{}"
    req = set(schema.get("required", []))
    parts = []
    for k, v in props.items():
        v = _resolve(v, S)
        if "enum" in v:
            t = "|".join(str(x) for x in v["enum"])
        elif v.get("type") == "array":
            t = f"[{_resolve(v.get('items', {}), S).get('type', 'any')}]"
        else:
            t = v.get("type", "any")
        parts.append(f"{k}{'*' if k in req else ''}:{t}")
    return "{" + ", ".join(parts) + "}"


@lru_cache(maxsize=1)
def load_catalog():
    d = json.loads((ROOT / "actions.json").read_text(encoding="utf-8"))
    S = d.get("components", {}).get("schemas", {})
    eps = []
    for raw_path, ms in d["paths"].items():
        for m, op in ms.items():
            if m not in ("get", "post", "put", "patch", "delete"):
                continue
            oid = op.get("operationId", "")
            path, _, q = raw_path.partition("?")
            qparam = q.rstrip("=") if q else None
            rb = (op.get("requestBody", {}).get("content", {})
                  .get("application/json", {}).get("schema"))
            eps.append({
                "id": oid,
                "method": m.upper(),
                "path": path,
                "query": qparam,
                "body": _brief(rb, S) if rb else None,
                "summary": op.get("summary", ""),
                "regex": re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/?#]+", path) + "$"),
                "blocked": oid in BLOCKED_IDS,
            })
    return eps


def catalog_text():
    lines = []
    for e in load_catalog():
        if e["blocked"]:
            continue
        p = e["path"] + (f"?{e['query']}=" if e["query"] else "")
        s = f"{e['id']} {e['method']} {p} — {e['summary']}"
        if e["method"] != "GET":
            s += f" body{e['body']}"
        lines.append(s)
    return "\n".join(lines)


def match(method, path, query):
    """허용된 엔드포인트인지 검사. (endpoint|None, error|None)"""
    method = method.upper()
    cands = [e for e in load_catalog() if e["method"] == method and e["regex"].match(path)]
    if not cands:
        return None, f"허용되지 않은 경로/메서드: {method} {path}"
    if any(e["blocked"] for e in cands):
        return None, "이 API는 에이전트가 호출할 수 없습니다."
    qkeys = set((query or {}).keys())
    for e in cands:
        if e["query"]:
            if qkeys == {e["query"]}:
                return e, None
        elif not qkeys:
            return e, None
    allowed = [e["query"] for e in cands if e["query"]]
    return None, f"쿼리 파라미터 오류. 이 경로에 허용되는 검색 파라미터(하나만): {allowed or '없음'}"
