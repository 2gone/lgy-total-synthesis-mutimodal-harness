"""Structured output for a model without json_schema (DeepSeek V4.1 Flash 1017 on teamorouter and on DeepSeek's own API, probed
2026-10-17: json_schema and tool_choice=required are refused, json_object is accepted). Loaded into every child of a run whose environment
sets TSDAF_JSON_SCHEMA_SHIM=1 (PYTHONPATH points at this folder); without it nothing changes, and Gemini runs never set it.

A call that asks for json_schema is sent as json_object instead, with the very same schema appended to the system message; the tuned
prompt itself is not touched. The answer is checked against the schema by the host. A non-streamed answer that does not follow it is asked
again (up to SHIM_TRIES times); a streamed one is passed through chunk by chunk and, when it ends not following the schema, the stream
raises at its end, so the caller's own retry takes over, as it does for any failed call.

SHIM_ADDENDA (optional): a JSON map {text that identifies a step's system prompt: path of a file}. The file's text is appended to that
step's system prompt, before the schema. It is how a DeepSeek-only round adds principles without touching the tuned prompt files.

SHIM_REPLACE (optional): a JSON file holding a list of [old, new] sentence pairs; in a system prompt that holds old it is replaced by new
(a DeepSeek-only rewording of a tuned sentence). An old that the prompt of its step does not hold is noted in the thought log.

Both apply to every call that has a system prompt, whether or not its schema is converted.

SHIM_REPLACE_STEPS (optional, 2026-10-25): a JSON map {step: path of a JSON list of [old, new] pairs} for the steps segment, record and
paper_task, told apart as the thought log tells them; in the record step the pairs apply after SHIM_REPLACE. It is how an old/new prompt
comparison rewords the tuned prompts of all three steps without touching the prompt files. With SHIM_REPLACE_STRICT=1 an old that the
prompt of its step does not hold stops the call instead of being noted only.

SHIM_UNFENCE=1 (optional, 2026-10-21): a non-streamed answer that does not read as JSON but whose text from its first brace to its last
does (Gemini on lpzproxy wraps long json_object answers in a ```json fence) is passed on as that text, then checked as any answer is. The
thought log keeps the answer as it came. Off unless the run sets it; the DeepSeek runs do not."""
import json, os, threading, time

# TSDAF_IPV4_ONLY=1 (2026-10-25): mfy-win reaches lpzproxy over IPv4 but not over IPv6, and the HTTP client takes the first address the
# resolver gives (IPv6 on Windows); the run's own processes are given the IPv4 addresses only. Nothing outside the run is changed.
if os.environ.get('TSDAF_IPV4_ONLY') == '1':
    import socket
    _getaddrinfo = socket.getaddrinfo
    def _v4_first(host, *a, **k):
        found = _getaddrinfo(host, *a, **k); v4 = [x for x in found if x[0] == socket.AF_INET]
        return v4 or found
    socket.getaddrinfo = _v4_first

# TSDAF_THOUGHT_LOG (from 2026-10-18, every run): a file that receives one JSON line per model call with the thinking text the provider
# returns in plain words (DeepSeek: reasoning_content), the answer, the usage and which step asked, so that prompt changes can be traced in
# what the model actually thought. Independent of the schema shim; a provider that returns no thinking text leaves it empty.
LOG = os.environ.get('TSDAF_THOUGHT_LOG')
_lock = threading.Lock()
STEPS = (('Reaction blocks. A reaction block runs from', 'record'), ('the paper-level record', 'paper_task'), ('SI', 'segment'))
def _step(kw):
    sys_text = next((m.get('content') for m in kw.get('messages') or [] if m.get('role') == 'system' and isinstance(m.get('content'), str)), '') or ''
    return next((name for key, name in STEPS if key in sys_text), 'other')
def _ask(kw):
    flat = lambda c: c if isinstance(c, str) else ' '.join(x.get('text', '') for x in c or [] if isinstance(x, dict))
    users = [flat(m.get('content')) for m in kw.get('messages') or [] if m.get('role') == 'user']
    text = next((u for u in users if 'THE ENTRY TO WRITE' in u), users[-1] if users else '')
    at = text.find('THE ENTRY TO WRITE')      # the record step: what identifies the call is its entry, not the main text before it
    # a call that hands the record back carries the entry in its first message: the entry, and that it was handed back
    return (text[at:at + 300] if at >= 0 else text[:300]) + (' [handed back]' if len(users) > 1 else '')
def _write(kw, thought, answer, usage, t0, *, empty_response=None):
    if not LOG: return
    row = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'seconds': round(time.time() - t0, 1), 'model': kw.get('model'), 'effort': kw.get('reasoning_effort'), 'step': _step(kw),
           'asked': _ask(kw), 'turns': len(kw.get('messages') or []), 'thought': thought, 'answer': answer,
           'usage': usage.model_dump(exclude_none=True) if hasattr(usage, 'model_dump') else usage}
    if empty_response is not None:
        row.update(empty_response=True, provider_response=empty_response)
    with _lock:
        with open(LOG, 'a', encoding='utf-8') as f: f.write(json.dumps(row, ensure_ascii=False) + '\n')

