import http.client
import json
import logging
import select
import socket
import ssl
import time
import urllib.parse

HISTORY_TURNS = 6
IDLE_TIMEOUT = 90
USER_AGENT = "pi/0.60.0 (linux; python/3.12; arm64)"


def _parse_json_args(raw):
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


class _SSEConnectionPool:
    """Reuses keep-alive HTTP/HTTPS connections across turns; drops broken or aborted sockets."""

    def __init__(self):
        self._conns = {}
        self._ssl_ctx = ssl.create_default_context()

    def _make(self, scheme, host, port, timeout):
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ssl_ctx)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def request_stream(self, url, headers, body_bytes, timeout):
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = (scheme, host, port)

        for attempt in range(2):
            conn = self._conns.pop(key, None)
            reused = conn is not None
            if conn is None:
                conn = self._make(scheme, host, port, timeout)
            else:
                try:
                    if conn.sock is None:
                        reused = False
                        conn = self._make(scheme, host, port, timeout)
                    else:
                        conn.sock.settimeout(timeout)
                except OSError:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    reused = False
                    conn = self._make(scheme, host, port, timeout)
            try:
                conn.request("POST", path, body=body_bytes, headers=headers)
                resp = conn.getresponse()
                return key, conn, resp
            except (OSError, http.client.HTTPException):
                try:
                    conn.close()
                except Exception:
                    pass
                if not reused or attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    def release(self, key, conn):
        old = self._conns.get(key)
        if old is not None and old is not conn:
            try:
                old.close()
            except Exception:
                pass
        self._conns[key] = conn

    def close_all(self):
        while self._conns:
            _, conn = self._conns.popitem()
            try:
                conn.close()
            except Exception:
                pass


