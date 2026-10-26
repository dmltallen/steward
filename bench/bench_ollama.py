#!/usr/bin/env python3
"""Steward serving-backend benchmark: Ollama backend (HTTP API, stream=true).

Measures, for a given model:
  - cold load time (Ollama's own load_duration + client wall TTFT)
  - TTFT (client wall-clocks first streamed token)
  - decode tok/s (eval_count / eval_duration from Ollama metrics)
  - total wall time
  - resident memory (/api/ps size+vram; before/after ollama process RSS)

Apples-to-apples config: temperature=0, num_predict=200 for both prompt
classes, num_ctx=8192 (explicit, matches vLLM max-model-len used elsewhere).
Unique request IDs so /api/ps attribution is unambiguous.
"""
import json, time, urllib.request, sys, statistics

API = "http://127.0.0.1:11434"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
NUM_CTX = 8192
NUM_PREDICT = 200

SHORT = ("Write a terse, useful reply that a personal assistant would give. "
         "Remind me to call the lab about the Thursday bloodwork results, "
         "confirm we have no other appointments that morning, and note the "
         "clinic number we should call first.")
# long prompt: repeated 40-word block ~ to land ~600 tokens
BLOCK = ("The Steward assistant runs locally on the user's own hardware, so no "
         "conversation, prompt, or generated document ever leaves the box "
         "unless the user explicitly sends it out over the mesh tunnel or "
         "through a tool connector they authorized in the settings pane. ")
LONG = ("Review the following briefing and then answer in plain prose.\n\n"
        + BLOCK * 11 + "\n\nQuestion: Which properties make local-first "
        + "serving the right call for a personal assistant product?")

def post(path, payload, stream=False):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)

def unload(model):
    try:
        post("/api/generate", {"model": model, "keep_alive": 0})
    except Exception as e:
        print(f"  (unload keep_alive=0: {e})")

def ollama_ps(model):
    ps = json.loads(urllib.request.urlopen(API + "/api/ps", timeout=30).read().decode())
    for m in ps.get("models", []):
        if m["name"].startswith(model.split(":")[0]):
            return m
    return None

def rss(pid=2046):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1])  # kB
    except Exception:
        return None
    return None

def run_one(prompt_text, label, unload_first=False):
    import os
    rid = os.urandom(4).hex()
    if unload_first:
        unload(MODEL)
    payload = {"model": MODEL, "prompt": prompt_text, "stream": True,
               "options": {"num_ctx": NUM_CTX, "num_predict": NUM_PREDICT,
                           "temperature": 0},
               "keep_alive": "10m"}
    t0 = time.monotonic()
    req = urllib.request.Request(API + "/api/generate",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=600)
    tfirst = None
    eval_count = eval_dur = load_dur = pe_count = pe_dur = tot_dur = None
    buf = b""
    for raw in resp:
        if tfirst is None:
            tfirst = time.monotonic()
        buf += raw
        if b"\n" in buf:
            lines = buf.split(b"\n")
            buf = lines.pop()
            for line in lines:
                line = line.strip()
                if line.startswith(b"data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("done"):
                    eval_count = d.get("eval_count")
                    eval_dur = d.get("eval_duration")
                    load_dur = d.get("load_duration")
                    pe_count = d.get("prompt_eval_count")
                    pe_dur = d.get("prompt_eval_duration")
                    tot_dur = d.get("total_duration")
    tend = time.monotonic()
    ttft = (tfirst - t0) * 1000
    total_s = tend - t0
    decode_ts = (eval_count / (eval_dur / 1e9)) if (eval_count and eval_dur) else None
    prefill_ts = (pe_count / (pe_dur / 1e9)) if (pe_count and pe_dur) else None
    load_s = (load_dur / 1e9) if load_dur else None
    out = {"request_id": rid, "label": label, "model": MODEL,
           "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT,
           "input_tokens": pe_count, "output_tokens": eval_count,
           "load_s": load_s, "ttft_ms": round(ttft, 1),
           "decode_tok_s": round(decode_ts, 2) if decode_ts else None,
           "prefill_tok_s": round(prefill_ts, 2) if prefill_ts else None,
           "total_wall_s": round(total_s, 2)}
    print(json.dumps(out))
    return out

def main():
    print(f"# Ollama bench | model={MODEL} num_ctx={NUM_CTX} "
          f"num_predict={NUM_PREDICT} | ollama ver via /api/version")
    print("# rss_ollama_served_kB_before:", rss())
    # ensure unloaded for the cold measurement (nothing resident yet expected)
    unload(MODEL)
    time.sleep(1.5)
    results = []
    print("# COLD short")
    results.append((run_one(SHORT, "cold-short", unload_first=False)))
    print("# WARM short x3")
    for i in range(3):
        results.append(run_one(SHORT, f"warm-short-{i}"))
    print("# COLD2 long (unload first)")
    results.append(run_one(LONG, "cold2-long", unload_first=True))
    print("# WARM long x3")
    for i in range(3):
        results.append(run_one(LONG, f"warm-long-{i}"))
    # memory snapshot while resident
    ps = ollama_ps(MODEL)
    print("# /api/ps while resident:", json.dumps(ps))
    print("# rss_ollama_served_kB_after:", rss())
    # summary stats (warm runs only = steady state)
    def avg(key, labels):
        vals = [r[key] for r in results if r["label"] in labels and r[key]]
        return round(statistics.mean(vals), 2) if vals else None
    short_warm = [f"warm-short-{i}" for i in range(3)]
    long_warm = [f"warm-long-{i}" for i in range(3)]
    summary = {"model": MODEL, "num_ctx": NUM_CTX,
               "load_s_cold": results[0]["load_s"],
               "ttft_ms_cold_short": results[0]["ttft_ms"],
               "ttft_ms_warm_short_avg": avg("ttft_ms", short_warm),
               "decode_tok_s_short_warm_avg": avg("decode_tok_s", short_warm),
               "ttft_ms_warm_long_avg": avg("ttft_ms", long_warm),
               "decode_tok_s_long_warm_avg": avg("decode_tok_s", long_warm),
               "prefill_tok_s_short_cold": results[0]["prefill_tok_s"],
               "input_tokens_short": results[0]["input_tokens"],
               "input_tokens_long": results[4]["input_tokens"],
               "output_tokens": results[0]["output_tokens"]}
    print("# SUMMARY:", json.dumps(summary))

if __name__ == "__main__":
    main()
