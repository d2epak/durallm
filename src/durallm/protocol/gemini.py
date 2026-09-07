"""Google Gemini REST Protocol Adapter for Normalized IR.

Handles Protobuf schema sanitization (`clean_gemini_schema`) and converts
between Normalized IR and Google AI Studio `/v1beta/models/...:generateContent`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from durallm.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
)

_GEMINI_TYPE_MAP = {
    "object": "OBJECT",
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
}
_GEMINI_PROHIBITED_KEYS = {
    "$schema", "additionalProperties", "default", "title",
    "$id", "$comment", "examples", "definitions", "$defs",
}
_MAX_REF_DEPTH = 8


def _resolve_local_ref(ref: str, definitions: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    parts = ref.split("/")
    if len(parts) == 3 and parts[0] == "#" and parts[1] in ("$defs", "definitions"):
        target = definitions.get(parts[2])
        return target if isinstance(target, dict) else None
    return None


def clean_gemini_schema(schema: Any, _definitions: Optional[Dict[str, Any]] = None, _depth: int = 0) -> Any:
    """Sanitize a JSON schema for a Gemini FunctionDeclaration.

    Gemini's Schema proto has no `$defs`/`$ref`, so local references are inlined
    (bounded depth; cycles collapse to a bare OBJECT) instead of being dropped.
    """
    if not isinstance(schema, dict):
        return schema

    if _definitions is None:
        _definitions = {**schema.get("definitions", {}), **schema.get("$defs", {})}

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = _resolve_local_ref(ref, _definitions)
        if target is None or _depth >= _MAX_REF_DEPTH:
            return {"type": "OBJECT"}
        merged = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
        return clean_gemini_schema(merged, _definitions, _depth + 1)

    cleaned: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in _GEMINI_PROHIBITED_KEYS:
            continue

        if k == "type":
            if isinstance(v, str):
                cleaned["type"] = _GEMINI_TYPE_MAP.get(v.lower(), v.upper())
            elif isinstance(v, list):
                non_null = [x for x in v if x != "null"]
                first_type = non_null[0] if non_null else "string"
                cleaned["type"] = _GEMINI_TYPE_MAP.get(first_type.lower(), "STRING")
            else:
                cleaned["type"] = "OBJECT"
        elif k == "properties" and isinstance(v, dict):
            cleaned["properties"] = {
                prop_k: clean_gemini_schema(prop_v, _definitions, _depth + 1)
                for prop_k, prop_v in v.items()
            }
        elif k == "items" and isinstance(v, dict):
            cleaned["items"] = clean_gemini_schema(v, _definitions, _depth + 1)
        elif k == "required" and isinstance(v, list):
            cleaned["required"] = [str(x) for x in v]
        elif k == "description" and isinstance(v, str):
            cleaned["description"] = v
        elif k == "enum" and isinstance(v, list):
            cleaned["enum"] = [str(x) for x in v]

    if "type" not in cleaned:
        cleaned["type"] = "OBJECT"

    return cleaned


def _gemini_response_id(gemini_resp: Dict[str, Any]) -> str:
    """Stable id for a response: the API's responseId, else a digest of the body."""
    rid = gemini_resp.get("responseId")
    if isinstance(rid, str) and rid:
        return rid
    body = json.dumps(gemini_resp, sort_keys=True, default=str).encode("utf-8")
    return f"gemini_{hashlib.sha1(body).hexdigest()[:12]}"


