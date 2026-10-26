#!/usr/bin/env python3
"""Steward serving-backend benchmark: vLLM backend (OpenAI /v1 API, stream).

Same prompt set as bench_ollama.py (short ~56 tok, long ~578 tok input),
same max_tokens=200, temperature=0. vLLM returns exact usage token counts,
so decode tok/s = completion_tokens / (wall_total - wall_ttft).
Thinking disabled via chat_template_kwargs to match Ollama's default output.
"""
import json, time, urllib.request, sys, statistics

API = "http://127.0.0.1:8001/v1"
MODEL = "Qwen/Qwen3-4B"
MAX_TOKENS = 200

SHORT = ("Write a terse, useful reply that a personal assistant would give. "
         "Remind me to call the lab about the Thursday bloodwork results, "
         "confirm we have no other appointments that morning, and note the "
         "clinic number we should call first.")
BLOCK = ("The Steward assistant runs locally on the user's own hardware, so no "
         "conversation, prompt, or generated document ever leaves the box "
         "unless the user explicitly sends it out over the mesh tunnel or "
         "through a tool connector they authorized in the settings pane. ")
LONG = ("Review the following briefing and then answer in plain prose.\n\n"
        + BLOCK * 11 + "\n\nQuestion: Which properties make local-first "
        + "serving the right call for a personal assistant product?")

def chat(prompt_text, label):
    payload = {"model": MODEL,
               "messages": [{"role": "user", "content": prompt_text}],
               "max_tokens": MAX_TOKENS, "temperature": 0, "stream": True,
               "stream_options": {"include_usage": True},
               "chat_template_kwargs": {"enable_thinking": False}}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    resp = urllib.request.urlopen(req, timeout=600)
    tfirst = None
    prompt_tokens = completion_tokens = None
    buf = b""
    for raw in resp:
        if tfirst is None:
            tfirst = time.monotonic()
        buf += raw
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.decode().strip()
            if line.startswith("data:"):
                line = line[5:].strip()
                if line == "[DONE]":
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                u = d.get("usage") or {}
                if u.get("completion_tokens") is not None:
                    completion_tokens = u["completion_tokens"]
                if u.get("prompt_tokens"):
                    prompt_tokens = u["prompt_tokens"]
    tend = time.monotonic()
    ttft = (tfirst - t0) * 1000
    total = tend - t0
    gen = total - (tfirst - t0)
    decode_ts = completion_tokens / gen if (completion_tokens and gen > 0) else None
    out = {"label": label, "model": MODEL, "max_tokens": MAX_TOKENS,
           "input_tokens": prompt_tokens, "output_tokens": completion_tokens,
           "ttft_ms": round(ttft, 1), "decode_tok_s": round(decode_ts, 2) if decode_ts else None,
           "total_wall_s": round(total, 2)}
    print(json.dumps(out))
    return out

def main():
    print(f"# vLLM bench | model={MODEL} max_tokens={MAX_TOKENS}")
    results = []
    results.append(chat(SHORT, "cold-short"))     # first inference after server up
    for i in range(3):
        results.append(chat(SHORT, f"warm-short-{i}"))
    results.append(chat(LONG, "cold2-long"))
    for i in range(3):
        results.append(chat(LONG, f"warm-long-{i}"))
    def avg(key, labels):
        vals = [r[key] for r in results if r["label"] in labels and r[key]]
        return round(statistics.mean(vals), 2) if vals else None
    sw = [f"warm-short-{i}" for i in range(3)]
    lw = [f"warm-long-{i}" for i in range(3)]
    summary = {"model": MODEL,
               "ttft_ms_cold_short": results[0]["ttft_ms"],
               "ttft_ms_warm_short_avg": avg("ttft_ms", sw),
               "decode_tok_s_short_warm_avg": avg("decode_tok_s", sw),
               "ttft_ms_warm_long_avg": avg("ttft_ms", lw),
               "decode_tok_s_long_warm_avg": avg("decode_tok_s", lw),
               "input_tokens_short": results[0]["input_tokens"],
               "input_tokens_long": results[4]["input_tokens"],
               "output_tokens": results[0]["output_tokens"]}
    print("# SUMMARY:", json.dumps(summary))

if __name__ == "__main__":
    main()