if LOG or os.environ.get('TSDAF_JSON_SCHEMA_SHIM') == '1':
    import jsonschema
    from openai.resources.chat import completions as _c
    SHIM = os.environ.get('TSDAF_JSON_SCHEMA_SHIM') == '1'
    TRIES = int(os.environ.get('SHIM_TRIES', '3')); UNFENCE = os.environ.get('SHIM_UNFENCE') == '1'
    ADDENDA = {k: open(v, encoding='utf-8').read().strip() for k, v in json.loads(os.environ.get('SHIM_ADDENDA') or '{}').items()}
    REPLACE = json.loads(open(os.environ['SHIM_REPLACE'], encoding='utf-8').read()) if os.environ.get('SHIM_REPLACE') else []
    STEP_REPLACE = {step: json.loads(open(path, encoding='utf-8').read()) for step, path in json.loads(os.environ.get('SHIM_REPLACE_STEPS') or '{}').items()}
    STRICT = os.environ.get('SHIM_REPLACE_STRICT') == '1'

    def _edit(kw):
        """The run's own rewordings and additions to a step's system prompt (both empty unless the run sets them)."""
        if not (ADDENDA or REPLACE or STEP_REPLACE): return kw, []
        step = _step(kw); msgs = [dict(m) for m in kw.get('messages') or []]; missing = []
        at = next((i for i, m in enumerate(msgs) if m.get('role') == 'system' and isinstance(m.get('content'), str)), None)
        if at is None: return kw, []
        text = msgs[at]['content']
        if 'Reaction blocks. A reaction block runs from' in text:      # the rewordings are written for the entry-record prompt only
            for old, new in REPLACE:
                if old in text: text = text.replace(old, new)
                else: missing.append(old[:80])
        for old, new in STEP_REPLACE.get(step, []):
            if old in text: text = text.replace(old, new)
            else: missing.append(old[:80])
        if STRICT and missing: raise ValueError('SHIM_REPLACE_STEPS: not found in the %s prompt: %s' % (step, ' | '.join(missing)))
        added = ''.join('\n\n' + a for k, a in ADDENDA.items() if k in text)
        msgs[at]['content'] = text + added
        return {**kw, 'messages': msgs}, missing
    NOTE = ('\n\nAnswer with one JSON object and nothing else. It must follow this JSON Schema exactly: every required property present, '
            'no property the schema does not define, the types and enumerated values as given.\n')

    def _convert(kw):
        rf = kw.get('response_format')
        if not (isinstance(rf, dict) and rf.get('type') == 'json_schema'): return None
        schema = (rf.get('json_schema') or {}).get('schema') or {}
        msgs = [dict(m) for m in kw.get('messages') or []]
        text = NOTE + json.dumps(schema, ensure_ascii=False)
        sys_at = next((i for i, m in enumerate(msgs) if m.get('role') == 'system'), None)
        if sys_at is None: msgs.insert(0, {'role': 'system', 'content': text.strip()})
        elif isinstance(msgs[sys_at].get('content'), str): msgs[sys_at]['content'] += text
        else: msgs[sys_at]['content'] = list(msgs[sys_at]['content']) + [{'type': 'text', 'text': text}]
        return {**kw, 'messages': msgs, 'response_format': {'type': 'json_object'}}, schema

    def _unfenced(content):
        """The JSON an answer holds inside a fence or a sentence: its text from the first brace to the last, when the whole does not read and
        that part does; None otherwise (the answer is passed on as it came)."""
        try: json.loads(content); return None
        except ValueError: pass
        try:
            part = content[content.index('{'):content.rindex('}') + 1]; json.loads(part); return part
        except ValueError: return None

    def _problem(content, schema):
        try:
            jsonschema.validate(json.loads(content), schema); return None
        except Exception as e: return f'{type(e).__name__}: {str(e)[:300]}'

    class _Checked:
        """A stream passed through as it comes; at its end the joined answer is checked against the schema."""
        def __init__(self, stream, schema, kw, t0): self._s, self._schema, self._text, self._thought, self._kw, self._t0, self._usage = stream, schema, [], [], kw, t0, None
        def __iter__(self):
            for chunk in self._s:
                for ch in chunk.choices or []:
                    self._text.append(ch.delta.content or ''); self._thought.append(getattr(ch.delta, 'reasoning_content', None) or '')
                self._usage = chunk.usage or self._usage
                yield chunk
            text = ''.join(self._text)
            _write(self._kw, ''.join(self._thought), text, self._usage, self._t0)
            if self._schema and text.strip():
                p = _problem(text, self._schema)
                if p: raise ValueError('the answer does not follow the schema (json_object mode): ' + p)
        def __getattr__(self, name): return getattr(self._s, name)
        def close(self): return self._s.close()
        def __enter__(self): return self
        def __exit__(self, *e): self.close()

    _orig = _c.Completions.create
    def create(self, *a, **kw):
        kw, missing = _edit(kw)
        if missing: _write({**kw, 'messages': kw.get('messages')}, '', 'SHIM_REPLACE: not found in the prompt: ' + ' | '.join(missing), {}, time.time())
        conv = _convert(kw) if SHIM else None
        kw2, schema = conv if conv else (kw, None)
        t0 = time.time()
        if kw2.get('stream'): return _Checked(_orig(self, *a, **kw2), schema, kw2, t0)
        last = None
        for _ in range(TRIES if schema else 1):
            r = _orig(self, *a, **kw2)
            # an answer without any choice (2026-10-27: lpzproxy's Gemini, the same few inputs every time): kept in the log with what the
            # provider sent instead, and handed to the caller as it came -- its own check of r.choices takes it as a failed call
            if not r.choices:
                dump = r.model_dump(exclude_none=True) if hasattr(r, 'model_dump') else {}
                _write(kw2, '', '', getattr(r, 'usage', None), t0, empty_response=dump)
                return r
            content = r.choices[0].message.content or ''
            _write(kw2, getattr(r.choices[0].message, 'reasoning_content', None) or '', content, r.usage, t0)
            if UNFENCE and schema and (part := _unfenced(content)) is not None: r.choices[0].message.content = content = part
            if not schema: return r
            last = _problem(content, schema)
            if not last: return r
        raise ValueError('the answer does not follow the schema (json_object mode) after %d tries: %s' % (TRIES, last))
    _c.Completions.create = create