def tool_choice_to_gemini(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """Map OpenAI- or Anthropic-style tool_choice onto Gemini functionCallingConfig."""
    mode: Optional[str] = None
    names: List[str] = []
    if isinstance(tool_choice, str):
        mode = {"auto": "AUTO", "required": "ANY", "none": "NONE"}.get(tool_choice)
    elif isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in ("auto", "none"):
            mode = kind.upper()
        elif kind == "any":
            mode = "ANY"
        elif kind == "tool" and tool_choice.get("name"):
            mode, names = "ANY", [tool_choice["name"]]
        elif kind == "function" and (tool_choice.get("function") or {}).get("name"):
            mode, names = "ANY", [tool_choice["function"]["name"]]
    if mode is None:
        return None
    config: Dict[str, Any] = {"mode": mode}
    if names:
        config["allowedFunctionNames"] = names
    return {"functionCallingConfig": config}


def ir_to_gemini_request(req: NormalizedRequest, target_model: str) -> Dict[str, Any]:
    """Convert NormalizedRequest IR into Gemini generateContent payload with sanitized schema."""
    contents: List[Dict[str, Any]] = []
    system_instruction: Optional[Dict[str, Any]] = None

    sys_text = req.get_effective_system_instruction()
    if sys_text:
        system_instruction = {"parts": [{"text": sys_text}]}

    for m in req.messages:
        if m.role == "system":
            continue

        if m.role == "user":
            user_parts: List[Dict[str, Any]] = []
            if m.content:
                user_parts.append({"text": m.content})
            for tr in m.tool_results:
                user_parts.append({
                    "functionResponse": {
                        "name": tr.tool_name or "tool",
                        "response": {"output": tr.content},
                    }
                })
            contents.append({"role": "user", "parts": user_parts or [{"text": ""}]})

        elif m.role == "assistant":
            parts: List[Dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                parts.append({
                    "functionCall": {
                        "name": tc.name,
                        "args": tc.arguments,
                    }
                })
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})

        elif m.role == "tool":
            # Gemini expects functionResponse parts in a "user" turn; "function" is rejected.
            for tr in m.tool_results:
                contents.append({
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tr.tool_name or "tool",
                                "response": {"output": tr.content},
                            }
                        }
                    ],
                })
            if not m.tool_results and m.content:
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": m.name or "tool", "response": {"output": m.content}}}],
                })

    gemini_req: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        gemini_req["systemInstruction"] = system_instruction

    # Tools
    if req.tools:
        declarations = []
        for t in req.tools:
            cleaned_params = clean_gemini_schema(t.parameters)
            declarations.append({
                "name": t.name,
                "description": t.description,
                "parameters": cleaned_params,
            })
        gemini_req["tools"] = [{"functionDeclarations": declarations}]
        tool_config = tool_choice_to_gemini(req.tool_choice)
        if tool_config is not None:
            gemini_req["toolConfig"] = tool_config

    gen_config: Dict[str, Any] = {}
    if req.max_output_tokens:
        gen_config["maxOutputTokens"] = min(req.max_output_tokens, 8192)
    if req.temperature is not None:
        gen_config["temperature"] = req.temperature
    if gen_config:
        gemini_req["generationConfig"] = gen_config

    return gemini_req


def gemini_response_to_ir(gemini_resp: Dict[str, Any], model_name: str) -> NormalizedResponse:
    """Convert Gemini generateContent response into NormalizedResponse IR."""
    candidates = gemini_resp.get("candidates", [])
    response_id = _gemini_response_id(gemini_resp)
    if not candidates:
        return NormalizedResponse(
            response_id=response_id,
            model=model_name,
            content="",
            finish_reason="stop",
            raw_response=gemini_resp,
        )

    first = candidates[0]
    content_obj = first.get("content", {})
    parts = content_obj.get("parts", [])

    text_pieces: List[str] = []
    tool_calls: List[NormalizedToolCall] = []

    for index, p in enumerate(parts):
        if "text" in p:
            text_pieces.append(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"]
            args = fc.get("args", {})
            tool_calls.append(
                NormalizedToolCall(
                    # Gemini gives functionCall parts no id; derive one that is identical on
                    # a retry so the tool ledger can recognise the same call.
                    id=f"call_{response_id}_{index}",
                    name=fc.get("name", ""),
                    arguments=args if isinstance(args, dict) else {},
                    raw_arguments=json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                )
            )

    usage = gemini_resp.get("usageMetadata", {})
    finish_reason = "tool_calls" if tool_calls else "stop"

    return NormalizedResponse(
        response_id=response_id,
        model=model_name,
        content="".join(text_pieces) if text_pieces else None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        input_tokens=usage.get("promptTokenCount", 0),
        output_tokens=usage.get("candidatesTokenCount", 0),
        raw_response=gemini_resp,
    )