class Brain:
    """In-process streaming LLM & tool-calling engine with HTTP keep-alive pooling."""

    def __init__(self, config):
        self.config = dict(config)
        profile_fields = ("apiKey", "api", "provider", "baseUrl", "maxTokens", "contextWindow")
        self.profiles = {config["provider"]: {key: config.get(key) for key in profile_fields}}
        bailian = config.get("bailian", {})
        if bailian.get("apiKey") and "bailian" not in self.profiles:
            self.profiles["bailian"] = {key: bailian.get(key) for key in profile_fields}
        expert = config.get("expert", {})
        if expert.get("provider") == "openai" and expert.get("apiKey") and "openai" not in self.profiles:
            self.profiles["openai"] = {key: expert.get(key) for key in profile_fields}
            self.profiles["openai"].update(maxTokens=4096, api="openai-responses")
        self.provider_order = config.get("providerOrder", [])
        self.provider_order_model = config.get("providerOrderModel", config["model"])
        if config["model"] != self.provider_order_model:
            self.config["providerOrder"] = []
        self.messages = []
        self.last_turn = None
        self.next_id = 0
        self.request = None
        self._tool_Result = None
        self._pool = _SSEConnectionPool()

    def alive(self):
        return True

    def start(self):
        pass

    def configure_models(self, model, expert_model, provider):
        if provider not in self.profiles:
            raise ValueError("所选服务尚未配置日常对话凭据")
        self.config.update(self.profiles[provider])
        self.config["model"] = model
        self.config["expert"]["model"] = expert_model
        self.config["providerOrder"] = self.provider_order if model == self.provider_order_model else []

    def _profile(self, expert=False):
        if expert and (self.config.get("expert") or {}).get("apiKey"):
            return self.config["expert"]
        return self.config

    def _describe_error(self, message):
        text = str(message or "unknown error")
        for key in (self.config.get("apiKey"), (self.config.get("expert") or {}).get("apiKey"),
                    (self.config.get("bailian") or {}).get("apiKey")):
            if key:
                text = text.replace(key, "[redacted]")
        return text

    def _trim_history(self):
        user_indices = [i for i, m in enumerate(self.messages) if m.get("role") == "user"]
        if len(user_indices) > HISTORY_TURNS:
            cut = user_indices[-HISTORY_TURNS]
            self.messages = self.messages[cut:]

    def chat(self, text, context, thinking, search, force_tool, check_cancel, expert=False):
        """Yields ("delta", text) and ("tool", call, name, args); answer each tool with tool_result()."""
        self.next_id += 1
        req_id = self.next_id
        self.request = req_id
        selected = self._profile(expert)
        is_responses = selected.get("api") == "openai-responses"
        timeout = 240 if is_responses else (270 if expert else IDLE_TIMEOUT)

        before = list(self.messages)
        self.messages.append({"role": "user", "content": text})
        self.last_turn = {"id": req_id, "before": before, "messages": []}

        try:
            step = 0
            while True:
                check_cancel()
                first_step = step == 0
                step += 1
                if is_responses:
                    gen = self._stream_responses(
                        selected, context, search, force_tool if first_step else None,
                        expert, timeout, check_cancel,
                    )
                else:
                    gen = self._stream_completions(
                        selected, context, thinking, search, force_tool if first_step else None,
                        timeout, check_cancel,
                    )
                assistant_msg = None
                for item in gen:
                    if item[0] == "delta":
                        yield item
                    elif item[0] == "_assistant":
                        assistant_msg = item[1]

                if assistant_msg is None:
                    break
                self.messages.append(assistant_msg)
                tool_calls = [b for b in assistant_msg.get("content", []) if b.get("type") == "toolCall"]
                if not tool_calls:
                    break

                terminated = False
                for tc in tool_calls:
                    check_cancel()
                    self._tool_Result = None
                    yield "tool", tc["id"], tc["name"], tc["arguments"]
                    res = self._tool_Result or {"text": "完成", "ok": True, "final": False}
                    self._tool_Result = None
                    if not res.get("ok", True):
                        logging.warning("tool %s failed: %s", tc["name"], res.get("text"))
                    self.messages.append({
                        "role": "toolResult",
                        "toolCallId": tc["id"],
                        "toolName": tc["name"],
                        "text": res.get("text") or "完成",
                        "isError": not res.get("ok", True),
                    })
                    if res.get("final"):
                        terminated = True
                if terminated:
                    break
        except Exception as error:
            if error.__class__.__name__ == "CommandCancelled":
                raise
            raise RuntimeError(f"chat request failed: {self._describe_error(error)}") from error
        finally:
            if self.last_turn is not None and self.last_turn["id"] == req_id:
                self.last_turn["messages"] = self.messages[len(before):]

    def _build_responses_input(self, selected, context):
        items = [{"role": "developer", "content": f"{self.config.get('system', '')}{context or ''}"}]
        msg_idx = 0
        for msg in self.messages:
            role = msg.get("role")
            if role == "user":
                items.append({
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(msg.get("content") or "")}],
                })
                msg_idx += 1
            elif role == "assistant":
                same_model = (
                    msg.get("provider") == selected.get("provider")
                    and msg.get("api") == "openai-responses"
                    and msg.get("model") == selected.get("model")
                )
                text_idx = 0
                for block in msg.get("content", []):
                    btype = block.get("type")
                    if btype == "thinking":
                        if same_model and isinstance(block.get("reasoning_item"), dict):
                            items.append(block["reasoning_item"])
                    elif btype == "text":
                        fallback_id = f"msg_pi_{msg_idx}" if text_idx == 0 else f"msg_pi_{msg_idx}_{text_idx}"
                        text_idx += 1
                        msg_id = block.get("id") or fallback_id
                        if len(msg_id) > 64:
                            msg_id = fallback_id
                        entry = {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": str(block.get("text") or ""), "annotations": []}],
                            "status": "completed",
                            "id": msg_id,
                        }
                        if block.get("phase") in ("commentary", "final_answer"):
                            entry["phase"] = block["phase"]
                        items.append(entry)
                    elif btype == "toolCall":
                        call_id, _, item_id = str(block.get("id") or "").partition("|")
                        fc = {
                            "type": "function_call",
                            "call_id": call_id or f"call_{msg_idx}",
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("arguments") or {}, ensure_ascii=False),
                        }
                        if same_model and item_id.startswith("fc_"):
                            fc["id"] = item_id
                        items.append(fc)
                msg_idx += 1
            elif role == "toolResult":
                call_id = str(msg.get("toolCallId") or "").split("|")[0]
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(msg.get("text") or "完成"),
                })
                msg_idx += 1
        return items

    def _stream_responses(self, selected, context, search, force_tool, expert, timeout, check_cancel):
        url = selected["baseUrl"].rstrip("/") + "/responses"
        tools = []
        for entry in self.config.get("tools", []):
            spec = entry.get("function") or entry
            tools.append({
                "type": "function",
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
            })
        if self.config.get("search") and search:
            tools.append({"type": "web_search"})

        effort = (selected.get("reasoningEffort") or "high") if expert else "medium"
        payload = {
            "model": selected["model"],
            "input": self._build_responses_input(selected, context),
            "stream": True,
            "store": False,
            "max_output_tokens": max(16, int(selected.get("maxTokens") or 4096)),
            "reasoning": {"effort": effort, "summary": "auto"},
        }
        if tools:
            payload["tools"] = tools
        if force_tool:
            payload["tool_choice"] = {"type": "function", "name": force_tool}

        slots = {}
        blocks = []
        reasoning_by_id = {}
        completed = False

        for event in self._iter_sse(url, selected["apiKey"], payload, timeout, check_cancel):
            etype = event.get("type")
            if etype == "response.output_item.added":
                idx = event.get("output_index", 0)
                item = event.get("item") or {}
                itype = item.get("type")
                if itype == "reasoning":
                    block = {"type": "thinking", "reasoning_item": None}
                    blocks.append(block)
                    slots[idx] = ("thinking", block)
                elif itype == "message":
                    block = {"type": "text", "text": "", "id": item.get("id"), "phase": item.get("phase")}
                    blocks.append(block)
                    slots[idx] = ("text", block)
                elif itype == "function_call":
                    block = {
                        "type": "toolCall",
                        "id": f"{item.get('call_id', '')}|{item.get('id', '')}",
                        "name": item.get("name") or "",
                        "arguments": {},
                        "_partial": item.get("arguments") or "",
                    }
                    blocks.append(block)
                    slots[idx] = ("toolCall", block)
            elif etype in ("response.output_text.delta", "response.refusal.delta"):
                delta = event.get("delta") or ""
                if delta:
                    slot = slots.get(event.get("output_index", 0))
                    if slot and slot[0] == "text":
                        slot[1]["text"] += delta
                    yield "delta", delta
            elif etype == "response.function_call_arguments.delta":
                slot = slots.get(event.get("output_index", 0))
                if slot and slot[0] == "toolCall":
                    slot[1]["_partial"] = (slot[1].get("_partial") or "") + (event.get("delta") or "")
            elif etype == "response.function_call_arguments.done":
                slot = slots.get(event.get("output_index", 0))
                if slot and slot[0] == "toolCall" and event.get("arguments") is not None:
                    slot[1]["_partial"] = event["arguments"]
            elif etype == "response.output_item.done":
                idx = event.get("output_index", 0)
                item = event.get("item") or {}
                itype = item.get("type")
                slot = slots.pop(idx, None)
                if itype == "reasoning":
                    block = slot[1] if (slot and slot[0] == "thinking") else {"type": "thinking"}
                    if slot is None:
                        blocks.append(block)
                    block["reasoning_item"] = item
                    if item.get("id"):
                        reasoning_by_id[item["id"]] = block
                elif itype == "message":
                    block = slot[1] if (slot and slot[0] == "text") else {"type": "text", "text": ""}
                    if slot is None:
                        blocks.append(block)
                    content_list = item.get("content") or []
                    full_text = "".join(
                        (c.get("text") if c.get("type") == "output_text" else (c.get("refusal") or ""))
                        for c in content_list if isinstance(c, dict)
                    )
                    if full_text and not block.get("text"):
                        yield "delta", full_text
                    block["text"] = full_text or block.get("text") or ""
                    block["id"] = item.get("id") or block.get("id")
                    block["phase"] = item.get("phase") or block.get("phase")
                elif itype == "function_call":
                    block = slot[1] if (slot and slot[0] == "toolCall") else {
                        "type": "toolCall",
                        "id": f"{item.get('call_id', '')}|{item.get('id', '')}",
                        "name": item.get("name") or "",
                    }
                    if slot is None:
                        blocks.append(block)
                    raw_args = item.get("arguments") or block.pop("_partial", "") or "{}"
                    block.pop("_partial", None)
                    block["arguments"] = _parse_json_args(raw_args)
                    if item.get("name"):
                        block["name"] = item["name"]
            elif etype in ("response.completed", "response.incomplete"):
                completed = True
                resp = event.get("response") or {}
                for out_item in resp.get("output") or []:
                    if isinstance(out_item, dict) and out_item.get("type") == "reasoning" and out_item.get("encrypted_content"):
                        rblock = reasoning_by_id.get(out_item.get("id"))
                        if rblock and isinstance(rblock.get("reasoning_item"), dict):
                            rblock["reasoning_item"]["encrypted_content"] = out_item["encrypted_content"]
                status = resp.get("status")
                if status == "incomplete":
                    reason = (resp.get("incomplete_details") or {}).get("reason")
                    if reason and reason != "max_output_tokens":
                        raise RuntimeError(f"Response incomplete: {reason}")
                elif status in ("failed", "cancelled"):
                    err = resp.get("error") or {}
                    raise RuntimeError(f"{err.get('code', 'failed')}: {err.get('message', status)}")
            elif etype == "error":
                raise RuntimeError(f"Error {event.get('code')}: {event.get('message')}")
            elif etype == "response.failed":
                resp = event.get("response") or {}
                err = resp.get("error") or {}
                raise RuntimeError(f"{err.get('code', 'failed')}: {err.get('message', 'response failed')}")

        if not completed:
            raise RuntimeError("OpenAI Responses stream ended before completion")

        for b in blocks:
            if b.get("type") == "toolCall" and "_partial" in b:
                raise RuntimeError(f"OpenAI Responses stream ended with unfinished tool call: {b.get('name')}")

        yield "_assistant", {
            "role": "assistant",
            "content": blocks,
            "api": "openai-responses",
            "provider": selected.get("provider", "openai"),
            "model": selected.get("model", ""),
        }

    def _build_completions_messages(self, context):
        msgs = [{"role": "system", "content": f"{self.config.get('system', '')}{context or ''}"}]
        for idx, msg in enumerate(self.messages):
            role = msg.get("role")
            if role == "user":
                msgs.append({"role": "user", "content": str(msg.get("content") or "")})
            elif role == "assistant":
                text_parts = []
                tool_calls = []
                for block in msg.get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        text_parts.append(block["text"])
                    elif block.get("type") == "toolCall":
                        call_id = str(block.get("id") or f"call_{idx}").split("|")[0]
                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": block.get("name") or "",
                                "arguments": json.dumps(block.get("arguments") or {}, ensure_ascii=False),
                            },
                        })
                entry = {"role": "assistant", "content": "".join(text_parts) if text_parts else None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                msgs.append(entry)
            elif role == "toolResult":
                call_id = str(msg.get("toolCallId") or "").split("|")[0]
                msgs.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(msg.get("text") or "完成"),
                })
        return msgs

    def _stream_completions(self, selected, context, thinking, search, force_tool, timeout, check_cancel):
        url = selected["baseUrl"].rstrip("/") + "/chat/completions"
        provider = selected.get("provider", "bailian")
        tools = list(self.config.get("tools", []))
        payload = {
            "model": selected["model"],
            "messages": self._build_completions_messages(context),
            "stream": True,
            "max_tokens": int(selected.get("maxTokens") or 256),
            "temperature": self.config.get("temperature", 0.6),
        }
        if provider == "openrouter":
            payload["reasoning"] = {"effort": "low" if thinking else "minimal"}
            if self.config.get("providerOrder"):
                payload["provider"] = {"order": self.config["providerOrder"]}
            if self.config.get("search") and search:
                tools = tools + [{"type": "openrouter:web_search", "parameters": {"engine": "exa", "mode": "fast"}}]
        else:
            payload["enable_thinking"] = bool(thinking)
            if thinking:
                payload["thinking_budget"] = self.config.get("thinkingBudget", 1024)
            if self.config.get("search") and search:
                payload["enable_search"] = True
                payload["search_options"] = {"search_strategy": "turbo"}
        if tools:
            payload["tools"] = tools
        if force_tool:
            payload["tool_choice"] = {"type": "function", "function": {"name": force_tool}}

        text_parts = []
        tc_by_index = {}
        tc_order = []

        for chunk in self._iter_sse(url, selected["apiKey"], payload, timeout, check_cancel):
            if chunk.get("error"):
                err = chunk["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise RuntimeError(msg or "API stream error")
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = (choices[0] or {}).get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                yield "delta", content
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                slot = tc_by_index.get(idx)
                if slot is None:
                    slot = {
                        "id": tc.get("id") or f"call_{idx}",
                        "name": fn.get("name") or "",
                        "partial": fn.get("arguments") or "",
                    }
                    tc_by_index[idx] = slot
                    tc_order.append(slot)
                else:
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["partial"] += fn["arguments"]

        blocks = []
        if text_parts:
            blocks.append({"type": "text", "text": "".join(text_parts)})
        for slot in tc_order:
            blocks.append({
                "type": "toolCall",
                "id": slot["id"],
                "name": slot["name"],
                "arguments": _parse_json_args(slot["partial"]),
            })
        yield "_assistant", {
            "role": "assistant",
            "content": blocks,
            "api": "openai-completions",
            "provider": provider,
            "model": selected.get("model", ""),
        }

    def _iter_sse(self, url, api_key, payload, timeout, check_cancel):
        if not api_key:
            raise RuntimeError("No API key configured")
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Connection": "keep-alive",
            "User-Agent": USER_AGENT,
        }
        if "openrouter.ai" in url:
            headers["HTTP-Referer"] = "https://pi.dev"
            headers["X-OpenRouter-Title"] = "pi"

        key, conn, resp = self._pool.request_stream(url, headers, body_bytes, timeout=min(20.0, timeout))
        clean_eof = False
        try:
            if resp.status >= 400:
                err_body = resp.read(4096).decode("utf-8", errors="replace").strip()
                try:
                    err_json = json.loads(err_body)
                    if isinstance(err_json.get("error"), dict) and err_json["error"].get("message"):
                        err_body = err_json["error"]["message"]
                except ValueError:
                    pass
                raise RuntimeError(f"HTTP {resp.status} {err_body}")

            deadline = time.monotonic() + timeout
            raw_stream = getattr(resp.fp, "raw", None)
            sock = getattr(raw_stream, "_sock", None) or getattr(conn, "sock", None)
            sock_fd = sock.fileno() if sock is not None and hasattr(sock, "fileno") else None
            while True:
                check_cancel()
                if sock_fd is not None:
                    while True:
                        check_cancel()
                        if (hasattr(resp.fp, "peek") and resp.fp.peek(1)) or (
                            hasattr(sock, "pending") and sock.pending() > 0
                        ):
                            break
                        ready, _, _ = select.select([sock_fd], [], [], 0.2)
                        if ready:
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError("chat request timed out")
                raw_line = resp.readline()
                if not raw_line:
                    clean_eof = True
                    break
                deadline = time.monotonic() + timeout
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    clean_eof = True
                    break
                try:
                    event = json.loads(data_str)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
        finally:
            if clean_eof and not resp.will_close:
                try:
                    # Drain any trailing CRLF at end of chunked stream so keep-alive socket stays clean.
                    resp.read()
                    self._pool.release(key, conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            else:
                try:
                    conn.close()
                except Exception:
                    pass

    def tool_result(self, call, text, ok, final):
        self._tool_Result = {"call": call, "text": text, "ok": ok, "final": final}

    def heard(self, text, interrupted=False):
        if self.last_turn is None or self.request is None or self.last_turn.get("id") != self.request:
            self.request = None
            return
        turn_msgs = self.last_turn.get("messages") or []
        completed_calls = {
            m["toolCallId"] for m in turn_msgs if m.get("role") == "toolResult" and m.get("toolCallId")
        }
        kept = []
        for item in turn_msgs:
            role = item.get("role")
            if role in ("user", "toolResult"):
                kept.append(item)
            elif role == "assistant":
                tcs = [b for b in item.get("content", []) if b.get("type") == "toolCall" and b.get("id") in completed_calls]
                if tcs:
                    kept.append(dict(item, content=tcs))
        self.messages = list(self.last_turn["before"])
        if text or completed_calls:
            self.messages.extend(kept)
            reply_text = (
                text + ("（播报被用户打断，只听到了以上内容。）" if interrupted else "")
                if text
                else "（工具已执行，用户在播报前打断了回复。）"
            )
            selected = self._profile(False)
            self.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": reply_text}],
                "api": selected.get("api", "openai-completions"),
                "provider": selected.get("provider", "bailian"),
                "model": selected.get("model", ""),
            })
        self.last_turn = None
        self.request = None
        self._trim_history()

    def note(self, user, reply):
        """Records a turn answered locally so the model keeps the conversation context."""
        if user and reply:
            self.messages.append({"role": "user", "content": user})
            self.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": reply}],
                "api": self.config.get("api", "openai-completions"),
                "provider": self.config.get("provider", "bailian"),
                "model": self.config.get("model", ""),
            })
            self._trim_history()

    def close(self):
        self._pool.close_all()
